#!/usr/bin/env python3
"""Parallel runner for E3c Column Grounding on current-chain failures.

For each failure question:
  1. Parse SELECT clause of draft SQL.
  2. Deterministic column probe: check column existence, ambiguity, COUNT(*),
     semantic mismatch with question keywords.
  3. If issues found, feed draft + column report to GLM-5.2 for correction.
  4. Pick best candidate (valid+non-empty, no gold).

Resume-safe, parallel.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.db_utils import BirdDatabase  # noqa: E402
from tools.llm_client import LLMClient  # noqa: E402

sys.path.insert(0, str(ROOT / "agents"))
from e3c_column_probe import probe_columns  # noqa: E402
from e4_execution_repair_agent import extract_sql  # noqa: E402


def _fmt_result(res: dict) -> str:
    if not res.get("ok"):
        return f"Execution error: {res.get('error', 'unknown')}"
    rows = res.get("rows") or []
    if not rows:
        return "Empty result set (0 rows)"
    return f"{len(rows)} rows (first {min(5, len(rows))}):\n" + "\n".join(
        " | ".join(str(v) for v in r) for r in rows[:5])


def _score(res: dict) -> tuple[int, int]:
    if not res.get("ok"):
        return (0, 0)
    return (1, 1 if res.get("rows") else 0)


def process_one(args):
    ex, cur_pred, cfg, client = args
    qid = ex.get("question_id")
    db_id = ex["db_id"]
    question = ex["question"]
    evidence = ex.get("evidence", "")
    sql = cur_pred.get("pred_sql", "")

    db = BirdDatabase(db_id=db_id, db_root=cfg["dataset"]["db_root"],
                      timeout=cfg["execution"]["timeout_seconds"],
                      max_rows=cfg["execution"]["max_rows"])
    rec: dict = {"question_id": qid, "db_id": db_id, "question": question}

    if not sql.strip():
        rec["pred_sql"] = ""
        return rec

    # 1. column probe
    probe = probe_columns(sql, question, db)
    candidates: list[tuple[str, str]] = [("base", sql)]

    # 2. LLM correction if issues found
    if probe["has_issues"]:
        schema = db.get_schema()
        draft_res = db.execute(sql)
        prompt_tmpl = open(cfg["prompt"]["template"]).read()
        prompt = (
            prompt_tmpl
            .replace("{db_id}", db_id)
            .replace("{schema}", schema)
            .replace("{evidence}", f"## Evidence\n{evidence}" if evidence else "")
            .replace("{question}", question)
            .replace("{draft_sql}", sql)
            .replace("{draft_result}", _fmt_result(draft_res))
            .replace("{column_report}", probe["report_text"])
        )
        try:
            comp = client.chat_completion(
                messages=[{"role": "system", "content": "You are an expert SQL assistant."},
                          {"role": "user", "content": prompt}],
                temperature=0.0, top_p=1.0, max_tokens=cfg["model"]["max_tokens"],
            )
            raw, _ = client.extract_content(comp)
            new_sql = extract_sql(raw)
            if new_sql and new_sql.strip().lower() != sql.strip().lower():
                candidates.append(("col_repair", new_sql))
        except Exception as e:
            rec["llm_error"] = str(e)[:200]

    # 3. pick best
    best_sql = sql
    best_key = (-1, -1, 0)
    prio = {"col_repair": 2, "base": 1}
    for tag, cand_sql in candidates:
        res = db.execute(cand_sql)
        sc = _score(res)
        key = (sc[0], sc[1], prio.get(tag, 0))
        if key > best_key:
            best_key = key
            best_sql = cand_sql
            rec["chosen"] = tag

    rec["pred_sql"] = best_sql
    rec["n_suggestions"] = len(probe["suggestions"])
    rec["has_issues"] = probe["has_issues"]
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--base-preds", required=True, type=Path)
    ap.add_argument("--dev", required=True, type=Path)
    ap.add_argument("--fail-qids", required=True, type=Path)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config))
    run_id = cfg["run_id"]
    out_dir = ROOT / "predictions" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "predictions.jsonl"

    dev = json.load(open(args.dev))
    dev_by = {d.get("question_id"): d for d in dev}
    base = {}
    for l in open(args.base_preds):
        if l.strip():
            d = json.loads(l); base[d["question_id"]] = d
    fail_ids = set(json.load(open(args.fail_qids)))

    done = set()
    if out_path.exists():
        for l in out_path.open():
            if l.strip():
                done.add(json.loads(l)["question_id"])
        print(f"resume: {len(done)}", flush=True)
    todo = [(dev_by[q], base[q]) for q in sorted(fail_ids) if q not in done and q in base]
    print(f"todo: {len(todo)}", flush=True)
    if not todo:
        return

    client = LLMClient(
        base_url=cfg["model"]["base_url"], model_name=cfg["model"]["model_name"],
        api_key_env=cfg["model"]["api_key_env"], timeout=args.timeout,
    )
    t0 = time.time(); written = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(process_one, (ex, bp, cfg, client)): ex.get("question_id") for ex, bp in todo}
        with out_path.open("a") as f:
            for fut in as_completed(futs):
                try:
                    rec = fut.result(timeout=args.timeout + 60)
                except Exception as e:
                    qid = futs[fut]
                    rec = {"question_id": qid, "pred_sql": base[qid]["pred_sql"], "error": str(e)[:150]}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                written += 1
                if written % 25 == 0:
                    print(f"  [{written}/{len(todo)}] {time.time()-t0:.0f}s", flush=True)
    print(f"done: {written} -> {out_path}", flush=True)


if __name__ == "__main__":
    main()
