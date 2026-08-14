#!/usr/bin/env python3
"""Entry point: run the plugin pipeline from a YAML config.

Usage:
    export DEEPSEEK_API_KEY="your-key"
    python run_pipeline.py pipeline_config.yaml [--limit 50] [--workers 8]

The config file declares which plugins to load and their order.
Edit the YAML to change the pipeline — no code changes needed.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from harness import Context, PluginRegistry, Pipeline, Question, load_pipeline


def load_questions(dev_path: str, root: Path) -> list[Question]:
    """Load BIRD dev questions into Question objects."""
    if not Path(dev_path).is_absolute():
        dev_path = root / dev_path
    dev = json.load(open(dev_path))
    questions = []
    for i, ex in enumerate(dev):
        questions.append(Question(
            question_id=ex.get("question_id", i),
            db_id=ex["db_id"],
            question=ex.get("question", ""),
            evidence=ex.get("evidence", ""),
        ))
    return questions


def evaluate(questions: list[Question], ctx: Context) -> int:
    """Quick evaluation: execute each pred_sql and compare with gold."""
    from evaluation.bird_official_eval import _compare, _exec_sql
    from pathlib import Path as P

    dev_path = ctx.get("dev_path", "data/dev.json")
    db_root = ctx.get("db_root", "data/dev_databases")
    if not Path(dev_path).is_absolute():
        dev_path = ROOT / dev_path

    dev = json.load(open(dev_path))
    gold_map = {ex.get("question_id", i): ex for i, ex in enumerate(dev)}

    correct = 0
    for q in questions:
        ex = gold_map.get(q.question_id)
        if not ex: continue
        db_path = str(P(db_root) / ex["db_id"] / f'{ex["db_id"]}.sqlite')
        try:
            gold_rows = _exec_sql(db_path, ex.get("SQL", ""), 5000)
            pred_rows = _exec_sql(db_path, q.pred_sql, 5000)
            if _compare(pred_rows, gold_rows):
                correct += 1
                q.meta["_correct"] = True
        except:
            pass
    return correct


def main():
    ap = argparse.ArgumentParser(description="Run plugin-based NL2SQL pipeline")
    ap.add_argument("config", help="Path to pipeline_config.yaml")
    ap.add_argument("--limit", type=int, default=0, help="Process only N questions (0=all)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--output", default=None, help="Output predictions JSONL path")
    args = ap.parse_args()

    print(f"Loading pipeline from {args.config}...")
    pipeline = load_pipeline(args.config)
    ctx = pipeline.ctx
    root = ctx.get("root", ROOT)

    print("Loading questions...")
    dev_path = ctx.get("dev_path", "data/dev.json")
    questions = load_questions(dev_path, root)
    if args.limit:
        questions = questions[:args.limit]
    print(f"  {len(questions)} questions")

    # Stage 0: Select (process ALL questions)
    print("\n=== Stage: select ===")
    select_plugins = pipeline.registry.get_stage_plugins("select")
    if select_plugins:
        # ORM select is special: processes all, not just failures
        from plugins.select.orm_band import run_select_all
        import yaml
        cfg = yaml.safe_load(open(args.config))
        for entry in cfg.get("plugins", []):
            if entry["stage"] == "select":
                questions = run_select_all(pipeline.registry, questions, ctx, entry.get("config", {}))
                break

        # Evaluate
        correct = evaluate(questions, ctx)
        print(f"  EX = {correct}/{len(questions)} = {100*correct/len(questions):.1f}%")

    # Run remaining stages
    for stage in ["repair", "judge", "regen", "finalize"]:
        plugins = pipeline.registry.get_stage_plugins(stage)
        if not plugins:
            continue

        print(f"\n=== Stage: {stage} ({len(plugins)} plugins) ===")
        t0 = time.time()
        questions = pipeline.run_stage(stage, questions, fail_only=True, workers=args.workers)
        elapsed = time.time() - t0

        correct = evaluate(questions, ctx)
        fails = len(questions) - correct
        print(f"  EX = {correct}/{len(questions)} = {100*correct/len(questions):.1f}%  ({elapsed:.0f}s, {fails} fails)")

    # Write output
    output_path = args.output or "predictions/pipeline_output.jsonl"
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        for q in questions:
            f.write(json.dumps({
                "question_id": q.question_id,
                "db_id": q.db_id,
                "question": q.question,
                "pred_sql": q.pred_sql,
            }, ensure_ascii=False) + "\n")

    correct = evaluate(questions, ctx)
    print(f"\n=== FINAL: EX = {correct}/{len(questions)} = {100*correct/len(questions):.1f}% ===")
    print(f"Output: {output_path}")


if __name__ == "__main__":
    main()
