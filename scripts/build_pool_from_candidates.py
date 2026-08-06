#!/usr/bin/env python3
"""Build a candidate pool for ORM scoring from raw candidate files.

For each dev question:
  - takes the base_prompt from the existing n4 scored pool (same question);
  - collects candidates from one or more raw candidate files (pred_sql each);
  - executes each candidate (read-only, fetchmany(5)) to attach `result`;
  - assigns `model` tag per source file.

Output schema matches the scored-pool format so the ORM scorer can consume it.
This is used to build the m4n8 (32-candidate) pool for compliant selection.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def _exec(db_path: str, sql: str, limit: int = 5):
    try:
        uri = f"file:{db_path}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=5) as conn:
            return [list(r) for r in conn.execute(sql).fetchmany(limit)]
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompts", required=True, type=Path,
                    help="JSONL with id/db_id/question/prompt (e.g. existing n4 pool)")
    ap.add_argument("--candidates", nargs="+", required=True,
                    help="raw candidate files: each line {question_id,db_id,pred_sql}; "
                         "use name=path to tag model")
    ap.add_argument("--db-root", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()

    # base prompts
    meta = {}
    for l in args.prompts.open():
        if not l.strip():
            continue
        d = json.loads(l)
        meta[d["id"]] = d

    # collect candidates per source
    src_files = []
    for spec in args.candidates:
        if "=" in spec:
            name, path = spec.split("=", 1)
        else:
            name, path = Path(spec).stem, spec
        src_files.append((name, Path(path)))

    db_paths = {}
    dev = json.load(open(args.db_root.parent / "dev.json"))
    for it in dev:
        db_paths[it["db_id"]] = str(args.db_root / it["db_id"] / f"{it['db_id']}.sqlite")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with args.output.open("w", encoding="utf-8") as out:
        for qid, m in meta.items():
            dbp = db_paths.get(m["db_id"])
            cands = []
            seen = set()
            for name, path in src_files:
                # candidates files are ordered by question_id 0..N; build index
                if not hasattr(main, "_idx_cache"):
                    main._idx_cache = {}
                cache_key = str(path)
                if cache_key not in main._idx_cache:
                    idx = {}
                    for line in path.open():
                        if line.strip():
                            p = json.loads(line)
                            idx[p.get("question_id")] = p.get("pred_sql", "")
                    main._idx_cache[cache_key] = idx
                sql = main._idx_cache[cache_key].get(int(qid), "")
                if not sql or sql.strip() in seen:
                    continue
                seen.add(sql.strip())
                cands.append({"sql": sql, "model": name, "result": _exec(dbp, sql) if dbp else None})
            rec = {
                "id": m["id"], "db_id": m["db_id"], "question": m["question"],
                "prompt": m["prompt"], "candidates": cands,
                "n_candidates": len(cands),
            }
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    print(f"wrote {n} questions -> {args.output}")


if __name__ == "__main__":
    main()
