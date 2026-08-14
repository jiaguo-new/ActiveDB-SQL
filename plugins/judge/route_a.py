"""Route A tournament judge: ORM top-K pairwise knockout with LLM judge."""
from __future__ import annotations
import sys, hashlib, json, random, re
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

def _parse_winner(text, n=2):
    t = text.strip().upper()
    if "{" in t:
        try:
            s, e = t.index("{"), t.rindex("}")+1
            d = json.loads(t[s:e])
            w = str(d.get("winner","")).upper()
            if w in ("A","B","C","TIE"): return w.lower()
        except: pass
    for w in ("A","B","C","TIE"):
        if w in t: return w.lower()
    return "parse_error"

def create_plugin(config: dict, ctx) -> callable:
    root = ctx.get("root", Path("."))
    sys.path.insert(0, str(root))
    from tools.db_utils import BirdDatabase

    db_root = ctx.get("db_root", "data/dev_databases")
    pool_path = Path(config.get("pool", "runs/merged4model_n8_pool_scored_20260729.jsonl"))
    if not pool_path.is_absolute():
        pool_path = root / pool_path
    top_k = config.get("top_k", 12)
    max_tokens = config.get("max_tokens", 1024)

    pool_data = {}
    for line in open(pool_path):
        if line.strip():
            d = json.loads(line)
            pool_data[d["id"]] = d

    def _judge(client, question, evidence, cand_a, cand_b, seed):
        rng = random.Random(seed)
        a_first = rng.random() < 0.5
        ca, cb = (cand_a, cand_b) if a_first else (cand_b, cand_a)
        prompt = (
            "You are an expert SQL judge. Two candidate SQL queries answer the same "
            "question but produce different results. Analyze each carefully, then choose "
            "the one that correctly answers the question.\n\n"
            f"Question: {question}\nEvidence: {evidence}\n\n"
            f"Candidate A SQL:\n```sql\n{ca['sql'].strip()}\n```\nResult of A:\n{ca['result_text']}\n\n"
            f"Candidate B SQL:\n```sql\n{cb['sql'].strip()}\n```\nResult of B:\n{cb['result_text']}\n\n"
            "Analysis checklist:\n- Does it answer the EXACT question?\n- Are JOINs correct?\n"
            "- Is WHERE correct?\n- Is aggregation correct?\n- Does the result make sense?\n\n"
            'After analysis, output: {"winner": "A"} or {"winner": "B"} or {"winner": "tie"}'
        )
        try:
            comp = client.chat_completion(
                messages=[
                    {"role":"system","content":"You are an expert SQL evaluator. Analyze step by step."},
                    {"role":"user","content":prompt},
                ],
                temperature=0.0, max_tokens=max_tokens,
            )
            raw = comp["response"]["choices"][0]["message"]["content"]
            w = _parse_winner(raw, 2)
            if w in ("tie","parse_error"): return None
            return cand_a if (w=="a")==a_first else cand_b
        except: return None

    def plugin_fn(q, ctx):
        if not q.pred_sql.strip(): return q.pred_sql
        sample = pool_data.get(q.question_id, {})
        cands = sample.get("candidates", [])
        if len(cands) < 2: return q.pred_sql

        db = BirdDatabase(db_id=q.db_id, db_root=db_root, timeout=30, max_rows=100)
        exec_cands = sorted([c for c in cands if c.get("result") is not None],
                           key=lambda c: -c.get("orm_score", 0.5))[:top_k]

        results = []
        for c in exec_cands:
            res = db.execute(c["sql"])
            results.append({"sql": c["sql"], "orm_score": c.get("orm_score", 0.5),
                           "hash": _rows_key(res.get("rows")), "result_text": _fmt(res),
                           "ok": res.get("ok", False)})

        exec_r = [r for r in results if r["ok"] and r["hash"]]
        if len(exec_r) < 2: return q.pred_sql
        hashes = [r["hash"] for r in exec_r]
        if len(set(hashes)) <= 1: return q.pred_sql

        distinct = []; seen = set()
        for r in exec_r:
            if r["hash"] not in seen:
                distinct.append(r); seen.add(r["hash"])

        client = ctx.get("llm")
        if not client: return q.pred_sql

        # Tournament
        rng = random.Random(42 + q.question_id)
        tournament = list(distinct[:4])
        while len(tournament) > 1:
            nxt = []
            for i in range(0, len(tournament), 2):
                if i+1 >= len(tournament): nxt.append(tournament[i]); continue
                winner = _judge(client, q.question, q.evidence, tournament[i], tournament[i+1], rng.randint(0,99999))
                nxt.append(winner if winner else tournament[i])
            tournament = nxt

        winner = tournament[0]
        res = db.execute(winner["sql"])
        if res.get("ok") and res.get("rows"):
            return winner["sql"]
        return q.pred_sql

    return plugin_fn
