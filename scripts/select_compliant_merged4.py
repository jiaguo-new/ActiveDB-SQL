#!/usr/bin/env python3
"""Select SQL from the merged4 candidate pool using ORM-v2 band rule +
result-hash tie-break.

All candidates are from train-finetuned models. This produces a fully-compliant
system: generator = train-finetuned models, selector = ORM v2 (train-only
training), no dev gold anywhere.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def _norm_cell(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 3)
    return str(v).strip().lower()


def _rows_key(rows):
    if rows is None:
        return None
    try:
        h = set()
        for row in rows:
            h.add(hashlib.md5(json.dumps(tuple(_norm_cell(v) for v in row), ensure_ascii=False).encode()).hexdigest())
        return hashlib.md5(",".join(sorted(h)).encode()).hexdigest()
    except Exception:
        return None


def select(sample, band=0.1):
    cands = sample["candidates"]
    pool = [i for i, c in enumerate(cands) if c.get("result") is not None]
    if not pool:
        bi = 0
    else:
        keys = [_rows_key(c.get("result")) for c in cands]
        cnt = {}
        for k in keys:
            if k:
                cnt[k] = cnt.get(k, 0) + 1
        hc = [cnt.get(k, 0) if k else 0 for k in keys]
        mx = max(cands[i].get("orm_score", 0.5) for i in pool)
        near = [i for i in pool if cands[i].get("orm_score", 0.5) >= mx - band]
        bi = max(near, key=lambda i: (hc[i], cands[i].get("orm_score", 0.5)))
    return cands[bi]["sql"], cands[bi].get("model"), cands[bi].get("orm_score", 0.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scored", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--band", type=float, default=0.1)
    ap.add_argument("--dev", required=True, type=Path, help="dev.json to enforce order")
    args = ap.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)

    by_qid = {}
    for line in args.scored.open():
        if not line.strip():
            continue
        d = json.loads(line)
        sql, model, score = select(d, args.band)
        by_qid[d["id"]] = sql
    dev = json.load(open(args.dev))
    n = 0
    with args.output.open("w", encoding="utf-8") as f:
        for i, ex in enumerate(dev):
            qid = ex.get("question_id", i)
            f.write(json.dumps({
                "question_id": qid, "db_id": ex["db_id"], "question": ex.get("question", ""),
                "pred_sql": by_qid.get(qid, ""),
            }, ensure_ascii=False) + "\n")
            n += 1
    print(f"selected {n} from compliant merged4 pool (dev-ordered) -> {args.output}")


if __name__ == "__main__":
    main()
