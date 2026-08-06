#!/usr/bin/env python3
"""Active-probe value grounding: look up actual DB cell values for WHERE-clause
literals in a draft SQL, using case-insensitive + fuzzy matching.

This is the deterministic probing layer (no LLM). It extracts string literals
from the draft SQL's WHERE clause, queries the DB for actual column values,
and returns the best-matching actual value for each literal.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from tools.db_utils import BirdDatabase


def _extract_alias_map(sql: str) -> dict[str, str]:
    alias_map: dict[str, str] = {}
    for m in re.finditer(
        r"(?:\bFROM\b|\bJOIN\b)\s+`?([A-Za-z_][A-Za-z0-9_]*)`?"
        r"(?:\s+AS\b|\s+)(`?[A-Za-z_][A-Za-z0-9_]*`?)?",
        sql, re.IGNORECASE,
    ):
        table = m.group(1)
        alias = m.group(2)
        if alias:
            alias_map[alias.strip("`")] = table
        alias_map[table] = table
    return alias_map


def _extract_string_conditions(sql: str) -> list[tuple[str | None, str, str]]:
    conditions: list[tuple[str | None, str, str]] = []
    pattern = re.compile(
        r"(?:(\w+)\.)?(?:`([^`]+)`|(\w+))\s*(?:=|IN|in|LIKE|like)\s*"
        r"('[^']*'(?:\s*,\s*'[^']*')*)",
        re.IGNORECASE,
    )
    for m in pattern.finditer(sql):
        alias = m.group(1)
        col = m.group(2) if m.group(2) else m.group(3)
        for lit_m in re.finditer(r"'([^']*)'", m.group(4)):
            conditions.append((alias, col, lit_m.group(1)))
    return conditions


def _resolve_table(alias: str | None, col: str, alias_map: dict, db: BirdDatabase) -> str | None:
    if alias and alias in alias_map:
        return alias_map[alias]
    for t in db.list_tables():
        try:
            schema = db.get_schema([t])
            if re.search(rf"`?{re.escape(col)}`?", schema, re.IGNORECASE):
                return t
        except Exception:
            pass
    return None


def _fuzzy_match(literal: str, samples: list[Any]) -> tuple[str | None, float, str]:
    """Return (best_value, score, match_type).

    match_type: 'exact' (case-insensitive exact), 'substring' (one contains
    the other), 'fuzzy' (SequenceMatcher ratio >= 0.6).
    """
    lit_lower = literal.lower().strip()
    # 1. case-insensitive exact
    for s in samples:
        if isinstance(s, str) and s.lower().strip() == lit_lower:
            return s, 1.0, "exact"
    # 2. substring (one contains the other), require len >= 3
    if len(lit_lower) >= 3:
        best_sub: tuple[str, float] | None = None
        for s in samples:
            if not isinstance(s, str):
                continue
            sl = s.lower().strip()
            if lit_lower in sl or sl in lit_lower:
                ratio = SequenceMatcher(None, lit_lower, sl).ratio()
                if best_sub is None or ratio > best_sub[1]:
                    best_sub = (s, ratio)
        if best_sub and best_sub[1] >= 0.5:
            return best_sub[0], best_sub[1], "substring"
    # 3. fuzzy ratio
    best_fuzz: tuple[str, float] | None = None
    for s in samples:
        if not isinstance(s, str):
            continue
        ratio = SequenceMatcher(None, lit_lower, s.lower().strip()).ratio()
        if best_fuzz is None or ratio > best_fuzz[1]:
            best_fuzz = (s, ratio)
    if best_fuzz and best_fuzz[1] >= 0.8:
        return best_fuzz[0], best_fuzz[1], "fuzzy"
    return None, 0.0, "none"


def probe_values(sql: str, db: BirdDatabase, sample_limit: int = 200) -> dict[str, Any]:
    """Probe the DB for actual values of WHERE-clause string literals.

    Returns a dict with:
      - cell_values_text: formatted string for prompt injection
      - repairs: list of {table, column, old, new, score, match_type}
      - repaired_sql: SQL with literals replaced by best matches
    """
    alias_map = _extract_alias_map(sql)
    conditions = _extract_string_conditions(sql)
    if not conditions:
        return {"cell_values_text": "(no string literals found in WHERE clause)",
                "repairs": [], "repaired_sql": sql}

    repairs: list[dict[str, Any]] = []
    new_sql = sql
    cell_lines: list[str] = []
    for alias, col, literal in conditions:
        table = _resolve_table(alias, col, alias_map, db)
        if not table:
            cell_lines.append(f"{col}: (table not found)")
            continue
        try:
            samples = db.get_column_samples(table, col, limit=sample_limit)
        except Exception:
            cell_lines.append(f"{table}.{col}: (lookup error)")
            continue
        # always show actual values for context
        shown = [str(s) for s in samples[:10]]
        best, score, mtype = _fuzzy_match(literal, samples)
        if best is not None and best != literal:
            repairs.append({"table": table, "column": col, "old": literal,
                            "new": best, "score": round(score, 3), "match_type": mtype})
            new_sql = new_sql.replace(f"'{literal}'", f"'{best}'")
            cell_lines.append(f"{table}.{col} (draft used '{literal}'): "
                              f"actual values include {shown} -> best match: '{best}' ({mtype})")
        elif best == literal:
            cell_lines.append(f"{table}.{col} (draft used '{literal}'): "
                              f"value matches DB exactly; actual values include {shown}")
        else:
            cell_lines.append(f"{table}.{col} (draft used '{literal}'): "
                              f"no good match; actual values include {shown}")

    return {
        "cell_values_text": "\n".join(cell_lines),
        "repairs": repairs,
        "repaired_sql": new_sql,
    }
