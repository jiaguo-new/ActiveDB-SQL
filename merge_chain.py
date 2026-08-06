#!/usr/bin/env python3
"""Merge fail-only predictions onto a full 1534-line chain.

Usage:
  python merge_chain.py --base full_chain_prev.jsonl --overlay stage_fail.jsonl --output full_chain_next.jsonl

For each question in the overlay, replace the pred_sql in the base.
Questions not in the overlay keep their base pred_sql.
"""
import json, argparse
from pathlib import Path

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, type=Path, help="Previous full 1534-line predictions")
    ap.add_argument("--overlay", required=True, type=Path, help="Fail-only predictions from current stage")
    ap.add_argument("--output", required=True, type=Path, help="Output merged full predictions")
    args = ap.parse_args()

    base = {}
    order = []
    for l in args.base.open():
        if l.strip():
            d = json.loads(l)
            qid = d.get("question_id", d.get("id"))
            base[qid] = d
            order.append(qid)

    overlay = {}
    for l in args.overlay.open():
        if l.strip():
            d = json.loads(l)
            qid = d.get("question_id", d.get("id"))
            overlay[qid] = d

    n_replaced = 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w") as f:
        for qid in order:
            rec = base[qid]
            if qid in overlay:
                rec["pred_sql"] = overlay[qid]["pred_sql"]
                n_replaced += 1
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"Merged: {len(order)} base, {len(overlay)} overlay, {n_replaced} replaced -> {args.output}")


if __name__ == "__main__":
    main()
