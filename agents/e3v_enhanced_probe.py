#!/usr/bin/env python3
"""Enhanced value probe: LIKE-pattern detection + date-format detection.

Extends e3v_value_probe with two new deterministic checks:
  1. LIKE pattern: if a WHERE literal doesn't exact-match any DB cell but
     a LIKE prefix/suffix would match, suggest changing = to LIKE.
  2. Date format: if a WHERE clause compares a date column against a string
     date literal (e.g., >= '1998-01-01'), and the gold-equivalent uses
     year()/strftime(), suggest the year() form.
"""
from __future__ import annotations

import re
from tools.db_utils import BirdDatabase
from agents.e3v_value_probe import probe_values, _extract_alias_map, _extract_string_conditions, _resolve_table


def probe_like_patterns(sql: str, question: str, db: BirdDatabase) -> dict:
    """Detect WHERE clauses where = should be LIKE."""
    alias_map = _extract_alias_map(sql)
    conditions = _extract_string_conditions(sql)
    suggestions = []
    report_lines = []

    for alias, col, literal in conditions:
        table = _resolve_table(alias, col, alias_map, db)
        if not table:
            continue
        try:
            samples = db.get_column_samples(table, col, limit=200)
        except Exception:
            continue
        # check if literal exact-matches any sample (case-insensitive)
        lit_lower = literal.lower().strip()
        exact = [s for s in samples if isinstance(s, str) and s.lower().strip() == lit_lower]
        if exact:
            continue  # exact match exists, = is fine
        # check if LIKE 'literal%' would match
        prefix_matches = [s for s in samples if isinstance(s, str) and s.lower().startswith(lit_lower)]
        if prefix_matches and len(prefix_matches) <= 20:
            suggestions.append({
                "table": table, "column": col, "literal": literal,
                "suggestion": f"Change {col} = '{literal}' to {col} LIKE '{literal}%'",
                "reason": f"'{literal}' not found exactly, but {len(prefix_matches)} values start with it",
                "matched_samples": [str(s) for s in prefix_matches[:3]],
            })
            report_lines.append(
                f"  WHERE {col} = '{literal}': no exact match in DB, but "
                f"{len(prefix_matches)} values start with '{literal}'. "
                f"Consider: {col} LIKE '{literal}%'")
        # check substring match
        else:
            substr_matches = [s for s in samples if isinstance(s, str) and lit_lower in s.lower()]
            if substr_matches and len(substr_matches) <= 20:
                suggestions.append({
                    "table": table, "column": col, "literal": literal,
                    "suggestion": f"Change {col} = '{literal}' to {col} LIKE '%{literal}%'",
                    "reason": f"'{literal}' found as substring in {len(substr_matches)} values",
                    "matched_samples": [str(s) for s in substr_matches[:3]],
                })
                report_lines.append(
                    f"  WHERE {col} = '{literal}': no exact match, but found as "
                    f"substring in {len(substr_matches)} values. Consider: "
                    f"{col} LIKE '%{literal}%'")

    has_issues = len(suggestions) > 0
    return {
        "report_text": "\n".join(report_lines) if report_lines else "No LIKE-pattern issues.",
        "suggestions": suggestions,
        "has_issues": has_issues,
    }


def probe_date_formats(sql: str, question: str, db: BirdDatabase) -> dict:
    """Detect date comparisons that might need year()/strftime() form."""
    # find date literals in WHERE
    date_lits = re.findall(r"'(\d{4}[-/]\d{1,2}[-/]\d{1,2})'", sql)
    if not date_lits:
        return {"report_text": "No date literals found.", "suggestions": [], "has_issues": False}

    # check if question mentions a year
    years = re.findall(r"\b(19\d{2}|20\d{2})\b", question)
    if not years:
        return {"report_text": "No year mentioned in question.", "suggestions": [], "has_issues": False}

    suggestions = []
    report_lines = []
    for year in years:
        # check if pred already has this year as a date literal
        has_date = any(year in d for d in date_lits)
        if has_date:
            report_lines.append(
                f"  Question mentions year {year}. The draft uses a full date "
                f"comparison (e.g., >= '{year}-01-01'). Consider using "
                f"CAST(strftime('%Y', date_column) AS INTEGER) = {year} or "
                f"strftime('%Y', date_column) = '{year}' instead, as this "
                f"handles date columns stored as text more robustly.")
            suggestions.append({"year": year, "suggestion": f"strftime('%Y', <date_col>) = '{year}'"})
    has_issues = len(suggestions) > 0
    return {
        "report_text": "\n".join(report_lines) if report_lines else "No date-format issues.",
        "suggestions": suggestions,
        "has_issues": has_issues,
    }
