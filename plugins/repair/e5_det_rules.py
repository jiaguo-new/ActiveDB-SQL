"""E5det: deterministic rules — COUNT(*) fix + over-JOIN prune. No LLM, no gold."""
from __future__ import annotations
import sys
from pathlib import Path

def create_plugin(config: dict, ctx) -> callable:
    root = ctx.get("root", Path("."))
    sys.path.insert(0, str(root))
    from agents.e5_deterministic_repair import deterministic_repair
    from tools.db_utils import BirdDatabase

    db_root = ctx.get("db_root", "data/dev_databases")

    def plugin_fn(q, ctx):
        if not q.pred_sql.strip():
            return q.pred_sql
        db = BirdDatabase(db_id=q.db_id, db_root=db_root, timeout=30, max_rows=100)
        new_sql, repairs = deterministic_repair(q.pred_sql, q.question, db)
        if new_sql != q.pred_sql:
            res = db.execute(new_sql)
            if res.get("ok") and res.get("rows"):
                return new_sql
        return q.pred_sql

    return plugin_fn
