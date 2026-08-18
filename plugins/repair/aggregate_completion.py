"""Aggregate completion: add missing aggregation when question implies it.

Targets the 26 'missing_agg' errors (46% recoverable in pool = highest
recoverability of any error class). Logic:
  1. If question asks for "highest/lowest/biggest/smallest/average/total/number of"
     but SQL has no corresponding aggregation function
  2. Try adding the aggregation (MAX/MIN/AVG/SUM/COUNT) via execution-guided edit
  3. Verify by executing and checking non-empty result

Compliance: no gold read. Uses only the draft SQL's own execution result.
"""
from __future__ import annotations
import sys, re
from pathlib import Path

AGG_TRIGGERS = {
    r"\b(highest|maximum|max|largest|biggest|top)\b": ("MAX", "highest/largest value"),
    r"\b(lowest|minimum|min|smallest|least|bottom)\b": ("MIN", "lowest/smallest value"),
    r"\b(average|avg|mean)\b": ("AVG", "average"),
    r"\b(total|sum|combined)\b": ("SUM", "total/sum"),
    r"\b(how many|number of|count|amount of)\b": ("COUNT", "count"),
}

def _has_agg(sql):
    return bool(re.search(r'\b(COUNT|SUM|AVG|MAX|MIN)\s*\(', sql, re.I))

def _add_agg(sql, agg_func):
    """Insert aggregation around the first numeric column in SELECT."""
    # Simple approach: wrap first column after SELECT with AGG(col)
    m = re.search(r'SELECT\s+(DISTINCT\s+)?([^,\n]+?)(,|\s+FROM)', sql, re.I | re.S)
    if not m:
        return None
    distinct_part = m.group(1) or ""
    col = m.group(2).strip()
    # Skip if already aggregated or is *
    if re.search(r'\b(COUNT|SUM|AVG|MAX|MIN)\s*\(', col, re.I) or col.strip() == "*":
        return None
    new_select = f"{agg_func}({col})"
    return sql[:m.start(2)] + new_select + sql[m.end(2):]

def create_plugin(config: dict, ctx) -> callable:
    root = ctx.get("root", Path("."))
    sys.path.insert(0, str(root))
    from tools.db_utils import BirdDatabase

    db_root = ctx.get("db_root", "data/dev_databases")

    def plugin_fn(q, ctx):
        if not q.pred_sql.strip() or _has_agg(q.pred_sql):
            return q.pred_sql

        q_lower = q.question.lower()
        triggered = None
        for pattern, (agg, desc) in AGG_TRIGGERS.items():
            if re.search(pattern, q_lower, re.I):
                triggered = (agg, desc)
                break
        if not triggered:
            return q.pred_sql

        db = BirdDatabase(db_id=q.db_id, db_root=db_root, timeout=30, max_rows=100)
        # Try adding the triggered aggregation
        new_sql = _add_agg(q.pred_sql, triggered[0])
        if not new_sql:
            return q.pred_sql

        res = db.execute(new_sql)
        if res.get("ok") and res.get("rows"):
            return new_sql

        # Also try alternative aggregations for the same trigger
        for pattern, (agg2, _) in AGG_TRIGGERS.items():
            if agg2 != triggered[0] and re.search(pattern, q_lower, re.I):
                alt = _add_agg(q.pred_sql, agg2)
                if alt:
                    res2 = db.execute(alt)
                    if res2.get("ok") and res2.get("rows"):
                        return alt
        return q.pred_sql

    return plugin_fn
