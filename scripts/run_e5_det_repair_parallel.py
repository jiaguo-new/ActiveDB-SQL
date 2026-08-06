#!/usr/bin/env python3
"""Runner for deterministic repair (COUNT(*) + over-JOIN pruning) on failures."""
from __future__ import annotations
import argparse, json, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from tools.db_utils import BirdDatabase
sys.path.insert(0, str(ROOT / "agents"))
from e5_deterministic_repair import deterministic_repair

def process_one(args):
    ex, cur_pred, cfg = args
    qid = ex.get("question_id"); db_id = ex["db_id"]
    question = ex["question"]; sql = cur_pred.get("pred_sql", "")
    db = BirdDatabase(db_id=db_id, db_root=cfg["dataset"]["db_root"],
                      timeout=cfg["execution"]["timeout_seconds"], max_rows=cfg["execution"]["max_rows"])
    rec = {"question_id": qid, "db_id": db_id, "question": question}
    if not sql.strip():
        rec["pred_sql"] = ""; return rec

    # apply deterministic repair
    repaired_sql, repairs = deterministic_repair(sql, question, db)

    # pick best: repaired must execute ok+non-empty to be accepted
    orig_res = db.execute(sql)
    orig_ok = orig_res.get("ok") and orig_res.get("rows")
    if repairs:
        rep_res = db.execute(repaired_sql)
        rep_ok = rep_res.get("ok") and rep_res.get("rows")
        if rep_ok:
            rec["pred_sql"] = repaired_sql; rec["chosen"] = "det_repair"
        else:
            rec["pred_sql"] = sql; rec["chosen"] = "base"
    else:
        rec["pred_sql"] = sql; rec["chosen"] = "base"
    rec["repairs"] = repairs
    return rec

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--base-preds", required=True, type=Path)
    ap.add_argument("--dev", required=True, type=Path)
    ap.add_argument("--fail-qids", required=True, type=Path)
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    cfg = yaml.safe_load(open(args.config)); run_id = cfg["run_id"]
    out_dir = ROOT / "predictions" / run_id; out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "predictions.jsonl"
    dev = json.load(open(args.dev)); dev_by = {d.get("question_id"): d for d in dev}
    base = {}
    for l in open(args.base_preds):
        if l.strip(): d = json.loads(l); base[d["question_id"]] = d
    fail_ids = set(json.load(open(args.fail_qids)))
    done = set()
    if out_path.exists():
        for l in out_path.open():
            if l.strip(): done.add(json.loads(l)["question_id"])
    todo = [(dev_by[q], base[q]) for q in sorted(fail_ids) if q not in done and q in base]
    print(f"todo: {len(todo)}", flush=True)
    if not todo: return
    t0 = time.time(); written = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(process_one, (ex, bp, cfg)): ex.get("question_id") for ex, bp in todo}
        with out_path.open("a") as f:
            for fut in as_completed(futs):
                try: rec = fut.result(timeout=60)
                except Exception as e:
                    qid = futs[fut]; rec = {"question_id": qid, "pred_sql": base[qid]["pred_sql"]}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n"); f.flush()
                written += 1
                if written % 50 == 0: print(f"  [{written}/{len(todo)}] {time.time()-t0:.0f}s", flush=True)
    print(f"done: {written} -> {out_path}", flush=True)

if __name__ == "__main__": main()
