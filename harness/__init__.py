#!/usr/bin/env python3
"""Plugin framework for NL2SQL harness.

Inspired by deepseek-harness's "everything is a plugin" architecture.
Adapted to Python with minimal complexity for our use case.

Core concepts:
  - Plugin: a module with register(ctx) function
  - Context (ctx): shared service registry, keyed by capability
  - Pipeline stages: waterfall events (each stage can mutate-and-pass or short-circuit)
  - Config: YAML declaring which plugins to load and their config

Pipeline stages (waterfall):
  1. select    — candidate selection from pool (ORM band, random, etc.)
  2. repair    — SQL repair plugins (value grounding, JOIN, column, execution)
  3. judge     — candidate judging (tournament, 3-way, consensus)
  4. regen     — regeneration (deep regen, preference-guided)
  5. finalize  — post-processing (deterministic rules, leak check)

Each plugin registers handlers on one or more stages.
Stages execute in order; within a stage, plugins run in registration order.
A plugin receives (question, candidates, current_pred, ctx) and returns
the updated pred_sql.
"""
from __future__ import annotations

import importlib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


# ── Context: shared service registry ─────────────────────────────────────

class Context:
    """Shared context passed through the pipeline. Plugins register services here."""

    def __init__(self):
        self.services: dict[str, Any] = {}
        self.config: dict[str, Any] = {}
        self.metrics: dict[str, Any] = {}

    def provide(self, key: str, value: Any):
        """Register a service."""
        self.services[key] = value

    def get(self, key: str, default=None):
        return self.services.get(key, default)

    def __getattr__(self, key):
        if key.startswith("_"):
            raise AttributeError(key)
        return self.services.get(key)


# ── Question: the unit of work ────────────────────────────────────────────

@dataclass
class Question:
    question_id: int
    db_id: str
    question: str
    evidence: str = ""
    pred_sql: str = ""  # current best prediction (mutated through pipeline)
    candidates: list[dict] = field(default_factory=list)  # scored candidates from pool
    meta: dict = field(default_factory=dict)  # plugin-specific metadata


# ── Plugin: the unit of extensibility ─────────────────────────────────────

PluginFn = Callable[[Question, Context], str]
"""A plugin function: takes (question, ctx), returns updated pred_sql."""


class PluginRegistry:
    """Registry of plugins organized by pipeline stage."""

    STAGES = ["select", "repair", "judge", "regen", "finalize"]

    def __init__(self):
        self.plugins: dict[str, list[tuple[str, PluginFn]]] = {
            stage: [] for stage in self.STAGES
        }

    def register(self, stage: str, name: str, fn: PluginFn):
        """Register a plugin function on a given stage."""
        if stage not in self.STAGES:
            raise ValueError(f"Unknown stage: {stage}. Must be one of {self.STAGES}")
        self.plugins[stage].append((name, fn))

    def get_stage_plugins(self, stage: str) -> list[tuple[str, PluginFn]]:
        return self.plugins.get(stage, [])


# ── Pipeline: runs plugins in order on failing questions ──────────────────

class Pipeline:
    """Orchestrates plugin execution across stages."""

    def __init__(self, registry: PluginRegistry, ctx: Context):
        self.registry = registry
        self.ctx = ctx

    def run_stage(self, stage: str, questions: list[Question],
                  fail_only: bool = True, workers: int = 8) -> list[Question]:
        """Run all plugins on a given stage. Only processes failing questions."""
        plugins = self.registry.get_stage_plugins(stage)
        if not plugins:
            return questions

        # Determine which questions to process
        if fail_only:
            # After stage 1 (select), fail_only means questions where pred is wrong
            # For select stage, process all
            todo = questions if stage == "select" else [
                q for q in questions if not q.meta.get("_correct", False)
            ]
        else:
            todo = questions

        if not todo:
            return questions

        print(f"  [{stage}] {len(plugins)} plugins, {len(todo)} questions", flush=True)

        def process_one(q: Question) -> Question:
            for name, fn in plugins:
                try:
                    new_sql = fn(q, self.ctx)
                    if new_sql and new_sql.strip():
                        q.pred_sql = new_sql
                except Exception as e:
                    q.meta.setdefault("errors", []).append(f"{name}: {str(e)[:80]}")
            return q

        qmap = {q.question_id: q for q in questions}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(process_one, q): q.question_id for q in todo}
            for fut in as_completed(futs):
                try:
                    result = fut.result(timeout=300)
                    qmap[result.question_id] = result
                except Exception as e:
                    qid = futs[fut]
                    qmap[qid].meta.setdefault("errors", []).append(f"timeout: {str(e)[:60]}")

        return list(qmap.values())

    def run(self, questions: list[Question], workers: int = 8,
            eval_fn: Callable | None = None,
            fail_qids_path: Path | None = None) -> list[Question]:
        """Run the full pipeline. If eval_fn is provided, evaluate after each stage."""
        for stage in PluginRegistry.STAGES:
            plugins = self.registry.get_stage_plugins(stage)
            if not plugins:
                continue

            # Only process specified fail_qids if provided
            if fail_qids_path and stage != "select":
                fail_ids = set(json.load(open(fail_qids_path)))
                todo_ids = fail_ids
            else:
                todo_ids = None

            questions = self.run_stage(stage, questions, workers=workers)

            if eval_fn:
                ex_count = eval_fn(questions, self.ctx)
                print(f"  [{stage}] EX = {ex_count}/{len(questions)}", flush=True)

        return questions


# ── Loader: load plugins from config ──────────────────────────────────────

def load_pipeline(config_path: str | Path) -> Pipeline:
    """Load a pipeline from a YAML config file.

    Config format:
        plugins:
          - stage: select
            name: orm_band
            module: plugins.select.orm_band
            config:
              band: 0.05
              pool: runs/merged4model_n4_clean_scored.jsonl

          - stage: repair
            name: value_grounding
            module: plugins.repair.value_grounding

          - stage: judge
            name: route_a_tournament
            module: plugins.judge.route_a
            config:
              pool: runs/merged4model_n8_pool.jsonl
              max_tokens: 1024

    Environment:
        DEEPSEEK_API_KEY, data paths, etc.
    """
    import yaml

    config = yaml.safe_load(open(config_path))
    ctx = Context()
    ctx.config = config.get("config", {})

    # Load global services (LLM client, DB root, etc.)
    _init_services(ctx, config)

    registry = PluginRegistry()

    for entry in config.get("plugins", []):
        stage = entry["stage"]
        name = entry["name"]
        module_path = entry["module"]
        plugin_config = entry.get("config", {})

        try:
            mod = importlib.import_module(module_path)
            if hasattr(mod, "create_plugin"):
                fn = mod.create_plugin(plugin_config, ctx)
                registry.register(stage, name, fn)
                print(f"  loaded: [{stage}] {name} from {module_path}")
            else:
                print(f"  WARNING: {module_path} has no create_plugin(), skipping")
        except Exception as e:
            print(f"  ERROR loading {module_path}: {e}")

    return Pipeline(registry, ctx)


def _init_services(ctx: Context, config: dict):
    """Initialize shared services from config."""
    import os
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))

    from tools.llm_client import LLMClient

    llm_config = config.get("llm", {})
    api_key = os.environ.get(llm_config.get("api_key_env", "DEEPSEEK_API_KEY"))
    if api_key:
        client = LLMClient(
            base_url=llm_config.get("base_url", "https://api.deepseek.com"),
            model_name=llm_config.get("model_name", "deepseek-v4-flash"),
            api_key_env=llm_config.get("api_key_env", "DEEPSEEK_API_KEY"),
            timeout=llm_config.get("timeout", 120),
        )
        ctx.provide("llm", client)

    ctx.provide("db_root", config.get("data", {}).get("db_root", "data/dev_databases"))
    ctx.provide("dev_path", config.get("data", {}).get("dev", "data/dev.json"))
    ctx.provide("root", Path(__file__).resolve().parent.parent)  # project root (parent of harness/)
