"""Execution repair: GLM/DeepSeek rewrite from execution error feedback."""
from __future__ import annotations
import sys
from pathlib import Path

def create_plugin(config: dict, ctx) -> callable:
    root = ctx.get("root", Path("."))
    sys.path.insert(0, str(root))
    from agents.e4_execution_repair_agent import extract_sql
    from tools.db_utils import BirdDatabase

    db_root = ctx.get("db_root", "data/dev_databases")
    max_repairs = config.get("max_repairs", 2)
    template_path = root / "prompts" / "e4_repair_sql.md"
    template = template_path.read_text() if template_path.exists() else ""

    def plugin_fn(q, ctx):
        if not q.pred_sql.strip():
            return q.pred_sql
        db = BirdDatabase(db_id=q.db_id, db_root=db_root, timeout=30, max_rows=100)
        sql = q.pred_sql

        for rnd in range(max_repairs):
            res = db.execute(sql)
            if res.get("ok") and res.get("rows"):
                return sql  # already works

            # Build repair prompt
            schema = db.get_schema()
            evidence = f"Evidence: {q.evidence}\n" if q.evidence else ""
            error_msg = res.get("error", "empty result")[:120]
            current_result = f"Error: {error_msg}" if not res.get("ok") else "Empty result"
            prompt = template.replace("{db_id}", q.db_id).replace("{schema}", schema)
            prompt = prompt.replace("{evidence}", evidence).replace("{question}", q.question)
            prompt = prompt.replace("{current_sql}", sql).replace("{current_result}", current_result)

            client = ctx.get("llm")
            if not client: return sql

            try:
                comp = client.chat_completion(
                    messages=[{"role": "system", "content": "You are an expert SQL debugging assistant."},
                              {"role": "user", "content": prompt}],
                    temperature=0.0, max_tokens=8192,
                )
                raw = comp["response"]["choices"][0]["message"]["content"]
                new_sql = extract_sql(raw)
                if new_sql and new_sql.strip() and new_sql.strip().lower() != sql.strip().lower():
                    new_res = db.execute(new_sql)
                    if new_res.get("ok") and new_res.get("rows"):
                        sql = new_sql
                    else:
                        sql = new_sql  # keep even if still broken, might be closer
            except:
                break

        return sql

    return plugin_fn
