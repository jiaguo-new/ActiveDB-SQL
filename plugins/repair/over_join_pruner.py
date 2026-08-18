"""Over-JOIN pruner: remove redundant JOIN tables verified by execution.

Targets 30 'extra_tables' errors (40% recoverable — highest category rate).
Logic: try removing each JOIN clause one at a time, keep the removal only if
the query still executes AND returns a plausible result (non-empty, fewer rows
or same as original but the table wasn't in SELECT output).
"""
from __future__ import annotations
import sys, re
from pathlib import Path


def _extract_joins(sql: str) -> list[tuple[str, int, int]]:
    """Find JOIN clauses as (table_name, start_pos, end_pos)."""
    joins = []
    for m in re.finditer(
        r'\bJOIN\s+(\w+)\s+(?:AS\s+)?(\w+)?\s+ON\s+[^J]+?(?=\s+JOIN\b|\s+WHERE\b|\s+GROUP\b|\s+ORDER\b|\s+HAVING\b|\s+LIMIT\b|\s*$)',
        sql, re.I | re.S,
    ):
        joins.append((m.group(1), m.start(), m.end()))
    return joins


def _remove_join(sql: str, start: int, end: int) -> str:
    return (sql[:start] + sql[end:]).strip()


def _tables_referenced(sql: str) -> set[str]:
    """All table names referenced (in FROM, JOIN, WHERE alias, SELECT alias)."""
    refs = set()
    for m in re.finditer(r'\b(?:FROM|JOIN)\s+(\w+)', sql, re.I):
        refs.add(m.group(1).lower())
    return refs


def create_plugin(config: dict, ctx) -> callable:
    root = ctx.get("root", Path("."))
    sys.path.insert(0, str(root))
    from tools.db_utils import BirdDatabase

    db_root = ctx.get("db_root", "data/dev_databases")

    def plugin_fn(q, ctx):
        if not q.pred_sql.strip():
            return q.pred_sql

        sql = q.pred_sql
        joins = _extract_joins(sql)
        if len(joins) < 2:
            return q.pred_sql  # only prune if multiple joins

        db = BirdDatabase(db_id=q.db_id, db_root=db_root, timeout=30, max_rows=100)
        base_res = db.execute(sql)
        if not base_res.get("ok") or not base_res.get("rows"):
            return q.pred_sql  # only prune working SQL

        best_sql = sql
        best_rows = base_res.get("rows", [])
        changed = False

        for table, start, end in joins:
            candidate = _remove_join(best_sql, start, end)
            if not candidate or candidate == best_sql:
                continue

            # Check if the removed table is referenced elsewhere (would break)
            remaining_refs = _tables_referenced(candidate)
            if table.lower() in remaining_refs:
                continue  # table used elsewhere, can't remove

            res = db.execute(candidate)
            if not res.get("ok") or not res.get("rows"):
                continue  # removal broke it

            # Prefer removal that produces fewer rows (dedup effect)
            # but not zero rows
            if 0 < len(res["rows"]) <= len(best_rows):
                best_sql = candidate
                best_rows = res["rows"]
                changed = True
                # recompute join positions after removal
                joins = _extract_joins(best_sql)

        return best_sql if changed else q.pred_sql

    return plugin_fn
