"""Parallel BIRD evaluator that keeps the same subprocess-per-query timeout."""

from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
from typing import Any

import concurrent.futures

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bird_official_eval import evaluate_one_with_timeout, QUERY_TIMEOUT, RESULT_LIMIT


def _eval_one(t):
    idx, args = t[0], t
    return idx, evaluate_one_with_timeout(args, 15)


def evaluate_predictions_fast(
    dev_path: Path | str,
    pred_path: Path | str,
    db_root: Path | str,
    output_path: Path | str,
    timeout: int = QUERY_TIMEOUT,
    limit: int = RESULT_LIMIT,
    workers: int = 8,
) -> dict[str, Any]:
    dev_path = Path(dev_path)
    pred_path = Path(pred_path)
    db_root = Path(db_root)
    output_path = Path(output_path)

    with open(dev_path, "r", encoding="utf-8") as f:
        dev = json.load(f)
    with open(pred_path, "r", encoding="utf-8") as f:
        preds = [json.loads(line) for line in f if line.strip()]

    n = min(len(dev), len(preds))
    db_base = db_root
    tasks = [(i, dev[i], preds[i].get("pred_sql", ""), db_base, limit) for i in range(n)]

    print(f"Evaluating {n} BIRD queries with subprocess timeout={timeout}s (workers={workers}) ...")
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as pool:
        results = [None] * n
        for i, res in enumerate(pool.map(_eval_one, tasks, chunksize=1)):
            idx, r = res
            results[idx] = r
            completed = i + 1
            if completed % 50 == 0 or completed == n:
                interim_ex = sum(1 for r in results[:completed] if r and r["ex"])
                interim_valid = sum(1 for r in results[:completed] if r and r["valid"])
                print(
                    f"  [{completed}/{n}] EX={interim_ex}/{completed} "
                    f"({100*interim_ex/completed:.1f}%), "
                    f"Valid={interim_valid}/{completed} ({100*interim_valid/completed:.1f}%)"
                )

    ex_count = sum(1 for r in results if r["ex"])
    em_count = sum(1 for r in results if r["em"])
    valid_count = sum(1 for r in results if r["valid"])
    join_results = [r for r in results if r["is_join"]]
    join_ex = sum(1 for r in join_results if r["ex"])

    summary = {
        "total": n,
        "em": em_count,
        "ex": ex_count,
        "valid": valid_count,
        "em_rate": 100 * em_count / n if n else 0,
        "ex_rate": 100 * ex_count / n if n else 0,
        "valid_rate": 100 * valid_count / n if n else 0,
        "join_total": len(join_results),
        "join_ex": join_ex,
        "join_ex_rate": 100 * join_ex / len(join_results) if join_results else 0,
        "per_query": results,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print("\n========== BIRD Fast Evaluation ==========")
    print(f"Total queries: {n}")
    print(f"Exact Match (EM): {em_count} / {n} = {summary['em_rate']:.2f}%")
    print(f"Execution Match (EX): {ex_count} / {n} = {summary['ex_rate']:.2f}%")
    print(f"Valid SQL Rate: {valid_count} / {n} = {summary['valid_rate']:.2f}%")
    if join_results:
        print(f"JOIN EX: {join_ex} / {len(join_results)} = {summary['join_ex_rate']:.2f}%")
    print("==========================================")
    print(f"Results written to {output_path}")
    return summary


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--dev", required=True)
    parser.add_argument("--pred", required=True)
    parser.add_argument("--db-root", required=True)
    parser.add_argument("--output", default="bird_fast_eval_results.json")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    evaluate_predictions_fast(args.dev, args.pred, args.db_root, args.output, workers=args.workers)
