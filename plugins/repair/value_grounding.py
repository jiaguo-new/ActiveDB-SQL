"""Value grounding repair: fix WHERE-clause string literal case using DB cell lookup."""
from __future__ import annotations
import sys
from pathlib import Path

def create_plugin(config: dict, ctx) -> callable:
    root = ctx.get("root", Path("."))
    sys.path.insert(0, str(root))
    from agents.e3v_value_probe import probe_values
    from tools.db_utils import BirdDatabase

    db_root = ctx.get("db_root", "data/dev_databases")

    def plugin_fn(q, ctx):
        if not q.pred_sql.strip():
            return q.pred_sql
        db = BirdDatabase(db_id=q.db_id, db_root=db_root, timeout=30, max_rows=100)
        result = probe_values(q.pred_sql, db, sample_limit=200)
        if result.get("repaired_sql") and result["repaired_sql"] != q.pred_sql:
            # Verify it executes
            res = db.execute(result["repaired_sql"])
            if res.get("ok") and res.get("rows"):
                return result["repaired_sql"]
        return q.pred_sql

    return plugin_fn
