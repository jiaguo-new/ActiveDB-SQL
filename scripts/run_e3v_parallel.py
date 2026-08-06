#!/usr/bin/env python3
"""Parallel runner for E3v Active-Probe Value Grounding on base-failure questions.

For each base-failure question:
  1. Execute the base draft SQL on the read-only DB.
  2. Deterministic value probe: extract WHERE literals, look up actual DB cell
     values, fuzzy-match to find the correct literal.
  3. If probe found repairs, try the repaired SQL; also feed probe info + draft
     to GLM-5.2 for a structural correction pass.
  4. Pick the best among {base, repaired, llm-corrected} by execution validity
     + non-empty result (no gold used).

Only base-FAILURE questions are processed; base-correct questions reuse the
base prediction.  Resume-safe (checkpoint per question).
"""
from __future__ import annotations

import argparse
import json
import os
import re
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
from e3v_value_probe import probe_values  # noqa: E402
from e4_execution_repair_agent import extract_sql  # noqa: E402 (GLM think-tag safe)


def _fmt_result(res: dict) -> str:
    if not res.get("ok"):
        return f"Execution error: {res.get('error', 'unknown')}"
    rows = res.get("rows") or []
    if not rows:
        return "Empty result set (0 rows)"
    sample = [" | ".join(str(v) for v in r) for r in rows[:5]]
    return f"{len(rows)} rows (first {min(5, len(rows))}):\n" + "\n".join(sample)


def _score_candidate(res: dict) -> tuple[int, int]:
    """Higher is better: (valid, non_empty). valid 0/1, non_empty 0/1."""
    if not res.get("ok"):
        return (0, 0)
    rows = res.get("rows") or []
    return (1, 1 if len(rows) > 0 else 0)


def process_one(args):
    ex, base_pred, cfg, client = args
    qid = ex.get("question_id")
    db_id = ex["db_id"]
    question = ex["question"]
    evidence = ex.get("evidence", "")
    draft_sql = base_pred.get("pred_sql", "")

    db = BirdDatabase(db_id=db_id, db_root=cfg["dataset"]["db_root"],
                      timeout=cfg["execution"]["timeout_seconds"],
                      max_rows=cfg["execution"]["max_rows"])

    rec: dict = {"question_id": qid, "db_id": db_id, "question": question}

    if not draft_sql.strip():
        rec["pred_sql"] = ""
        rec["stage"] = "empty_base"
        return rec

    # candidates to choose from
    candidates: list[tuple[str, str]] = [("base", draft_sql)]

    # 1. execute draft
    draft_res = db.execute(draft_sql)

    # 2. deterministic value probe
    probe = probe_values(draft_sql, db, sample_limit=cfg["value_grounding"]["sample_limit"])
    if probe["repaired_sql"] != draft_sql:
        candidates.append(("det_probe", probe["repaired_sql"]))

    # 3. LLM correction (only if draft failed or probe found repairs)
    draft_score = _score_candidate(draft_res)
    needs_llm = (draft_score[1] == 0) or len(probe["repairs"]) > 0
    if needs_llm:
        schema = db.get_schema()
        fks_raw = db.get_foreign_keys()
        fks = "\n".join(
            f"{fk.get('table')}.{fk.get('from_column')} -> {fk.get('referenced_table')}.{fk.get('to_column')}"
            for fk in fks_raw if "error" not in fk
        ) or "No explicit foreign keys detected."
        prompt_tmpl = open(cfg["prompt"]["template"]).read()
        prompt = (
            prompt_tmpl
            .replace("{db_id}", db_id)
            .replace("{schema}", schema)
            .replace("{fks}", fks)
            .replace("{evidence}", f"## Evidence\n{evidence}" if evidence else "")
            .replace("{question}", question)
            .replace("{draft_sql}", draft_sql)
            .replace("{draft_result}", _fmt_result(draft_res))
            .replace("{cell_values}", probe["cell_values_text"])
        )
        try:
            comp = client.chat_completion(
                messages=[{"role": "system", "content": "You are an expert SQL assistant."},
                          {"role": "user", "content": prompt}],
                temperature=0.0, top_p=1.0, max_tokens=cfg["model"]["max_tokens"],
            )
            raw, usage = client.extract_content(comp)
            llm_sql = extract_sql(raw)
            if llm_sql and llm_sql.strip() != draft_sql.strip():
                candidates.append(("llm_correct", llm_sql))
            rec["llm_request_id"] = comp["response"].get("id")
        except Exception as e:
            rec["llm_error"] = str(e)[:200]

    # 4. pick best candidate (no gold): valid > invalid; non-empty > empty;
    #    tie-break: prefer llm_correct > det_probe > base
    best_sql = draft_sql
    best_score = (-1, -1)
    priority = {"llm_correct": 3, "det_probe": 2, "base": 1}
    for tag, sql in candidates:
        res = db.execute(sql)
        sc = _score_candidate(res)
        key = (sc[0], sc[1], priority.get(tag, 0))
        if key > (best_score[0], best_score[1], 0):
            best_score = sc
            best_sql = sql
            rec["chosen"] = tag

    rec["pred_sql"] = best_sql
    rec["n_repairs"] = len(probe["repairs"])
    rec["probe_repairs"] = probe["repairs"][:5]
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--base-preds", required=True, type=Path)
    ap.add_argument("--dev", required=True, type=Path)
    ap.add_argument("--failures-only", action="store_true", default=True,
                    help="Only process base-failure questions (default)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--per-call-timeout", type=float, default=120.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--fail-qids", type=Path, default=None,
                    help="JSON list of question_ids to process (overrides auto fail detection)")
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config))
    run_id = cfg["run_id"]
    out_dir = ROOT / "predictions" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "predictions.jsonl"

    dev = json.load(open(args.dev))
    base = {}
    for l in open(args.base_preds):
        if l.strip():
            d = json.loads(l)
            base[d["question_id"]] = d

    # determine failures
    fail_ids = None
    if args.fail_qids is not None:
        fail_ids = set(json.load(open(args.fail_qids)))
        print(f"fail-qids: {len(fail_ids)} questions", flush=True)
    elif args.failures_only:
        import glob
        mfiles = glob.glob(str(ROOT / "metrics/e0_bird_dev_full_glm5.2_cot8k_20260724_eval.json"))
        if mfiles:
            m = json.load(open(mfiles[0]))
            fail_ids = {r["idx"] for r in m["per_query"] if not r["ex"]}
            print(f"failures-only: {len(fail_ids)} base-failure questions", flush=True)

    # resume
    done = set()
    if out_path.exists():
        for l in out_path.open():
            if l.strip():
                done.add(json.loads(l)["question_id"])
        print(f"resume: {len(done)} done", flush=True)

    todo = []
    for ex in dev:
        qid = ex.get("question_id")
        if qid in done:
            continue
        bp = base.get(qid)
        if bp is None:
            continue
        if fail_ids is not None and qid not in fail_ids:
            continue
        todo.append((ex, bp))
    if args.limit:
        todo = todo[: args.limit]
    print(f"todo: {len(todo)}", flush=True)
    if not todo:
        return

    client = LLMClient(
        base_url=cfg["model"]["base_url"],
        model_name=cfg["model"]["model_name"],
        api_key_env=cfg["model"]["api_key_env"],
        timeout=args.per_call_timeout,
    )

    t0 = time.time()
    written = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(process_one, (ex, bp, cfg, client)): ex.get("question_id")
                   for ex, bp in todo}
        with out_path.open("a", encoding="utf-8") as f:
            for fut in as_completed(futures):
                try:
                    rec = fut.result(timeout=args.per_call_timeout + 60)
                except Exception as e:
                    qid = futures[fut]
                    rec = {"question_id": qid, "pred_sql": base[qid]["pred_sql"],
                           "stage": "future_error", "error": str(e)[:150]}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                written += 1
                if written % 25 == 0:
                    el = time.time() - t0
                    print(f"  [{written}/{len(todo)}] {el:.0f}s ETA {(len(todo)-written)/(written/el if el else 1):.0f}s",
                          flush=True)
    print(f"done: {written} -> {out_path}", flush=True)


if __name__ == "__main__":
    main()
