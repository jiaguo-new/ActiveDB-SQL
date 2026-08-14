"""ORM band selection plugin: pick best candidate from scored pool."""
from __future__ import annotations
import hashlib, json
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

def select(sample, band=0.05):
    cands = sample.get("candidates", [])
    pool = [i for i, c in enumerate(cands) if c.get("result") is not None]
    if not pool: return cands[0]["sql"] if cands else "", "", 0.5
    keys = [_rows_key(c.get("result")) for c in cands]
    cnt = {}
    for k in keys:
        if k: cnt[k] = cnt.get(k, 0) + 1
    hc = [cnt.get(k, 0) if k else 0 for k in keys]
    mx = max(cands[i].get("orm_score", 0.5) for i in pool)
    near = [i for i in pool if cands[i].get("orm_score", 0.5) >= mx - band]
    bi = max(near, key=lambda i: (hc[i], cands[i].get("orm_score", 0.5)))
    return cands[bi]["sql"], cands[bi].get("model", ""), cands[bi].get("orm_score", 0.5)


def create_plugin(config: dict, ctx) -> callable:
    pool_path = Path(config.get("pool", "runs/merged4model_n4_clean_scored_20260805.jsonl"))
    band = config.get("band", 0.05)
    dev_path = ctx.get("dev_path", "data/dev.json")
    root = ctx.get("root", Path("."))

    # Resolve relative to root
    if not pool_path.is_absolute():
        pool_path = root / pool_path
    if isinstance(dev_path, str) and not Path(dev_path).is_absolute():
        dev_path = root / dev_path

    # Load pool
    pool_data = {}
    for line in open(pool_path):
        if line.strip():
            d = json.loads(line)
            pool_data[d["id"]] = d

    dev = json.load(open(dev_path))

    def plugin_fn(q, ctx):
        sample = pool_data.get(q.question_id, {})
        sql, model, score = select(sample, band)
        q.candidates = sample.get("candidates", [])
        return sql

    # This plugin needs to process ALL questions (not just failures)
    # We handle this by running it specially in the select stage
    return plugin_fn


# Special: select stage processes all questions
def run_select_all(registry, questions, ctx, config):
    """Run ORM selection for all questions (not just failures)."""
    pool_path = Path(config.get("pool", "runs/merged4model_n4_clean_scored_20260805.jsonl"))
    band = config.get("band", 0.05)
    dev_path = ctx.get("dev_path", "data/dev.json")
    root = ctx.get("root", Path("."))

    if not pool_path.is_absolute():
        pool_path = root / pool_path
    if isinstance(dev_path, str) and not Path(dev_path).is_absolute():
        dev_path = root / dev_path

    pool_data = {}
    for line in open(pool_path):
        if line.strip():
            d = json.loads(line)
            pool_data[d["id"]] = d

    for q in questions:
        sample = pool_data.get(q.question_id, {})
        sql, model, score = select(sample, band)
        q.candidates = sample.get("candidates", [])
        q.pred_sql = sql

    return questions
