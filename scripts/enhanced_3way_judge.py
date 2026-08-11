#!/usr/bin/env python3
"""Enhanced 3-way judge for selector failures.

Instead of pairwise knockout (which can propagate early errors),
present top-3 distinct-result candidates simultaneously to DeepSeek
with full execution results and a structured analysis template.
"""
from __future__ import annotations
import argparse, hashlib, json, random, re, sys, time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.db_utils import BirdDatabase
from tools.llm_client import LLMClient


def _norm_cell(v):
    if v is None: return None
    if isinstance(v, (int, float)): return round(float(v), 3)
    return str(v).strip().lower()

def _rows_key(rows):
    if rows is None: return None
    try:
        h = {hashlib.md5(json.dumps(tuple(_norm_cell(v) for v in r), ensure_ascii=False).encode()).hexdigest() for r in rows}
        return hashlib.md5(",".join(sorted(h)).encode()).hexdigest()
    except: return None

def _fmt(res):
    if not res.get("ok"): return f"Error: {res.get('error','')[:80]}"
    rows = res.get("rows") or []
    if not rows: return "Empty (0 rows)"
    return f"{len(rows)} rows | " + " | ".join(str(v)[:30] for v in rows[0][:5])

JUDGE_PROMPT = """You are an expert SQL evaluator. Three candidate SQL queries answer the same question but may produce different results. Analyze each carefully and pick the BEST one.

Database: {db_id}

Question: {question}
Evidence: {evidence}

{candidates}

Analysis:
For each candidate, check:
1. Does it answer the EXACT question (count vs list vs average vs max)?
2. Are the JOINs correct (right tables, right ON conditions)?
3. Is the WHERE filter correct (matching question conditions, right values)?
4. Is the aggregation correct (COUNT column, SUM, AVG, etc)?
5. Does the result make sense for this question?

After analyzing all candidates, output your final answer:
WINNER: A  (or B, or C)

If two candidates are equally good, prefer the one with higher confidence (simpler SQL, more standard patterns)."""


def process_one(args):
    ex, db_root, scored_sample, cur_sql, client = args
    qid = ex.get("question_id")
    db = BirdDatabase(db_id=ex["db_id"], db_root=db_root, timeout=30, max_rows=100)

    cands = scored_sample.get("candidates", [])
    exec_cands = [c for c in cands if c.get("result") is not None]
    if len(exec_cands) < 2:
        return {"question_id": qid, "pred_sql": cur_sql, "chosen": "base"}

    exec_cands.sort(key=lambda c: -c.get("orm_score", 0.5))
    top_k = exec_cands[:8]

    # execute + build detail
    detail = []
    for c in top_k:
        res = db.execute(c["sql"])
        detail.append({
            "sql": c["sql"], "model": c.get("model",""),
            "orm_score": c.get("orm_score",0.5),
            "ok": res.get("ok",False), "hash": _rows_key(res.get("rows")),
            "result_text": _fmt(res),
        })

    exec_d = [d for d in detail if d["ok"] and d["hash"]]
    if len(exec_d) < 2:
        return {"question_id": qid, "pred_sql": cur_sql, "chosen": "base"}

    hashes = [d["hash"] for d in exec_d]
    if len(set(hashes)) == 1:
        return {"question_id": qid, "pred_sql": cur_sql, "chosen": "base"}

    # top-3 distinct results
    distinct = []
    seen = set()
    for d in exec_d:
        if d["hash"] not in seen:
            distinct.append(d); seen.add(d["hash"])
        if len(distinct) >= 3: break
    if len(distinct) < 2:
        return {"question_id": qid, "pred_sql": cur_sql, "chosen": "base"}

    # build candidate blocks
    blocks = []
    for i, d in enumerate(distinct):
        label = chr(65+i)
        blocks.append(
            f"Candidate {label}:\n"
            f"```sql\n{d['sql'].strip()}\n```\n"
            f"Result: {d['result_text']}"
        )

    prompt = JUDGE_PROMPT.format(
        db_id=ex["db_id"], question=ex["question"],
        evidence=ex.get("evidence",""),
        candidates="\n\n".join(blocks),
    )

    try:
        comp = client.chat_completion(
            messages=[
                {"role": "system", "content": "You are an expert SQL evaluator. Analyze step by step, then give a clear final verdict."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.0, max_tokens=2048,
        )
        raw = comp["response"]["choices"][0]["message"]["content"]
        # parse WINNER: A/B/C
        winner_label = None
        for m in re.finditer(r"WINNER:\s*([ABC])", raw, re.I):
            winner_label = m.group(1).upper()
        if not winner_label:
            # fallback: last occurrence of A/B/C
            for w in ["A","B","C"]:
                if f"answer is {w}" in raw.lower() or f"winner is {w}" in raw.lower() or f"choose {w}" in raw.lower():
                    winner_label = w; break
        if not winner_label:
            # last resort: any standalone A/B/C in last 200 chars
            tail = raw[-200:].upper()
            for w in ["A","B","C"]:
                if w in tail:
                    winner_label = w; break

        if winner_label and winner_label in "ABC":
            idx = ord(winner_label) - 65
            if idx < len(distinct):
                winner = distinct[idx]
                # consensus check: prefer higher ORM if same result
                for d in distinct:
                    if d is not winner and d["hash"] == winner["hash"] and d["orm_score"] > winner["orm_score"]:
                        winner = d
                res = db.execute(winner["sql"])
                if res.get("ok") and res.get("rows"):
                    return {"question_id": qid, "pred_sql": winner["sql"], "chosen": "3way_judge"}
        return {"question_id": qid, "pred_sql": cur_sql, "chosen": "judge_failed"}
    except Exception as e:
        return {"question_id": qid, "pred_sql": cur_sql, "chosen": "error", "error": str(e)[:80]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scored-pool", required=True, type=Path)
    ap.add_argument("--base-preds", required=True, type=Path)
    ap.add_argument("--dev", required=True, type=Path)
    ap.add_argument("--db-root", required=True, type=Path)
    ap.add_argument("--fail-qids", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)

    pool = {}
    for l in args.scored_pool.open():
        if l.strip(): d = json.loads(l); pool[d["id"]] = d
    base = {}
    for l in args.base_preds.open():
        if l.strip(): d = json.loads(l); base[d["question_id"]] = d
    dev = json.load(open(args.dev))
    fail_ids = set(json.load(open(args.fail_qids)))

    client = LLMClient(
        base_url="https://api.deepseek.com",
        model_name="deepseek-v4-flash",
        api_key_env="DEEPSEEK_API_KEY", timeout=120,
    )

    todo = [ex for ex in dev if ex.get("question_id") in fail_ids and ex.get("question_id") in pool]
    print(f"todo: {len(todo)}", flush=True)

    written = 0; chosen = Counter()
    with args.output.open("w") as fout, ThreadPoolExecutor(max_workers=args.workers) as p:
        futs = {p.submit(process_one, (ex, args.db_root, pool.get(ex["question_id"],{}),
                     base.get(ex["question_id"],{}).get("pred_sql",""), client)): ex["question_id"] for ex in todo}
        for fut in as_completed(futs):
            try: rec = fut.result(timeout=150)
            except: rec = {"question_id": futs[fut], "pred_sql": base.get(futs[fut],{}).get("pred_sql",""), "chosen": "error"}
            fout.write(json.dumps(rec, ensure_ascii=False) + "\n"); fout.flush()
            chosen[rec.get("chosen","?")] += 1; written += 1
            if written % 25 == 0: print(f"  [{written}/{len(todo)}] {dict(chosen)}", flush=True)
    print(f"done: {written} -> {args.output}")
    print(f"chosen: {dict(chosen)}")

if __name__ == "__main__":
    main()
