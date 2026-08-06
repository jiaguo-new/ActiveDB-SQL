"""Official-style BIRD evaluation that reads our predictions.jsonl.

Uses subprocess timeout and set-based result comparison (value normalization
with float rounding) to match the upstream BIRD evaluation semantics.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import sqlite3
from pathlib import Path
from typing import Any

QUERY_TIMEOUT = int(os.environ.get("BIRD_QUERY_TIMEOUT", 12))
RESULT_LIMIT = 5000


def _normalize_cell(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return round(float(v), 3)
    return str(v).strip().lower()


def _rows_key(rows) -> str | None:
    """Deterministic set hash for a result set, matching _compare semantics.

    _compare treats result sets as unordered sets of normalized rows.  This
    function serializes each normalized row as JSON (which is unambiguous for the
    primitive values returned by SQLite) and hashes the sorted set of row
    digests.  It is therefore order- and multiplicity-independent and should agree
    with _compare for all practical purposes (MD5 collisions excluded).
    """
    if rows is None:
        return None
    try:
        row_hashes = set()
        for row in rows:
            normalized = tuple(_normalize_cell(v) for v in row)
            row_hashes.add(hashlib.md5(json.dumps(normalized, ensure_ascii=False).encode("utf-8")).hexdigest())
        return hashlib.md5(",".join(sorted(row_hashes)).encode("utf-8")).hexdigest()
    except Exception:
        return None


def _compare(pred_rows, gold_rows) -> bool:
    if pred_rows is None and gold_rows is None:
        return True
    if pred_rows is None or gold_rows is None:
        return False
    try:
        pred_set = {tuple(_normalize_cell(v) for v in row) for row in pred_rows}
        gold_set = {tuple(_normalize_cell(v) for v in row) for row in gold_rows}
        return pred_set == gold_set
    except Exception:
        return False


def _exec_sql(db_path: str, sql: str, limit: int):
    if not sql or not sql.strip():
        raise ValueError("empty_sql")
    uri = f"file:{db_path}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=5) as conn:
        cur = conn.execute(sql)
        return cur.fetchmany(limit)


def _worker_main(args, queue):
    idx, item, pred_sql, db_base, limit = args
    db_id = item["db_id"]
    db_path = str(db_base / db_id / f"{db_id}.sqlite")
    gold_sql = item.get("SQL") or item.get("query", "")
    is_join = "join" in gold_sql.lower()

    try:
        gold_rows = _exec_sql(db_path, gold_sql, limit)
    except Exception as e:
        queue.put(
            {
                "idx": idx,
                "ex": False,
                "em": False,
                "valid": False,
                "gold_error": str(e),
                "pred_error": None,
                "is_join": is_join,
            }
        )
        return

    try:
        pred_rows = _exec_sql(db_path, pred_sql, limit)
    except Exception as e:
        queue.put(
            {
                "idx": idx,
                "ex": False,
                "em": False,
                "valid": False,
                "gold_error": None,
                "pred_error": str(e),
                "is_join": is_join,
            }
        )
        return

    em = (
        pred_sql.strip().lower().rstrip(";").replace("\n", " ")
        == gold_sql.strip().lower().rstrip(";").replace("\n", " ")
    )
    ex = _compare(pred_rows, gold_rows)
    queue.put(
        {
            "idx": idx,
            "ex": ex,
            "em": em,
            "valid": True,
            "gold_error": None,
            "pred_error": None,
            "is_join": is_join,
            "pred_hash": _rows_key(pred_rows),
            "gold_hash": _rows_key(gold_rows),
        }
    )


def evaluate_one_with_timeout(args, timeout: int):
    queue = multiprocessing.Queue(maxsize=0)
    p = multiprocessing.Process(target=_worker_main, args=(args, queue))
    p.start()
    p.join(timeout)
    if p.is_alive():
        p.terminate()
        p.join(2)
        if p.is_alive():
            p.kill()
            p.join()
        item = args[1]
        gold_sql = item.get("SQL") or item.get("query", "")
        return {
            "idx": args[0],
            "ex": False,
            "em": False,
            "valid": False,
            "gold_error": None,
            "pred_error": "eval_timeout",
            "is_join": "join" in gold_sql.lower(),
        }
    try:
        return queue.get(block=False)
    except Exception:
        gold_sql = args[1].get("SQL") or args[1].get("query", "")
        return {
            "idx": args[0],
            "ex": False,
            "em": False,
            "valid": False,
            "gold_error": None,
            "pred_error": "eval_no_result",
            "is_join": "join" in gold_sql.lower(),
        }


def evaluate_predictions(
    dev_path: Path | str,
    pred_path: Path | str,
    db_root: Path | str,
    output_path: Path | str,
    timeout: int = QUERY_TIMEOUT,
    limit: int = RESULT_LIMIT,
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

    print(f"Evaluating {n} BIRD queries with subprocess timeout={timeout}s ...")
    results = [None] * n
    for i, task in enumerate(tasks):
        results[i] = evaluate_one_with_timeout(task, timeout + 3)
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

    print("\n========== BIRD Official-style Evaluation ==========")
    print(f"Total queries: {n}")
    print(f"Exact Match (EM): {em_count} / {n} = {summary['em_rate']:.2f}%")
    print(f"Execution Match (EX): {ex_count} / {n} = {summary['ex_rate']:.2f}%")
    print(f"Valid SQL Rate: {valid_count} / {n} = {summary['valid_rate']:.2f}%")
    if join_results:
        print(f"JOIN EX: {join_ex} / {len(join_results)} = {summary['join_ex_rate']:.2f}%")
    print("====================================================")
    print(f"Results written to {output_path}")
    return summary


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--dev", required=True)
    parser.add_argument("--pred", required=True)
    parser.add_argument("--db-root", required=True)
    parser.add_argument("--output", default="bird_eval_results.json")
    args = parser.parse_args()
    evaluate_predictions(args.dev, args.pred, args.db_root, args.output)
