"""Deep regeneration: LLM generates SQL from scratch with full DB context."""
from __future__ import annotations
import sys
from pathlib import Path

def create_plugin(config: dict, ctx) -> callable:
    root = ctx.get("root", Path("."))
    sys.path.insert(0, str(root))
    from agents.e4_execution_repair_agent import extract_sql
    from tools.db_utils import BirdDatabase

    db_root = ctx.get("db_root", "data/dev_databases")
    max_repairs = config.get("max_repairs", 3)

    def plugin_fn(q, ctx):
        db = BirdDatabase(db_id=q.db_id, db_root=db_root, timeout=30, max_rows=100)
        schema = db.get_schema()
        fks = str(db.get_foreign_keys())

        # Build column samples (string columns, first 5 tables, 3 samples each)
        samples_text = ""
        for table in db.list_tables()[:5]:
            cols = db.get_table_columns(table)
            for col in cols[:3]:
                vals = db.get_column_samples(table, col["name"], limit=3)
                if vals and isinstance(vals[0], str):
                    samples_text += f"  {table}.{col['name']}: {vals}\n"
            if len(samples_text) > 2000: break

        prompt = (
            f"Generate a valid SQLite SELECT query to answer this question.\n\n"
            f"Database: {q.db_id}\nSchema:\n{schema}\n\n"
            f"Foreign Keys:\n{fks}\n\n"
            f"Column Samples:\n{samples_text}\n\n"
            f"Question: {q.question}\n"
        )
        if q.evidence:
            prompt += f"Evidence: {q.evidence}\n"
        prompt += "\nOutput only the SQL query in a ```sql block."

        client = ctx.get("llm")
        if not client: return q.pred_sql

        try:
            comp = client.chat_completion(
                messages=[{"role":"system","content":"You are an expert SQL assistant."},
                          {"role":"user","content":prompt}],
                temperature=0.3, max_tokens=4096,
            )
            raw = comp["response"]["choices"][0]["message"]["content"]
            sql = extract_sql(raw)

            # Repair rounds
            for _ in range(max_repairs):
                res = db.execute(sql)
                if res.get("ok") and res.get("rows"):
                    return sql
                error = res.get("error", "empty")[:100]
                repair_prompt = (
                    f"The following SQL produced an error. Fix it.\n\n"
                    f"SQL: {sql}\nError: {error}\n\n"
                    f"Question: {q.question}\nSchema:\n{schema}\n"
                    f"Output only the fixed SQL in a ```sql block."
                )
                comp2 = client.chat_completion(
                    messages=[{"role":"system","content":"You are an expert SQL repair assistant."},
                              {"role":"user","content":repair_prompt}],
                    temperature=0.0, max_tokens=4096,
                )
                raw2 = comp2["response"]["choices"][0]["message"]["content"]
                sql = extract_sql(raw2)

            # Final check
            res = db.execute(sql)
            if res.get("ok") and res.get("rows"):
                return sql
        except: pass
        return q.pred_sql

    return plugin_fn
