#!/usr/bin/env python3
"""Continuous-learning loop: auto-discover better plugin configurations.

Usage:
    export DEEPSEEK_API_KEY="your-key"
    python run_discovery.py configs_exp/exp_f_full_12.yaml \
        --trials 8 --subset 400 --workers 8

Flow per trial:
    1. Mutate the champion config (knob / reorder / drop / reinsert)
    2. Compliance gate (tune plugins must point at train only; backbone intact)
    3. Run the pipeline on a fixed random subset of questions
    4. Score with the isolated evaluator (the ONLY place gold is read)
    5. Append to ledger; accept mutant as new champion if EX improves
After the budget: champion config is written out for a full run.

Compliance notes:
    - Aggregate-level selection on dev EX (standard dev usage). No per-sample
      tuning: plugins receive questions only, never gold.
    - Tune plugins, when present in a config, are executed ONLY with the train
      split iterator; a tune config referencing dev/test is rejected.
"""
from __future__ import annotations

import argparse
import copy
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import yaml

from harness.discovery import (
    DiscoveryEngine, config_hash, append_ledger, load_ledger,
)
from harness import Question


def build_questions(dev_path: Path, qids: set[int] | None = None) -> list[Question]:
    dev = json.load(open(dev_path))
    out = []
    for i, ex in enumerate(dev):
        qid = ex.get("question_id", i)
        if qids is not None and qid not in qids:
            continue
        out.append(Question(
            question_id=qid, db_id=ex["db_id"],
            question=ex.get("question", ""), evidence=ex.get("evidence", ""),
        ))
    return out


def run_tune_stage(cfg: dict, ctx) -> dict:
    """Run tune plugins with TRAIN data only. Returns artifacts."""
    tune_entries = [e for e in cfg.get("plugins", []) if e["stage"] == "tune"]
    if not tune_entries:
        return {}
    artifacts: dict = {}
    import importlib
    train_path = cfg.get("tuning", {}).get(
        "train_source", "experiments/generator_oof_v1/bird_train_official.json"
    )
    # Resolve train path (search common locations)
    candidates = [
        ROOT / train_path,
        Path("/home/dameng/Sql+text2sql/experiments/generator_oof_v1/bird_train_official.json"),
        ROOT.parent / train_path,
    ]
    tp = next((p for p in candidates if p.exists()), None)
    if tp is None:
        print("  [tune] train source not found, skipping tune stage")
        return {}

    def train_iter():
        for ex in json.load(open(tp)):
            yield ex

    for entry in tune_entries:
        mod = importlib.import_module(entry["module"])
        if not hasattr(mod, "create_tuner"):
            print(f"  [tune] {entry['name']}: no create_tuner, skip")
            continue
        t0 = time.time()
        tuner = mod.create_tuner(entry.get("config", {}), ctx)
        result = tuner(train_iter())
        # merge artifacts (tuners return {artifact_key: value})
        for k, v in result.items():
            if v is not None:
                artifacts[k] = v
        print(f"  [tune] {entry['name']}: {time.time()-t0:.0f}s -> "
              f"{ {k: (len(v) if isinstance(v, list) else v) for k, v in result.items() if not isinstance(v, (int, float)) or k.startswith('n_')} }")
    return artifacts


def run_trial(cfg: dict, questions: list[Question], ctx, workers: int,
              artifacts: dict) -> tuple[int, int]:
    """Run select->repair->judge->regen->finalize on the subset. Returns (ex, total)."""
    # Inject tune artifacts into ctx for plugins that consume them
    for k, v in artifacts.items():
        ctx.provide(k, v)

    questions = copy.deepcopy(questions)

    # select stage (all questions)
    from plugins.select.orm_band import run_select_all
    for e in cfg.get("plugins", []):
        if e["stage"] == "select":
            questions = run_select_all(None, questions, ctx, e.get("config", {}))
            break

    # remaining stages on failures only
    from harness import Pipeline, PluginRegistry
    registry = PluginRegistry()
    import importlib
    for e in cfg.get("plugins", []):
        if e["stage"] in ("select", "tune"):
            continue
        mod = importlib.import_module(e["module"])
        if hasattr(mod, "create_plugin"):
            registry.register(e["stage"], e["name"], mod.create_plugin(e.get("config", {}), ctx))
    pipeline = Pipeline(registry, ctx)

    # Mark correct after select for fail_only semantics
    from run_pipeline import evaluate
    evaluate(questions, ctx)

    for stage in ["repair", "judge", "regen", "finalize"]:
        if not registry.get_stage_plugins(stage):
            continue
        questions = pipeline.run_stage(stage, questions, fail_only=True, workers=workers)

    ex = evaluate(questions, ctx)
    return ex, len(questions)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base_config", help="Base pipeline YAML (the champion seed)")
    ap.add_argument("--trials", type=int, default=8)
    ap.add_argument("--subset", type=int, default=400,
                    help="Questions per trial (fixed random subset, seeded)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--ledger", default="trials.jsonl")
    ap.add_argument("--champion-out", default="configs_exp/auto_champion.yaml")
    ap.add_argument("--champion-full-out", default=None,
                    help="If set, also write full-run predictions of champion")
    args = ap.parse_args()

    engine = DiscoveryEngine(Path(args.base_config), seed=args.seed)

    # Fixed evaluation subset (same across trials -> fair comparison)
    dev_path = ROOT / engine.base.get("data", {}).get("dev", "data/dev.json")
    all_qids = [ex.get("question_id", i) for i, ex in enumerate(json.load(open(dev_path)))]
    rng = random.Random(args.seed)
    subset_qids = set(rng.sample(all_qids, min(args.subset, len(all_qids))))
    questions = build_questions(dev_path, subset_qids)
    print(f"Discovery: {args.trials} trials on fixed subset of {len(questions)} questions")
    print(f"Ledger: {args.ledger}")

    # Shared ctx (llm client etc.) built from champion's global config
    from harness import load_pipeline
    base_pipeline = load_pipeline(args.base_config)
    ctx = base_pipeline.ctx

    # Baseline trial (trial 0 = unmutated champion)
    for t in range(args.trials + 1):
        if t == 0:
            cfg, mutation, parent = copy.deepcopy(engine.base), "baseline", None
        else:
            cfg, mutation = engine.mutate(engine.champion)
            parent = config_hash(engine.champion)

        issues = DiscoveryEngine.compliance_check(cfg)
        if issues:
            print(f"[trial {t}] REJECTED by compliance gate: {issues}")
            continue

        # Tune stage (train-only); artifacts feed inference plugins
        try:
            artifacts = run_tune_stage(cfg, ctx)
        except Exception as e:
            print(f"[trial {t}] tune stage error: {e}")
            artifacts = {}

        t0 = time.time()
        try:
            ex, total = run_trial(cfg, questions, ctx, args.workers, artifacts)
        except Exception as e:
            print(f"[trial {t}] pipeline error: {str(e)[:120]}")
            continue
        dur = time.time() - t0

        trial = engine.record(cfg, ex, total, dur, mutation, parent)
        append_ledger(Path(args.ledger), trial)
        star = "*" if trial.accepted else " "
        print(f"[trial {t}] {star} EX {ex}/{total} ({100*ex/total:.1f}%) "
              f"{dur:.0f}s | {mutation} | hash={trial.config_hash}"
              + ("  -> new champion" if trial.accepted else ""))

    # Write champion
    Path(args.champion_out).parent.mkdir(parents=True, exist_ok=True)
    yaml.safe_dump(engine.champion, open(args.champion_out, "w"), sort_keys=False)
    print(f"\nChampion: EX {engine.champion_ex} on subset | config -> {args.champion_out}")
    print(f"Ledger now has {len(load_ledger(Path(args.ledger)))} trials")


if __name__ == "__main__":
    main()
