#!/usr/bin/env python3
"""Deep regeneration agent: for blind-spot failures, use DeepSeek-V4-Flash to regenerate
SQL from scratch with full DB context (schema + column samples + FK graph +
evidence), then execute + repair up to 3 rounds.

This is not candidate reselection — it's fresh generation with maximal context.
Compliant: read-only DB, no gold SQL.
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
from tools.db_utils import BirdDatabase
from tools.llm_client import LLMClient
sys.path.insert(0, str(ROOT / "agents"))
from e3v_value_probe import probe_values
from e3c_column_probe import probe_columns
from e4_execution_repair_agent import extract_sql


def build_context(ex, db):
    """Build maximal DB context for the model."""
    schema = db.get_schema()
    fks = db.get_foreign_keys()
    fk_text = "\n".join(
        f"{fk.get('table')}.{fk.get('from_column')} -> {fk.get('referenced_table')}.{fk.get('to_column')}"
        for fk in fks if "error" not in fk
    ) or "No explicit foreign keys."

    # column samples for string columns
    tables = db.list_tables()[:5]  # limit to avoid token explosion
    samples_lines = []
    for t in tables:
        try:
            with db._connection() as conn:
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info(`{t}`)").fetchall()][:10]
            for c in cols:
                vals = db.get_column_samples(t, c, limit=3)
                if vals and any(isinstance(v, str) for v in vals):
                    samples_lines.append(f"  {t}.{c}: {[str(v)[:30] for v in vals[:3]]}")
        except Exception:
            pass
    samples_text = "\n".join(samples_lines[:20]) if samples_lines else "(no samples)"

    return schema, fk_text, samples_text


def process_one(args):
    ex, cur_pred, cfg, client = args
    qid = ex.get("question_id")
    db_id = ex["db_id"]
    question = ex["question"]
    evidence = ex.get("evidence", "")
    draft_sql = cur_pred.get("pred_sql", "")

    db = BirdDatabase(db_id=db_id, db_root=cfg["dataset"]["db_root"],
                      timeout=cfg["execution"]["timeout_seconds"], max_rows=cfg["execution"]["max_rows"])
    rec = {"question_id": qid, "db_id": db_id, "question": question}
    candidates = [("base", draft_sql)] if draft_sql.strip() else []

    # Round 1: fresh regeneration with full context
    schema, fk_text, samples_text = build_context(ex, db)
    prompt1 = f"""You are an expert SQL assistant. Generate a correct SQLite SELECT query.

## Database
{db_id}

## Schema
{schema}

## Foreign Keys
{fk_text}

## Column Samples (actual values in the database)
{samples_text}

{"## Evidence" + chr(10) + evidence if evidence else ""}

## Question
{question}

## SQL
"""
    try:
        comp = client.chat_completion(
            messages=[{"role": "system", "content": "You are an expert SQL assistant. Output only SQL."},
                      {"role": "user", "content": prompt1}],
            temperature=0.3, top_p=0.95, max_tokens=cfg["model"]["max_tokens"],
        )
        raw, _ = client.extract_content(comp)
        regen_sql = extract_sql(raw)
        if regen_sql and regen_sql.strip().lower() != draft_sql.strip().lower():
            candidates.append(("regen", regen_sql))
    except Exception as e:
        rec["regen_error"] = str(e)[:150]

    # Round 2-3: execution repair on best candidate
    best_sql = draft_sql
    best_score = (-1, -1)
    for tag, sql in candidates:
        res = db.execute(sql)
        ok = res.get("ok", False)
        nonempty = bool(res.get("rows"))
        key = (1 if ok else 0, 1 if nonempty else 0)
        if key > best_score:
            best_score = key
            best_sql = sql

    # repair loop
    for rnd in range(3):
        res = db.execute(best_sql)
        if res.get("ok") and res.get("rows"):
            break
        error = res.get("error", "empty result") if not res.get("ok") else "empty result"
        prompt2 = f"""Fix this SQL query. It returned an error or empty result.

Question: {question}
Evidence: {evidence}

Schema: {schema[:1500]}

Failed SQL: {best_sql}
Error: {error}

Output only the corrected SQL:"""
        try:
            comp = client.chat_completion(
                messages=[{"role": "user", "content": prompt2}],
                temperature=0.0, max_tokens=cfg["model"]["max_tokens"],
            )
            raw, _ = client.extract_content(comp)
            new_sql = extract_sql(raw)
            if new_sql and new_sql.strip().lower() != best_sql.strip().lower():
                best_sql = new_sql
                candidates.append((f"repair_r{rnd+1}", new_sql))
            else:
                break
        except Exception:
            break

    # pick best by valid+nonempty
    final_sql = draft_sql
    final_key = (-1, -1, 0)
    prio = {"repair_r3": 5, "repair_r2": 4, "repair_r1": 3, "regen": 2, "base": 1}
    for tag, sql in candidates:
        res = db.execute(sql)
        key = (1 if res.get("ok") else 0, 1 if res.get("rows") else 0, prio.get(tag, 0))
        if key > final_key:
            final_key = key
            final_sql = sql
            rec["chosen"] = tag

    rec["pred_sql"] = final_sql
    rec["n_candidates"] = len(candidates)
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
