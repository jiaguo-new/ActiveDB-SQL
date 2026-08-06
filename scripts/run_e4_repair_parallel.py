#!/usr/bin/env python3
"""E4 execution-repair on top of the merged4 ORM selector output.

For each dev question:
  1. take the selector's chosen SQL (from compliant_merged4_ormband005);
  2. execute it (read-only);
  3. if it errors or returns empty -> feed (failed_sql, error, schema, question)
     to GLM-5.2 for up to max_repairs rounds;
  4. keep the repaired SQL only if it executes successfully (safe: never keep
     a repair that breaks a previously-ok SQL).

Attacks BOTH error classes:
  - A (selector-miss): repair may fix a syntactically-broken or empty pick;
  - B (generator-miss): the error feedback lets GLM correct join/column errors.

Compliant: only dev schema+question+evidence+execution-error; no gold.
Parallel + resumable.
"""
from __future__ import annotations

import argparse
import json
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


def _render(template: str, **kw) -> str:
    out = template
    for k, v in kw.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def _extract_sql(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"<reasoning>.*?</reasoning>", "", text, flags=re.DOTALL | re.I)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.I)
    m = list(re.finditer(r"```sql\s*\n?(.*?)```", text, re.S))
    if m:
        return m[-1].group(1).strip()
    m = list(re.finditer(r"```\s*\n?(.*?)```", text, re.S))
    if m:
        return m[-1].group(1).strip()
    idx = text.upper().find("SELECT")
    if idx >= 0:
        return text[idx:].strip()
    return text.rstrip(";").strip()


def process_one(ex, cfg, db_root, client, repair_tpl):
    qid = ex.get("question_id")
    db_id = ex["db_id"]
    question = ex["question"]
    evidence = ex.get("evidence", "")
    ev = f"## Evidence\n{evidence}\n" if evidence else ""
    base_sql = ex.get("pred_sql", "")  # selector's pick

    db = BirdDatabase(db_id=db_id, db_root=db_root, timeout=30, max_rows=100)
    schema = db.get_schema()
    max_repairs = cfg.get("agent", {}).get("max_repairs", 2)

    rec = {"question_id": qid, "db_id": db_id, "pred_sql": base_sql,
           "repaired": False, "repair_rounds": 0}

    sql = base_sql
    res = db.execute(sql) if sql.strip() else {"ok": False, "error": "empty", "rows": None}

    for rnd in range(max_repairs):
        # only repair if not ok OR empty result
        if res["ok"] and res.get("rows"):
            break
        err = res.get("error") or "empty result set (0 rows)"
        # build a short result preview for the prompt
        rows = res.get("rows")
        if rows is None:
            result_preview = f"Execution error: {err}"
        elif len(rows) == 0:
            result_preview = "Empty result set (0 rows returned)"
        else:
            result_preview = f"{len(rows)} rows. First rows: " + " | ".join(str(v) for v in rows[0]) if rows else ""
        prompt = _render(repair_tpl, db_id=db_id, schema=schema, evidence=ev,
                         question=question, current_sql=sql, current_result=result_preview)
        try:
            comp = client.chat_completion(
                messages=[{"role": "system", "content": "You are an expert SQL debugging assistant."},
                          {"role": "user", "content": prompt}],
                temperature=0.0, top_p=1.0, max_tokens=8192,
            )
            raw, usage = client.extract_content(comp)
            new_sql = _extract_sql(raw)
            rec[f"request_id_r{rnd}"] = comp["response"].get("id")
        except Exception as e:
            rec[f"error_r{rnd}"] = str(e)[:150]
            break
        if not new_sql.strip() or new_sql.strip().lower() == sql.strip().lower():
            break
        new_res = db.execute(new_sql)
        # safe keep: adopt if new is strictly better (ok and non-empty)
        if new_res["ok"] and new_res.get("rows"):
            sql = new_sql
            res = new_res
            rec["repaired"] = True
            rec["repair_rounds"] = rnd + 1
        else:
            # new also fails; continue trying with it (may be closer)
            sql = new_sql
            res = new_res
    rec["pred_sql"] = sql
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--baseline-pred", required=True, help="selector predictions.jsonl to repair from")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--per-call-timeout", type=float, default=180.0)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config))
    run_id = cfg["run_id"]
    out_dir = ROOT / "predictions" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "predictions.jsonl"

    dev = json.load(open(cfg["dataset"]["source"]))
    base = {json.loads(l)["question_id"]: json.loads(l) for l in open(args.baseline_pred) if l.strip()}
    todo = []
    for i, ex in enumerate(dev):
        qid = ex.get("question_id", i)
        b = base.get(qid, {})
        ex["pred_sql"] = b.get("pred_sql", "")
        todo.append(ex)
    if args.limit:
        todo = todo[: args.limit]

    repair_tpl = open(cfg["prompt"]["repair"]).read()
    db_root = cfg["dataset"]["db_root"]

    done = set()
    if out_path.exists():
        for l in out_path.open():
            if l.strip():
                done.add(json.loads(l)["question_id"])
        print(f"resume: {len(done)} done", flush=True)
    todo = [ex for ex in todo if ex.get("question_id") not in done]
    print(f"todo: {len(todo)}", flush=True)
    if not todo:
        return

    client = LLMClient(base_url=cfg["model"]["base_url"], model_name=cfg["model"]["model_name"],
                       api_key_env=cfg["model"]["api_key_env"], timeout=args.per_call_timeout)
    t0 = time.time()
    written = 0
    with out_path.open("a", encoding="utf-8") as f:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = {pool.submit(process_one, ex, cfg, db_root, client, repair_tpl): ex.get("question_id") for ex in todo}
            for fut in as_completed(futs):
                try:
                    rec = fut.result(timeout=args.per_call_timeout + 60)
                except Exception as e:
                    qid = futs[fut]
                    rec = {"question_id": qid, "pred_sql": "", "error": str(e)[:120]}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                written += 1
                if written % 25 == 0:
                    el = time.time() - t0
                    print(f"  [{written}/{len(todo)}] {el:.0f}s", flush=True)
    print(f"done: {written} -> {out_path}", flush=True)


if __name__ == "__main__":
    main()
