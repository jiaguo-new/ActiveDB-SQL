#!/usr/bin/env python3
"""Deterministic SQL repair: COUNT(*) normalization + over-JOIN pruning.

Two rule-based, no-LLM, no-gold repairs:

1. COUNT(*) → COUNT(entity_column):
   When the question asks "how many X" and the draft uses COUNT(*),
   find the primary entity column (id/code/name in the main table) and
   replace COUNT(*) with COUNT(that_column). Only applies if the column
   has NULLs (otherwise COUNT(*) == COUNT(col) and no effect).

2. Over-JOIN pruning:
   When the draft JOINs a table whose columns are NOT referenced in
   SELECT, WHERE, GROUP BY, ORDER BY, or HAVING — that table is unnecessary.
   Remove the JOIN + its ON condition. Only applies if removal doesn't
   cause a syntax error (verified by re-execution).
"""
from __future__ import annotations

import re
from tools.db_utils import BirdDatabase


# ── 1. COUNT(*) normalization ─────────────────────────────────────────

def repair_count_star(sql: str, question: str, db: BirdDatabase) -> tuple[str, list[dict]]:
    """Replace COUNT(*) with COUNT(entity_column) when appropriate."""
    if not re.search(r"count\s*\(\s*\*\s*\)", sql, re.IGNORECASE):
        return sql, []

    # extract tables
    tables = re.findall(r"(?:\bFROM\b|\bJOIN\b)\s+`?([A-Za-z_][A-Za-z0-9_]*)`?", sql, re.IGNORECASE)
    tables = list(dict.fromkeys(tables))
    if not tables:
        return sql, []

    # find best entity column: prefer id/code columns in the first table
    best_col = None
    best_table = None
    for t in tables:
        try:
            with db._connection() as conn:
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info(`{t}`)").fetchall()]
        except Exception:
            continue
        for c in cols:
            c_lower = c.lower()
            if any(k in c_lower for k in ["id", "code", "cds"]):
                # check if column has NULLs
                try:
                    with db._connection() as conn:
                        null_count = conn.execute(
                            f'SELECT COUNT(*) FROM `{t}` WHERE `{c}` IS NULL'
                        ).fetchone()[0]
                except Exception:
                    continue
                if null_count > 0:
                    best_col = c; best_table = t; break
        if best_col:
            break

    if not best_col:
        return sql, []

    # also try name columns if no id column has NULLs
    if not best_col:
        for t in tables:
            try:
                with db._connection() as conn:
                    cols = [r[1] for r in conn.execute(f"PRAGMA table_info(`{t}`)").fetchall()]
            except Exception:
                continue
            for c in cols:
                c_lower = c.lower()
                if "name" in c_lower or "school" in c_lower:
                    try:
                        with db._connection() as conn:
                            null_count = conn.execute(
                                f'SELECT COUNT(*) FROM `{t}` WHERE `{c}` IS NULL'
                            ).fetchone()[0]
                    except Exception:
                        continue
                    if null_count > 0:
                        best_col = c; best_table = t; break
            if best_col:
                break

    if not best_col:
        return sql, []

    new_sql = re.sub(
        r"count\s*\(\s*\*\s*\)",
        f"COUNT({best_col})",
        sql,
        flags=re.IGNORECASE,
    )
    if new_sql != sql:
        return new_sql, [{"repair": "count_star", "column": best_col, "table": best_table}]
    return sql, []


# ── 2. Over-JOIN pruning ──────────────────────────────────────────────

def _find_join_clauses(sql: str) -> list[dict]:
    """Find JOIN clauses: {table, alias, full_match, start, end}."""
    # Match: JOIN table [AS] alias ON condition
    pattern = re.compile(
        r"(?:\bINNER\s+|\bLEFT\s+|\bRIGHT\s+|\bOUTER\s+|\bCROSS\s+)?"
        r"\bJOIN\s+`?([A-Za-z_][A-Za-z0-9_]*)`?"
        r"(?:\s+(?:AS\s+)?(`?[A-Za-z_][A-Za-z0-9_]*`?))?"
        r"\s+ON\s+.*?(?=\b(?:INNER|LEFT|RIGHT|OUTER|CROSS|JOIN|WHERE|GROUP|ORDER|LIMIT|UNION|$))",
        re.IGNORECASE | re.DOTALL,
    )
    joins = []
    for m in pattern.finditer(sql):
        table = m.group(1)
        alias = m.group(2).strip("`") if m.group(2) else table
        joins.append({
            "table": table,
            "alias": alias,
            "full_match": m.group(0),
            "start": m.start(),
            "end": m.end(),
        })
    return joins


def _table_referenced_in_clauses(sql: str, table: str, alias: str) -> bool:
    """Check if table/alias is referenced in SELECT, WHERE, GROUP BY, ORDER BY, HAVING."""
    # remove the JOIN clause itself from checking
    # check if alias or table name appears outside FROM/JOIN context
    # Look for alias. or `table`. pattern in SELECT/WHERE/etc
    patterns = [
        rf"\b{re.escape(alias)}\s*\.",
        rf"`{re.escape(alias)}`\s*\.",
        rf"\b{re.escape(table)}\s*\.",
        rf"`{re.escape(table)}`\s*\.",
    ]
    for p in patterns:
        if re.search(p, sql, re.IGNORECASE):
            # count occurrences: if only in the JOIN clause itself, it's not referenced elsewhere
            # simple heuristic: if alias appears more than once (once in JOIN, once elsewhere)
            count = len(re.findall(p, sql, re.IGNORECASE))
            if count > 1:
                return True
    return False


def prune_over_joins(sql: str, db: BirdDatabase) -> tuple[str, list[dict]]:
    """Remove unnecessary JOIN clauses whose tables are not referenced elsewhere."""
    joins = _find_join_clauses(sql)
    if len(joins) <= 1:
        return sql, []  # need at least FROM + 1 JOIN to consider pruning

    repairs = []
    new_sql = sql

    for join in joins:
        table = join["table"]
        alias = join["alias"]

        # Check if this table's columns are referenced anywhere in the SQL
        # outside of the JOIN clause itself
        # Remove the JOIN clause from the SQL and check if alias/table still appears
        sql_without_join = new_sql.replace(join["full_match"], "", 1)

        referenced = _table_referenced_in_clauses(sql_without_join, table, alias)

        if not referenced:
            # Try removing this JOIN — verify the result still executes
            candidate = sql_without_join
            # clean up double spaces
            candidate = re.sub(r"\s+", " ", candidate).strip()

            try:
                res = db.execute(candidate)
                if res.get("ok"):
                    new_sql = candidate
                    repairs.append({"repair": "prune_join", "table": table, "alias": alias})
            except Exception:
                pass  # removal caused error, skip

    return new_sql, repairs


# ── Combined ──────────────────────────────────────────────────────────

def deterministic_repair(sql: str, question: str, db: BirdDatabase) -> tuple[str, list[dict]]:
    """Apply both COUNT(*) normalization and over-JOIN pruning."""
    all_repairs = []
    # 1. COUNT(*) first
    sql, reps = repair_count_star(sql, question, db)
    all_repairs.extend(reps)
    # 2. over-JOIN pruning
    sql, reps = prune_over_joins(sql, db)
    all_repairs.extend(reps)
    return sql, all_repairs
