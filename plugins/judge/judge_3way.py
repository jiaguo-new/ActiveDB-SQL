"""3-way judge: present top-3 distinct candidates simultaneously for better selection.

Complements route_a pairwise tournament: for questions where pairwise
elimination may propagate early errors, showing all candidates at once
lets the model compare holistically.
"""
from __future__ import annotations
import sys, hashlib, json, re
from pathlib import Path


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


def create_plugin(config: dict, ctx) -> callable:
    root = ctx.get("root", Path("."))
    sys.path.insert(0, str(root))
    from tools.db_utils import BirdDatabase

    db_root = ctx.get("db_root", "data/dev_databases")
    pool_path = Path(config.get("pool", "runs/merged4model_n8_pool_scored_20260729.jsonl"))
    if not pool_path.is_absolute():
        pool_path = root / pool_path
    top_k = config.get("top_k", 8)

    pool_data = {}
    for line in open(pool_path):
        if line.strip():
            d = json.loads(line)
            pool_data[d["id"]] = d

    def plugin_fn(q, ctx):
        if not q.pred_sql.strip(): return q.pred_sql
        sample = pool_data.get(q.question_id, {})
        cands = sample.get("candidates", [])
        if len(cands) < 2: return q.pred_sql

        db = BirdDatabase(db_id=q.db_id, db_root=db_root, timeout=30, max_rows=100)
        exec_cands = sorted([c for c in cands if c.get("result") is not None],
                           key=lambda c: -c.get("orm_score", 0.5))[:top_k]

        detail = []
        for c in exec_cands:
            res = db.execute(c["sql"])
            detail.append({
                "sql": c["sql"], "orm_score": c.get("orm_score", 0.5),
                "ok": res.get("ok", False), "hash": _rows_key(res.get("rows")),
                "result_text": _fmt(res),
            })

        exec_d = [d for d in detail if d["ok"] and d["hash"]]
        if len(exec_d) < 2: return q.pred_sql
        hashes = [d["hash"] for d in exec_d]
        if len(set(hashes)) <= 1: return q.pred_sql

        # top-3 distinct results
        distinct = []; seen = set()
        for d in exec_d:
            if d["hash"] not in seen:
                distinct.append(d); seen.add(d["hash"])
            if len(distinct) >= 3: break
        if len(distinct) < 2: return q.pred_sql

        # Build 3-way prompt
        blocks = []
        for i, d in enumerate(distinct):
            label = chr(65 + i)
            blocks.append(
                f"Candidate {label}:\n```sql\n{d['sql'].strip()}\n```\nResult: {d['result_text']}"
            )

        prompt = (
            "You are an expert SQL evaluator. Three candidate SQL queries answer the same "
            "question but may produce different results. Analyze each carefully and pick the BEST one.\n\n"
            f"Database: {q.db_id}\n\nQuestion: {q.question}\nEvidence: {q.evidence}\n\n"
            + "\n\n".join(blocks) + "\n\n"
            "Analysis checklist:\n"
            "1. Does it answer the EXACT question (count vs list vs avg)?\n"
            "2. Are the JOINs correct (right tables, right ON conditions)?\n"
            "3. Is the WHERE filter correct?\n"
            "4. Is the aggregation correct?\n"
            "5. Does the result make sense?\n\n"
            "After analyzing all candidates, output your final answer:\n"
            "WINNER: A  (or B, or C)"
        )

        client = ctx.get("llm")
        if not client: return q.pred_sql

        try:
            comp = client.chat_completion(
                messages=[
                    {"role": "system", "content": "You are an expert SQL evaluator. Analyze step by step, then give a clear final verdict."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0, max_tokens=2048,
            )
            raw = comp["response"]["choices"][0]["message"]["content"]

            # Parse WINNER: A/B/C (with fallbacks)
            winner_label = None
            for m in re.finditer(r"WINNER:\s*([ABC])", raw, re.I):
                winner_label = m.group(1).upper()
            if not winner_label:
                tail = raw[-300:].upper()
                for w in ["A", "B", "C"]:
                    if w in tail:
                        winner_label = w; break

            if winner_label and winner_label in "ABC":
                idx = ord(winner_label) - 65
                if idx < len(distinct):
                    winner = distinct[idx]
                    # consensus: prefer higher ORM if same result
                    for d in distinct:
                        if d is not winner and d["hash"] == winner["hash"] and d["orm_score"] > winner["orm_score"]:
                            winner = d
                    res = db.execute(winner["sql"])
                    if res.get("ok") and res.get("rows"):
                        return winner["sql"]
        except Exception:
            pass
        return q.pred_sql

    return plugin_fn
