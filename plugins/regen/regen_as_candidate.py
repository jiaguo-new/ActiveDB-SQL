"""Hybrid plugin: regen first, then judge over pool + regen candidates.

Key insight: in the old pipeline, regen runs AFTER judge, so regen-generated
SQL never competes with pool candidates. 318/400 regen SQLs are new (not in
pool). This plugin:

  1. Runs deep_regen to generate SQL for failing questions
  2. Adds the regen SQL as a new candidate into the question's candidate list
  3. Lets the judge stage (running after) pick from pool + regen candidates

This means judge sees: pool candidates + fresh regen candidates, and picks
the best across both sources.
"""
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
    # Optionally give regen candidates an ORM-style score boost/penalty
    regen_score = config.get("regen_score", 0.75)  # pseudo-ORM score for regen SQL

    def plugin_fn(q, ctx):
        if not q.pred_sql.strip():
            return q.pred_sql

        db = BirdDatabase(db_id=q.db_id, db_root=db_root, timeout=30, max_rows=100)
        schema = db.get_schema()
        fks = str(db.get_foreign_keys())

        # Build column samples
        samples_text = ""
        for table in db.list_tables()[:5]:
            cols = db.get_table_columns(table)
            for col in cols[:3]:
                vals = db.get_column_samples(table, col["name"], limit=3)
                if vals and isinstance(vals[0], str):
                    samples_text += f"  {table}.{col['name']}: {vals}\n"
            if len(samples_text) > 2000:
                break

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
        if not client:
            return q.pred_sql

        regen_sql = ""
        try:
            comp = client.chat_completion(
                messages=[{"role": "system", "content": "You are an expert SQL assistant."},
                          {"role": "user", "content": prompt}],
                temperature=0.3, max_tokens=4096,
            )
            raw = comp["response"]["choices"][0]["message"]["content"]
            regen_sql = extract_sql(raw)

            # Repair rounds
            for _ in range(max_repairs):
                res = db.execute(regen_sql)
                if res.get("ok") and res.get("rows"):
                    break
                error = res.get("error", "empty")[:100]
                repair_prompt = (
                    f"The following SQL produced an error. Fix it.\n\n"
                    f"SQL: {regen_sql}\nError: {error}\n\n"
                    f"Question: {q.question}\nSchema:\n{schema}\n"
                    f"Output only the fixed SQL in a ```sql block."
                )
                comp2 = client.chat_completion(
                    messages=[{"role": "system", "content": "You are an expert SQL repair assistant."},
                              {"role": "user", "content": repair_prompt}],
                    temperature=0.0, max_tokens=4096,
                )
                raw2 = comp2["response"]["choices"][0]["message"]["content"]
                regen_sql = extract_sql(raw2)
        except Exception:
            return q.pred_sql

        if not regen_sql or not regen_sql.strip():
            return q.pred_sql

        # Execute regen SQL to attach result
        res = db.execute(regen_sql)
        rows = res.get("rows") if res.get("ok") else None

        # KEY HYBRID LOGIC: add regen SQL to the question's candidates
        # so the judge stage (running after) can compare pool + regen
        existing_sqls = {c.get("sql", "").strip().lower() for c in q.candidates if c.get("sql")}
        if regen_sql.strip().lower() not in existing_sqls:
            q.candidates.append({
                "sql": regen_sql,
                "model": "deep_regen",
                "orm_score": regen_score,
                "result": rows,
                "_is_regen": True,
            })

        # Only adopt regen SQL directly if pool had NO executable candidate
        # (otherwise let judge decide)
        has_exec_pool = any(c.get("result") is not None and not c.get("_is_regen") for c in q.candidates)
        if not has_exec_pool and res.get("ok") and rows:
            return regen_sql

        return q.pred_sql

    return plugin_fn
