"""E7 Result self-critique: show draft SQL + execution result to LLM,
ask it to verify if the result actually answers the question."""
from __future__ import annotations
import sys
from pathlib import Path

def create_plugin(config: dict, ctx) -> callable:
    root = ctx.get("root", Path("."))
    sys.path.insert(0, str(root))
    from agents.e4_execution_repair_agent import extract_sql
    from tools.db_utils import BirdDatabase

    db_root = ctx.get("db_root", "data/dev_databases")
    template_path = root / "prompts" / "e7_result_critique.md"
    template = template_path.read_text() if template_path.exists() else ""

    def plugin_fn(q, ctx):
        if not q.pred_sql.strip():
            return q.pred_sql
        db = BirdDatabase(db_id=q.db_id, db_root=db_root, timeout=30, max_rows=100)

        exec_res = db.execute(q.pred_sql)
        if not exec_res.get("ok") or not exec_res.get("rows"):
            return q.pred_sql  # already broken, skip critique

        rows = exec_res.get("rows", [])
        draft_result = f"{len(rows)} rows:\n"
        for r in rows[:5]:
            draft_result += "  " + " | ".join(str(v)[:30] for v in r[:5]) + "\n"

        schema = db.get_schema()
        evidence = f"Evidence: {q.evidence}\n" if q.evidence else ""

        prompt = template.replace("{db_id}", q.db_id).replace("{schema}", schema)
        prompt = prompt.replace("{evidence}", evidence).replace("{question}", q.question)
        prompt = prompt.replace("{draft_sql}", q.pred_sql).replace("{draft_result}", draft_result)

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
            if new_sql and new_sql.strip() and new_sql.strip().lower() != q.pred_sql.strip().lower():
                res = db.execute(new_sql)
                if res.get("ok") and res.get("rows"):
                    return new_sql
        except: pass
        return q.pred_sql

    return plugin_fn
