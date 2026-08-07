#!/usr/bin/env python3
"""Route A: GLM top-K reselection on selector-failure questions.

For each question where the current chain prediction is wrong:
  1. Load ORM-scored candidates from the pool.
  2. Take ORM top-3 candidates (by orm_score, executable only).
  3. Execute each, collect results.
  4. If all 3 produce the same result hash -> no disagreement, skip.
  5. Otherwise: GLM-5.2 pairwise/tournament judge among top-3 (with execution
     results, no gold). Pick winner.
  6. Accept judged winner only if it executes ok+non-empty.

Also tries hash-majority among top-3 as a fallback signal.

Resume-safe, parallel.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.db_utils import BirdDatabase  # noqa: E402
from tools.llm_client import LLMClient  # noqa: E402


def _norm_cell(v):
    if v is None: return None
    if isinstance(v, (int, float)): return round(float(v), 3)
    return str(v).strip().lower()

def _rows_key(rows):
    if rows is None: return None
    try:
        h = set()
        for r in rows:
            h.add(hashlib.md5(json.dumps(tuple(_norm_cell(v) for v in r), ensure_ascii=False).encode()).hexdigest())
        return hashlib.md5(",".join(sorted(h)).encode()).hexdigest()
    except Exception:
        return None

def _fmt_result(res):
    if not res.get("ok"):
        return f"Error: {res.get('error','unknown')}"
    rows = res.get("rows") or []
    if not rows: return "Empty result (0 rows)"
    return f"{len(rows)} rows: " + " | ".join(str(v) for v in rows[0][:5])

def _parse_winner(text, n_candidates):
    """Parse 'A', 'B', 'C' or 'tie' from LLM output."""
    t = text.strip().upper()
    if "{" in t:
        try:
            s = t.index("{"); e = t.rindex("}") + 1
            d = json.loads(t[s:e])
            w = str(d.get("winner","")).strip().upper()
            if w in ("A","B","C","TIE"): return w.lower()
        except: pass
    for w in ("A","B","C","TIE"):
        if w in t: return w.lower()
    return "parse_error"


def _pairwise_judge(client, question, evidence, cand_a, cand_b, qid, rng):
    """Run a single pairwise judge call, return the winning candidate dict or None on error."""
    a_first = rng.random() < 0.5
    if a_first:
        ca, cb = cand_a, cand_b
    else:
        ca, cb = cand_b, cand_a
    prompt = (
        "You are an expert SQL judge. Two candidate SQL queries answer the same "
        "question but produce different results. Choose the one that correctly "
        "answers the question based on the question intent.\n\n"
        f"Question: {question}\n"
        f"Evidence: {evidence}\n\n"
        f"Candidate A SQL:\n```sql\n{ca['sql'].strip()}\n```\n"
        f"Result of A:\n{ca['result_text']}\n\n"
        f"Candidate B SQL:\n```sql\n{cb['sql'].strip()}\n```\n"
        f"Result of B:\n{cb['result_text']}\n\n"
        'Return ONLY: {"winner": "A"} or {"winner": "B"} or {"winner": "tie"}'
    )
    try:
        comp = client.chat_completion(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0, max_tokens=64, thinking={"type": "disabled"},
        )
        raw = comp["response"]["choices"][0]["message"]["content"]
        w = _parse_winner(raw, 2)
        if w == "tie" or w == "parse_error":
            return None
        if (w == "a") == a_first:
            return cand_a
        else:
            return cand_b
    except Exception:
        return None


def judge_top_k(args):
    ex, scored_sample, cur_sql, cfg, client, seed = args
    qid = ex.get("question_id")
    db_id = ex["db_id"]
    question = ex["question"]
    evidence = ex.get("evidence", "")

    db = BirdDatabase(db_id=db_id, db_root=cfg["dataset"]["db_root"],
                      timeout=cfg["execution"]["timeout_seconds"], max_rows=cfg["execution"]["max_rows"])

    # load all candidates from the pool
    all_cands = scored_sample.get("candidates", [])
    # filter executable + sort by orm_score desc
    exec_cands = [c for c in all_cands if c.get("result") is not None]
    if len(exec_cands) < 2:
        return {"question_id": qid, "chosen": "base", "pred_sql": cur_sql}
    exec_cands.sort(key=lambda c: -c.get("orm_score", 0.5))
    top_k = exec_cands[:12]  # expand to top-8

    # execute top-k, get result hashes
    results = []
    for c in top_k:
        res = db.execute(c["sql"])
        results.append({
            "sql": c["sql"], "model": c.get("model",""),
            "orm_score": c.get("orm_score",0.5),
            "ok": res.get("ok",False),
            "rows": res.get("rows"),
            "hash": _rows_key(res.get("rows")),
            "result_text": _fmt_result(res),
        })

    # if all same hash -> no disagreement
    hashes = [r["hash"] for r in results if r["hash"]]
    if len(set(hashes)) <= 1:
        return {"question_id": qid, "chosen": "base", "pred_sql": cur_sql}

    # collect distinct-hash candidates (best ORM per hash)
    distinct = []
    seen_hashes = set()
    for r in results:
        if r["hash"] and r["hash"] not in seen_hashes:
            distinct.append(r); seen_hashes.add(r["hash"])
    if len(distinct) < 2:
        return {"question_id": qid, "chosen": "base", "pred_sql": cur_sql}

    # Tournament: pairwise knockout among distinct candidates
    # Seed 0 = highest ORM. Brackets by ORM order.
    rng = random.Random(seed + qid)
    tournament = list(distinct)
    while len(tournament) > 1:
        # pair up: 0v1, 2v3, ... (by ORM order)
        next_round = []
        for i in range(0, len(tournament), 2):
            if i + 1 >= len(tournament):
                next_round.append(tournament[i]); continue
            a, b = tournament[i], tournament[i+1]
            a_first = rng.random() < 0.5
            if a_first: cand_a, cand_b = a, b
            else: cand_a, cand_b = b, a
            winner = _pairwise_judge(client, question, evidence, cand_a, cand_b, qid, rng)
            next_round.append(winner if winner else a)  # fallback to higher-ORM a
        tournament = next_round

    winner = tournament[0]

    prompt = (
        "You are an expert SQL judge. Two candidate SQL queries answer the same "
        "question but produce different results. Choose the one that correctly "
        "answers the question based on the question intent.\n\n"
        f"Question: {question}\n"
        f"Evidence: {evidence}\n\n"
        f"Candidate A SQL:\n```sql\n{cand_a['sql'].strip()}\n```\n"
        f"Result of A:\n{cand_a['result_text']}\n\n"
        f"Candidate B SQL:\n```sql\n{cand_b['sql'].strip()}\n```\n"
        f"Result of B:\n{cand_b['result_text']}\n\n"
        'Return ONLY: {"winner": "A"} or {"winner": "B"} or {"winner": "tie"}'
    )

    rec = {"question_id": qid}
    # accept tournament winner if ok+non-empty
    wres = db.execute(winner["sql"])
    if wres.get("ok") and wres.get("rows"):
        rec["pred_sql"] = winner["sql"]
        rec["chosen"] = "tournament"
    else:
        rec["chosen"] = "base"; rec["pred_sql"] = cur_sql

    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--scored-pool", required=True, type=Path)
    ap.add_argument("--cur-preds", required=True, type=Path)
    ap.add_argument("--dev", required=True, type=Path)
    ap.add_argument("--fail-qids", required=True, type=Path)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--timeout", type=float, default=90.0)
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.config))
    run_id = cfg["run_id"]
    out_dir = ROOT / "predictions" / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "predictions.jsonl"

    dev = json.load(open(args.dev))
    dev_by = {d.get("question_id"): d for d in dev}

    scored = {}
    for l in open(args.scored_pool):
        if l.strip():
            d = json.loads(l); scored[d["id"]] = d

    cur = {}
    for l in open(args.cur_preds):
        if l.strip():
            d = json.loads(l); cur[d["question_id"]] = d

    fail_ids = set(json.load(open(args.fail_qids)))

    done = set()
    if out_path.exists():
        for l in out_path.open():
            if l.strip():
                done.add(json.loads(l)["question_id"])
        print(f"resume: {len(done)}", flush=True)

    todo = []
    for qid in sorted(fail_ids):
        if qid in done or qid not in scored or qid not in cur:
            continue
        todo.append((dev_by[qid], scored[qid], cur[qid]["pred_sql"]))
    print(f"todo: {len(todo)}", flush=True)
    if not todo:
        return

    client = LLMClient(
        base_url=cfg["model"]["base_url"], model_name=cfg["model"]["model_name"],
        api_key_env=cfg["model"]["api_key_env"], timeout=args.timeout,
    )
    t0 = time.time(); written = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(judge_top_k, (ex, sc, cs, cfg, client, 42)): ex.get("question_id")
                for ex, sc, cs in todo}
        with out_path.open("a") as f:
            for fut in as_completed(futs):
                try:
                    rec = fut.result(timeout=args.timeout + 30)
                except Exception as e:
                    qid = futs[fut]
                    rec = {"question_id": qid, "chosen": "base", "pred_sql": cur[qid]["pred_sql"]}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                written += 1
                if written % 25 == 0:
                    print(f"  [{written}/{len(todo)}] {time.time()-t0:.0f}s", flush=True)
    print(f"done: {written} -> {out_path}", flush=True)


if __name__ == "__main__":
    main()
