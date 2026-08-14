"""E6 Preference-guided regeneration: BIRD annotation rules from train 9428 examples.
Rules are mined from train split only (no dev gold). Uses LLM to regenerate from scratch
with preference hints injected into the prompt."""
from __future__ import annotations
import sys
from pathlib import Path

def create_plugin(config: dict, ctx) -> callable:
    root = ctx.get("root", Path("."))
    sys.path.insert(0, str(root))
    from agents.e4_execution_repair_agent import extract_sql
    from tools.db_utils import BirdDatabase

    db_root = ctx.get("db_root", "data/dev_databases")
    template_path = root / "prompts" / "e6_preference_guided.md"
    template = template_path.read_text() if template_path.exists() else ""

    def plugin_fn(q, ctx):
        if not q.pred_sql.strip():
            return q.pred_sql
        db = BirdDatabase(db_id=q.db_id, db_root=db_root, timeout=30, max_rows=100)
        schema = db.get_schema()
        evidence = q.evidence or ""

        prompt = template.replace("{db_id}", q.db_id).replace("{schema}", schema)
        prompt = prompt.replace("{evidence}", evidence).replace("{question}", q.question)

        client = ctx.get("llm")
        if not client: return q.pred_sql

        try:
            comp = client.chat_completion(
                messages=[{"role": "system", "content": "You are an expert SQL assistant."},
                          {"role": "user", "content": prompt}],
                temperature=0.0, max_tokens=4096,
            )
            raw = comp["response"]["choices"][0]["message"]["content"]
            new_sql = extract_sql(raw)
            if new_sql and new_sql.strip():
                res = db.execute(new_sql)
                if res.get("ok") and res.get("rows"):
                    return new_sql
        except: pass
        return q.pred_sql

    return plugin_fn
