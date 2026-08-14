"""E3v+ enhanced value probe: LIKE pattern + date format detection. Only reads DB."""
from __future__ import annotations
import sys
from pathlib import Path

def create_plugin(config: dict, ctx) -> callable:
    root = ctx.get("root", Path("."))
    sys.path.insert(0, str(root))
    from agents.e3v_enhanced_probe import probe_like_patterns, probe_date_formats
    from agents.e3v_value_probe import probe_values
    from agents.e4_execution_repair_agent import extract_sql
    from tools.db_utils import BirdDatabase

    db_root = ctx.get("db_root", "data/dev_databases")
    template_path = root / "prompts" / "e3v_value_grounding.md"
    template = template_path.read_text() if template_path.exists() else ""

    def plugin_fn(q, ctx):
        if not q.pred_sql.strip():
            return q.pred_sql
        db = BirdDatabase(db_id=q.db_id, db_root=db_root, timeout=30, max_rows=100)

        like_report = probe_like_patterns(q.pred_sql, q.question, db)
        date_report = probe_date_formats(q.pred_sql, q.question, db)
        value_report = probe_values(q.pred_sql, db, sample_limit=200)

        has_issues = (like_report.get("repairs") or date_report.get("repairs")
                      or value_report.get("repairs"))
        if not has_issues:
            return q.pred_sql

        # Combine reports
        cell_text = value_report.get("cell_values_text", "")
        for r in (like_report.get("repairs", []) + date_report.get("repairs", [])):
            cell_text += f"\n{sug.get('suggestion','')}" if isinstance(r, dict) else str(r)

        client = ctx.get("llm")
        if not client: return q.pred_sql

        schema = db.get_schema()
        fks = str(db.get_foreign_keys())
        evidence = f"Evidence: {q.evidence}\n" if q.evidence else ""
        exec_res = db.execute(q.pred_sql)
        draft_result = f"{len(exec_res.get('rows', []))} rows" if exec_res.get("ok") else exec_res.get("error", "error")[:80]

        prompt = template.replace("{db_id}", q.db_id).replace("{schema}", schema)
        prompt = prompt.replace("{fks}", fks).replace("{evidence}", evidence)
        prompt = prompt.replace("{question}", q.question).replace("{draft_sql}", q.pred_sql)
        prompt = prompt.replace("{draft_result}", draft_result).replace("{cell_values}", cell_text)

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
