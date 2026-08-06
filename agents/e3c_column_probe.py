#!/usr/bin/env python3
"""Column Grounding probe: diagnose SELECT-clause column-selection errors by
querying the DB for all candidate column names + samples, then semantically
matching question entities to the correct columns.

This is the deterministic probing layer (no LLM). It:
  - parses the SELECT clause of a draft SQL;
  - enumerates all columns of involved tables (PRAGMA table_info);
  - samples actual cell values for candidate columns;
  - matches question keywords to column names + sample values;
  - flags suspicious selections and suggests alternatives.

Symmetric to e3v_value_probe (which fixes WHERE values); this fixes SELECT columns.
"""
from __future__ import annotations

import re
from collections import defaultdict
from difflib import SequenceMatcher
from typing import Any

from tools.db_utils import BirdDatabase


# ── SQL parsing ────────────────────────────────────────────────────────

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


def _parse_select_columns(sql: str) -> list[dict]:
    """Parse SELECT clause into list of {raw, agg, col, alias, table, is_star, is_count_star}."""
    m = re.search(r"\bSELECT\s+(DISTINCT\s+)?(.*?)\s+FROM", sql, re.IGNORECASE | re.DOTALL)
    if not m:
        return []
    distinct = bool(m.group(1))
    raw_cols = m.group(2).strip()
    if raw_cols == "*":
        return [{"raw": "*", "is_star": True}]

    # split by comma at depth 0
    parts = []
    depth = 0
    cur = ""
    for ch in raw_cols:
        if ch == "(":
            depth += 1; cur += ch
        elif ch == ")":
            depth -= 1; cur += ch
        elif ch == "," and depth == 0:
            parts.append(cur.strip()); cur = ""
        else:
            cur += ch
    if cur.strip():
        parts.append(cur.strip())

    alias_map = _extract_alias_map(sql)
    result = []
    for part in parts:
        entry: dict[str, Any] = {"raw": part, "is_star": False}
        low = part.lower().strip()
        # COUNT(*)
        if re.match(r"count\s*\(\s*\*\s*\)", low):
            entry["is_count_star"] = True
            entry["agg"] = "COUNT"
            entry["col"] = "*"
        else:
            # aggregate?
            agg_m = re.match(r"(count|sum|avg|max|min)\s*\((.+?)\)", low)
            if agg_m:
                entry["agg"] = agg_m.group(1).upper()
                inner = agg_m.group(2).strip()
            else:
                entry["agg"] = None
                inner = part.strip()
            # alias?
            as_m = re.search(r"\bAS\b\s+(.+)$", part, re.IGNORECASE)
            if as_m:
                entry["alias"] = as_m.group(1).strip()
                inner_raw = re.sub(r"\bAS\b\s+.+$", "", part, flags=re.IGNORECASE).strip()
                inner = re.sub(r"\bAS\b\s+.+$", "", inner, flags=re.IGNORECASE).strip()
            else:
                entry["alias"] = None
            # table.col?
            tc_m = re.match(r"(`?\w+`?)\s*\.\s*`?([^`\s]+)`?", inner.strip())
            if tc_m:
                tbl_alias = tc_m.group(1).strip("`")
                entry["table"] = alias_map.get(tbl_alias, tbl_alias)
                entry["col"] = tc_m.group(2).strip("`")
            else:
                entry["col"] = inner.strip().strip("`")
                entry["table"] = None
            # concatenation?
            if "||" in part:
                entry["is_concat"] = True
        result.append(entry)
    return result


def _get_all_columns(db: BirdDatabase, table: str) -> list[str]:
    """Get all column names of a table via PRAGMA."""
    with db._connection() as conn:
        rows = conn.execute(f"PRAGMA table_info(`{table}`)").fetchall()
    return [r[1] for r in rows]


def _semantic_match(question_word: str, col_name: str, samples: list) -> float:
    """Score how well a question keyword matches a column (name + sample values)."""
    qw = question_word.lower().strip()
    cn = col_name.lower().strip()
    # exact substring in column name
    if qw in cn or cn in qw:
        return 1.0
    # fuzzy on column name
    ratio = SequenceMatcher(None, qw, cn).ratio()
    if ratio >= 0.6:
        return ratio
    # check sample values (string type)
    for s in samples[:5]:
        if isinstance(s, str) and qw in s.lower():
            return 0.7
    return ratio * 0.5


def probe_columns(sql: str, question: str, db: BirdDatabase) -> dict[str, Any]:
    """Diagnose SELECT column selection and suggest corrections.

    Returns:
      - report_text: formatted report for prompt injection
      - suggestions: list of {current_col, suggested_col, reason, score}
      - has_issues: whether any column selection looks suspicious
    """
    select_cols = _parse_select_columns(sql)
    if not select_cols:
        return {"report_text": "(could not parse SELECT clause)",
                "suggestions": [], "has_issues": False}

    alias_map = _extract_alias_map(sql)
    # all tables in FROM/JOIN
    tables = list(dict.fromkeys(
        re.findall(r"(?:\bFROM\b|\bJOIN\b)\s+`?([A-Za-z_][A-Za-z0-9_]*)`?", sql, re.IGNORECASE)
    ))

    # gather all columns per table
    all_cols: dict[str, list[str]] = {}
    for t in tables:
        try:
            all_cols[t] = _get_all_columns(db, t)
        except Exception:
            all_cols[t] = []

    # question keywords (lowercase words >= 3 chars, excluding common words)
    stop = {"the", "how", "many", "what", "which", "list", "show", "find",
            "all", "are", "for", "and", "with", "that", "have", "from",
            "where", "their", "each", "this", "these", "those", "than",
            "less", "more", "between", "into", "was", "were", "has", "had",
            "been", "being", "does", "did", "was", "also", "not", "but",
            "they", "them", "his", "her", "its", "our", "you", "your",
            "please", "state", "give", "provide", "return", "number",
            "total", "average", "sum", "count", "highest", "lowest",
            "maximum", "minimum", "most", "least", "top", "bottom",
            "first", "last", "name", "names", "of", "in", "on", "at", "to",
            "is", "it", "a", "an", "by", "or", "if", "as", "be", "no",
            "out", "up", "do", "so", "we", "he", "she"}
    q_words = [w for w in re.findall(r"[A-Za-z_]+", question.lower()) if len(w) >= 3 and w not in stop]

    suggestions = []
    report_lines = []
    has_issues = False

    for entry in select_cols:
        if entry.get("is_star"):
            continue
        col = entry.get("col", "")
        if not col or col == "*":
            continue
        tbl = entry.get("table")

        # check if this column exists in the table
        if tbl and tbl in all_cols:
            cols_in_table = all_cols[tbl]
            col_clean = col.strip("`")
            if col_clean not in cols_in_table:
                # column doesn't exist! find best match
                best_col = None
                best_score = 0
                for c in cols_in_table:
                    s = SequenceMatcher(None, col_clean.lower(), c.lower()).ratio()
                    if s > best_score:
                        best_score = s; best_col = c
                if best_col and best_score >= 0.6:
                    suggestions.append({"current": col, "suggested": best_col,
                                        "table": tbl, "reason": "column not found, closest match",
                                        "score": round(best_score, 2)})
                    has_issues = True
                    report_lines.append(f"  Column '{tbl}.{col}' does not exist. Closest match: '{tbl}.{best_col}' (score {best_score:.2f})")
                continue

        # COUNT(*) check
        if entry.get("is_count_star"):
            # suggest COUNT(primary_key) — find first column with "id" or "code" in name
            pk_candidates = []
            for t in tables:
                for c in all_cols.get(t, []):
                    if any(k in c.lower() for k in ["id", "code", "cds"]):
                        pk_candidates.append(f"{t}.{c}")
            if pk_candidates:
                best_pk = pk_candidates[0]
                suggestions.append({"current": "COUNT(*)", "suggested": f"COUNT({best_pk})",
                                    "reason": "question asks 'how many X', use COUNT(entity_column) not COUNT(*)",
                                    "score": 0.8})
                has_issues = True
                report_lines.append(f"  COUNT(*) may overcount rows with NULLs. Consider COUNT({best_pk}).")
            continue

        # check for ambiguous column (same semantic, different name available)
        # e.g., pred uses MailCity but question says "city" → suggest City
        col_lower = col.strip("`").lower()
        for qw in q_words:
            # find all columns across tables that match this question word better
            for t in tables:
                for c in all_cols.get(t, []):
                    c_lower = c.lower()
                    # skip if same column
                    if c_lower == col_lower and t == tbl:
                        continue
                    cur_score = _semantic_match(qw, col_lower, [])
                    alt_score = _semantic_match(qw, c_lower, [])
                    # alternative matches significantly better
                    if alt_score > cur_score + 0.15 and alt_score >= 0.5:
                        # check not already suggested
                        key = f"{tbl}.{col}->{t}.{c}"
                        if not any(s.get("current") == col and s.get("suggested") == c for s in suggestions):
                            samples = []
                            try:
                                samples = db.get_column_samples(t, c, limit=3)
                            except Exception:
                                pass
                            suggestions.append({"current": col, "suggested": c,
                                                "table": t, "reason": f"question mentions '{qw}', '{c}' matches better than '{col}'",
                                                "score": round(alt_score, 2), "samples": [str(s) for s in samples[:3]]})
                            has_issues = True
                            report_lines.append(
                                f"  Question mentions '{qw}'. Current column '{col}' "
                                f"(score {cur_score:.2f}); alternative '{t}.{c}' "
                                f"(score {alt_score:.2f}), samples: {[str(s) for s in samples[:3]]}")
                        break

    # ── Post-loop checks: aggregation, DISTINCT, extra columns ──────

    q_lower = question.lower()

    # Check: COUNT(*) anywhere in SELECT (catches complex expressions too)
    select_raw = ""
    sm = re.search(r"\bSELECT\s+(.*?)\s+FROM", sql, re.IGNORECASE | re.DOTALL)
    if sm:
        select_raw = sm.group(1)
    if re.search(r"count\s*\(\s*\*\s*\)", select_raw, re.IGNORECASE):
        already_caught = any(e.get("is_count_star") for e in select_cols)
        if not already_caught:
            # find best entity column
            pk_candidates = []
            for t in tables:
                for c in all_cols.get(t, []):
                    if any(k in c.lower() for k in ["id", "code", "cds", "name"]):
                        pk_candidates.append(f"{t}.{c}")
            if pk_candidates:
                best_pk = pk_candidates[0]
                suggestions.append({"current": "COUNT(*)", "suggested": f"COUNT({best_pk})",
                                    "reason": "COUNT(*) includes NULL rows; COUNT(entity_column) is more precise",
                                    "score": 0.8})
                has_issues = True
                report_lines.append(f"  COUNT(*) found in SELECT. Consider COUNT({best_pk}) instead.")

    # Check: missing aggregation (question asks "lowest/highest/average" but no agg in SELECT)
    agg_keywords = {
        "lowest": "MIN", "minimum": "MIN", "smallest": "MIN", "least": "MIN",
        "highest": "MAX", "maximum": "MAX", "largest": "MAX", "biggest": "MAX",
        "average": "AVG", "mean": "AVG",
        "total": "SUM", "sum of": "SUM",
    }
    has_agg_in_select = any(e.get("agg") for e in select_cols)
    for kw, agg_fn in agg_keywords.items():
        if kw in q_lower and not has_agg_in_select:
            # find the column the question is asking about
            for e in select_cols:
                if e.get("col") and e["col"] != "*":
                    suggestions.append({
                        "current": e["col"], "suggested": f"{agg_fn}({e['col']})",
                        "reason": f"question asks '{kw}', should use {agg_fn}()",
                        "score": 0.9})
                    has_issues = True
                    report_lines.append(
                        f"  Question asks '{kw}' but SELECT has no aggregation. "
                        f"Consider {agg_fn}({e['col']}).")
                    break
            break

    # Check: missing DISTINCT (gold often uses DISTINCT for "different"/"unique")
    if any(w in q_lower for w in ["different", "unique", "distinct"]) and \
       not any("distinct" in e.get("raw","").lower() for e in select_cols):
        has_issues = True
        report_lines.append(
            "  Question asks for 'different/unique' results. "
            "Consider adding DISTINCT to the SELECT clause.")

    # Check: extra columns (pred has more columns than question requests)
    # Count question-requested entities vs SELECT columns
    n_select = len([e for e in select_cols if not e.get("is_star")])
    if n_select > 1:
        # crude: if question is a simple "what is the X" (singular), shouldn't have many columns
        if re.search(r'\bwhat is\b|\bphone number\b|\bemail\b', q_lower) and n_select > 2:
            has_issues = True
            report_lines.append(
                f"  SELECT has {n_select} columns but the question seems to ask for "
                f"a single value. Consider removing unnecessary columns.")

    # build report
    if not report_lines:
        report_text = "No column-selection issues detected."
    else:
        report_text = "\n".join(report_lines)

    return {
        "report_text": report_text,
        "suggestions": suggestions[:8],
        "has_issues": has_issues,
        "select_cols": select_cols,
        "all_tables": tables,
    }
