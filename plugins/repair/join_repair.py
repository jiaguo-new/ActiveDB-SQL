"""JOIN repair: FK-graph shortest path + denoising."""
from __future__ import annotations
import sys
from pathlib import Path

def create_plugin(config: dict, ctx) -> callable:
    root = ctx.get("root", Path("."))
    sys.path.insert(0, str(root))
    from agents.e2_join_repair import repair_joins, diagnose_execution
    from agents.e4_execution_repair_agent import extract_sql
    from tools.db_utils import BirdDatabase

    db_root = ctx.get("db_root", "data/dev_databases")
    template_path = root / "prompts" / "e2_join_repair.md"
    template = template_path.read_text() if template_path.exists() else ""

    def plugin_fn(q, ctx):
        if not q.pred_sql.strip():
            return q.pred_sql
        db = BirdDatabase(db_id=q.db_id, db_root=db_root, timeout=30, max_rows=100)

        join_info = repair_joins(q.pred_sql, db)
        exec_diag = diagnose_execution(q.pred_sql, db)

        if exec_diag.get("noise_report", {}).get("is_clean", True) and join_info.get("is_connected", True):
            return q.pred_sql

        schema = db.get_schema()
        fks = str(db.get_foreign_keys())
        evidence = f"Evidence: {q.evidence}\n" if q.evidence else ""
        noise = json.dumps(exec_diag.get("noise_report", {}))[:200]
        jinfo = json.dumps(join_info.get("suggested_join_info", {}))[:200]

        prompt = template.replace("{db_id}", q.db_id).replace("{schema}", schema)
        prompt = prompt.replace("{fks}", fks).replace("{evidence}", evidence)
        prompt = prompt.replace("{question}", q.question).replace("{draft_sql}", q.pred_sql)
        prompt = prompt.replace("{noise_report}", noise).replace("{join_info}", jinfo)

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

import json
