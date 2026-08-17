"""Prompt evolver: evolve repair-prompt rules using TRAIN split only.

Self-contained, no gold from dev/test. Works by:
  1. Sampling train examples (question + gold SQL — train gold is allowed)
  2. Asking the LLM to distill recurring SQL-writing rules from the train pairs
  3. Writing the evolved rules to an artifact file that repair-stage plugins
     can inject into their prompts (via ctx.artifacts["prompt_rules"])

This is the cheapest form of "model tuning" — tuning the prompt, not weights.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

def create_tuner(config: dict, ctx) -> callable:
    root = ctx.get("root", Path("."))
    sys.path.insert(0, str(root))

    sample_size = config.get("sample_size", 200)
    artifact_key = config.get("artifact_key", "prompt_rules")

    def tune(train_iter) -> dict:
        client = ctx.get("llm")
        if not client:
            return {artifact_key: None, "error": "no llm"}

        # Collect train examples
        pairs = []
        for ex in train_iter:
            if len(pairs) >= sample_size:
                break
            q = ex.get("question", "")
            sql = ex.get("SQL", "")
            if q and sql:
                pairs.append((q, sql))

        if not pairs:
            return {artifact_key: None, "error": "no train examples"}

        # Distill rules from train pairs (few calls, chunked)
        rules_all = []
        chunk = 40
        for i in range(0, len(pairs), chunk):
            part = pairs[i:i+chunk]
            examples_text = "\n".join(
                f"Q: {q}\nSQL: {s}" for q, s in part
            )
            prompt = (
                "You are studying how gold SQL queries are written in a "
                "text-to-SQL training set. Below are question/SQL pairs from "
                "the TRAINING split. Distill 5 concise, actionable rules that "
                "capture recurring patterns (aggregation choice, join style, "
                "value formatting, ordering, distinct usage, etc).\n\n"
                f"{examples_text}\n\n"
                "Output exactly 5 rules, one per line, each starting with '- '."
            )
            try:
                comp = client.chat_completion(
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2, max_tokens=1024,
                )
                raw = comp["response"]["choices"][0]["message"]["content"]
                for line in raw.split("\n"):
                    line = line.strip()
                    if line.startswith("- ") and len(line) > 10:
                        rules_all.append(line[2:])
            except Exception:
                continue

        # Deduplicate rules
        seen, rules = set(), []
        for r in rules_all:
            k = r.lower()[:60]
            if k not in seen:
                seen.add(k)
                rules.append(r)

        return {artifact_key: rules[:20], "n_pairs": len(pairs), "n_rules": len(rules)}

    return tune
