#!/usr/bin/env python3
"""Auto-discovery engine: search plugin combinations and hyperparameters.

The continuous-learning loop:
  1. Load a base pipeline config (YAML)
  2. Propose a mutation (reorder plugins / drop a plugin / perturb a knob)
  3. Run the pipeline on a fixed question subset (fast trial)
  4. Evaluate EX with the isolated evaluator (only place gold is touched)
  5. Append a trial record to the ledger (trials.jsonl)
  6. If EX improves, the mutant becomes the new champion
  7. Repeat until budget exhausted; champion gets a full run

Compliance rules enforced by construction:
  - Plugins never see gold: evaluation happens outside the plugin pipeline
  - Trials score aggregate dev EX only (no per-sample tuning hooks exist)
  - Every trial is recorded with a config hash for auditability
  - Tuning plugins (stage "tune") are resolved against TRAIN data paths only;
    the engine refuses a tune config whose data.source contains "dev"
"""
from __future__ import annotations

import copy
import hashlib
import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


# ── Ledger ────────────────────────────────────────────────────────────────

@dataclass
class Trial:
    trial_id: int
    config: dict
    config_hash: str
    ex: int
    total: int
    duration_s: float
    parent_hash: str | None
    mutation: str
    accepted: bool
    ts: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))

    def to_line(self) -> str:
        return json.dumps({
            "trial_id": self.trial_id,
            "config_hash": self.config_hash,
            "parent_hash": self.parent_hash,
            "mutation": self.mutation,
            "ex": self.ex,
            "total": self.total,
            "ex_rate": round(self.ex / max(self.total, 1), 4),
            "duration_s": round(self.duration_s, 1),
            "accepted": self.accepted,
            "ts": self.ts,
            "config": self.config,
        }, ensure_ascii=False)


def config_hash(cfg: dict) -> str:
    return hashlib.md5(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:10]


# ── Config space ──────────────────────────────────────────────────────────

# Mutable knobs per plugin module: {module_name: {knob: [choices]}}
# Keep choices conservative; every knob here is one we know is safe to move.
KNOB_SPACE: dict[str, dict[str, list]] = {
    "plugins.select.orm_band": {
        "band": [0.02, 0.05, 0.08, 0.12],
    },
    "plugins.judge.route_a": {
        "top_k": [6, 8, 12, 16],
        "max_tokens": [512, 1024, 2048],
    },
    "plugins.repair.execution_repair": {
        "max_repairs": [1, 2, 3],
    },
    "plugins.regen.deep_regen": {
        "max_repairs": [2, 3, 4],
    },
    "plugins.regen.regen_as_candidate": {
        "regen_score": [0.6, 0.7, 0.75, 0.8, 0.9],
        "max_repairs": [1, 2, 3],
    },
}

# Plugins safe to drop for subset trials (order/subset search space)
DROPPABLE = {
    "plugins.repair.value_grounding",
    "plugins.repair.execution_repair",
    "plugins.repair.join_repair",
    "plugins.repair.column_grounding",
    "plugins.repair.e3v_enhanced",
    "plugins.repair.e5_det_rules",
    "plugins.judge.judge_3way",
    "plugins.judge.multigen",
    "plugins.regen.preference_guided",
    "plugins.regen.result_critique",
}
# Never drop (backbone)
REQUIRED = {
    "plugins.select.orm_band",
    "plugins.judge.route_a",
    "plugins.regen.deep_regen",
}


class DiscoveryEngine:
    def __init__(self, base_config_path: Path, seed: int = 42):
        self.base: dict = yaml.safe_load(open(base_config_path))
        self.rng = random.Random(seed)
        self.trials: list[Trial] = []
        self.champion: dict = copy.deepcopy(self.base)
        self.champion_ex: int = -1
        self.trial_counter = 0

    # ── mutation operators ──

    def _plugins(self, cfg: dict) -> list[dict]:
        return cfg.setdefault("plugins", [])

    def mutate(self, cfg: dict) -> tuple[dict, str]:
        """Return (mutant, description). One mutation per call."""
        mutant = copy.deepcopy(cfg)
        ops = ["knob", "reorder", "drop", "reinsert"]
        weights = [0.55, 0.15, 0.15, 0.15]
        op = self.rng.choices(ops, weights=weights, k=1)[0]

        if op == "knob":
            candidates = [
                (i, e, knob, choices)
                for i, e in enumerate(self._plugins(mutant))
                for knob, choices in KNOB_SPACE.get(e["module"], {}).items()
            ]
            if not candidates:
                return mutant, "noop"
            i, e, knob, choices = self.rng.choice(candidates)
            old = e.setdefault("config", {}).get(knob)
            new = self.rng.choice([c for c in choices if c != old] or choices)
            e["config"][knob] = new
            return mutant, f"knob {e['name']}.{knob}: {old} -> {new}"

        if op == "reorder":
            repair_idx = [i for i, e in enumerate(self._plugins(mutant))
                          if e["stage"] == "repair"]
            if len(repair_idx) < 2:
                return mutant, "noop"
            i, j = self.rng.sample(repair_idx, 2)
            pl = self._plugins(mutant)
            pl[i], pl[j] = pl[j], pl[i]
            names = [self._plugins(mutant)[k]["name"] for k in sorted(repair_idx)]
            return mutant, f"reorder repair: {names}"

        if op == "drop":
            dropable_idx = [i for i, e in enumerate(self._plugins(mutant))
                            if e["module"] in DROPPABLE]
            if not dropable_idx:
                return mutant, "noop"
            i = self.rng.choice(dropable_idx)
            name = self._plugins(mutant)[i]["name"]
            del self._plugins(mutant)[i]
            return mutant, f"drop {name}"

        # reinsert: re-add a dropped plugin at a random position in its stage
        present = {e["module"] for e in self._plugins(mutant)}
        missing = [m for m in DROPPABLE if m not in present]
        if not missing:
            return mutant, "noop"
        module = self.rng.choice(missing)
        stage = module.split(".")[1]
        entry = {"stage": stage, "name": module.split(".")[-1], "module": module}
        stage_idx = [i for i, e in enumerate(self._plugins(mutant))
                     if e["stage"] == stage]
        pos = self.rng.randint(0, len(stage_idx)) if stage_idx else len(self._plugins(mutant))
        # map to absolute position
        if stage_idx:
            pos = stage_idx[min(pos, len(stage_idx) - 1)]
        else:
            pos = len(self._plugins(mutant))
        self._plugins(mutant).insert(pos, entry)
        return mutant, f"reinsert {entry['name']} at {stage}[{pos}]"

    # ── compliance gate ──

    @staticmethod
    def compliance_check(cfg: dict) -> list[str]:
        issues = []
        for e in cfg.get("plugins", []):
            if e["stage"] == "tune":
                src = str(e.get("config", {}).get("data", {}).get("source", ""))
                if "dev" in src.lower() or "test" in src.lower():
                    issues.append(f"tune plugin {e['name']} source points at {src!r}")
        if not any(e["stage"] == "select" for e in cfg.get("plugins", [])):
            issues.append("no select-stage plugin")
        return issues

    def record(self, cfg: dict, ex: int, total: int, duration: float,
               mutation: str, parent: str | None) -> Trial:
        self.trial_counter += 1
        accepted = ex > self.champion_ex
        if accepted:
            self.champion = copy.deepcopy(cfg)
            self.champion_ex = ex
        t = Trial(self.trial_counter, cfg, config_hash(cfg), ex, total,
                  duration, parent, mutation, accepted)
        self.trials.append(t)
        return t


def load_ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.open() if l.strip()]


def append_ledger(path: Path, trial: Trial) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(trial.to_line() + "\n")
