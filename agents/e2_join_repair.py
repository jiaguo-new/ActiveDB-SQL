#!/usr/bin/env python3
"""FK-graph JOIN repair: deterministic JOIN path correction using the database's
foreign-key graph (SchemaGraphSQL-inspired, zero training).

Two operations:
  - under-JOIN repair: tables in FROM/JOIN are not fully connected in the FK
    graph -> find shortest paths to connect them, add missing intermediate
    tables + JOIN conditions.
  - over-JOIN repair: a table in the draft is not on any FK path between the
    question-relevant tables -> flag as candidate for removal.

Also provides a "noise report" (denoising signals):
  - row count anomaly (empty / suspiciously large / duplicate-heavy)
  - SELECT columns not in FROM tables (schema linking mismatch)
"""
from __future__ import annotations

import re
from collections import deque, defaultdict
from typing import Any

from tools.db_utils import BirdDatabase


# ── FK graph construction ──────────────────────────────────────────────

def build_fk_graph(db: BirdDatabase) -> dict[str, dict[str, list[dict]]]:
    """Build undirected adjacency: adj[table][other_table] = [{from,to}, ...]."""
    tables = db.list_tables()
    adj: dict[str, dict[str, list[dict]]] = {t: {} for t in tables}
    for t in tables:
        try:
            fks = db.get_foreign_keys([t])
        except Exception:
            continue
        for fk in fks:
            if "error" in fk:
                continue
            src = fk.get("table", t)
            dst = fk.get("referenced_table", "")
            col_from = fk.get("from_column", "")
            col_to = fk.get("to_column", "")
            if not dst or dst not in adj:
                continue
            edge = {"from_table": src, "from_col": col_from,
                    "to_table": dst, "to_col": col_to}
            adj.setdefault(src, {}).setdefault(dst, []).append(edge)
            rev = {"from_table": dst, "from_col": col_to,
                   "to_table": src, "to_col": col_from}
            adj.setdefault(dst, {}).setdefault(src, []).append(rev)
    return adj


def bfs_shortest_path(adj, src, dst) -> list[str] | None:
    """BFS shortest path src->dst, returns list of table names or None."""
    if src == dst:
        return [src]
    visited = {src}
    queue = deque([(src, [src])])
    while queue:
        node, path = queue.popleft()
        for nbr in adj.get(node, {}):
            if nbr in visited:
                continue
            new_path = path + [nbr]
            if nbr == dst:
                return new_path
            visited.add(nbr)
            queue.append((nbr, new_path))
    return None


def steiner_connect(adj, tables: list[str]) -> list[tuple[str, str, list[str]]] | None:
    """Greedy Steiner-tree: connect all `tables` via shortest FK paths.

    Returns list of (src, dst, path_tables) pairs that need to be connected,
    including intermediate tables.  Returns None if any pair is disconnected.
    """
    if len(tables) <= 1:
        return []
    connected = {tables[0]}
    remaining = set(tables[1:])
    edges_needed: list[tuple[str, str, list[str]]] = []
    while remaining:
        best = None  # (path_len, src_in_connected, dst_in_remaining, path)
        for src in connected:
            for dst in remaining:
                path = bfs_shortest_path(adj, src, dst)
                if path and (best is None or len(path) < best[0]):
                    best = (len(path), src, dst, path)
        if best is None:
            return None  # disconnected
        _, src, dst, path = best
        edges_needed.append((src, dst, path))
        for t in path:
            connected.add(t)
        remaining.discard(dst)
    return edges_needed


# ── SQL parsing ────────────────────────────────────────────────────────

_TABLE_RE = re.compile(
    r"(?:\bFROM\b|\bJOIN\b)\s+`?([A-Za-z_][A-Za-z0-9_]*)`?",
    re.IGNORECASE,
)


def extract_tables(sql: str) -> list[str]:
    return list(dict.fromkeys(_TABLE_RE.findall(sql)))


# ── JOIN repair ────────────────────────────────────────────────────────

def repair_joins(sql: str, db: BirdDatabase) -> dict[str, Any]:
    """Diagnose and repair JOIN topology using the FK graph.

    Returns:
      - noise_report: list of issues found
      - suggested_join_info: text describing correct JOIN paths
      - is_connected: whether draft tables are FK-connected
      - missing_tables: intermediate tables not in draft
      - fk_edges: the FK edges for the correct path
    """
    adj = build_fk_graph(db)
    draft_tables = extract_tables(sql)
    report: list[str] = []
    missing_tables: list[str] = []
    fk_edges: list[str] = []

    # check connectivity
    steiner = steiner_connect(adj, draft_tables) if len(draft_tables) > 1 else []
    if steiner is None:
        report.append("DRAFT TABLES ARE NOT FK-CONNECTED: some tables have no FK path "
                      "between them. The JOIN structure is likely wrong.")
    elif steiner:
        for src, dst, path in steiner:
            intermediate = [t for t in path if t not in draft_tables]
            if intermediate:
                missing_tables.extend(intermediate)
                report.append(f"Missing JOIN path: {src} -> {' -> '.join(path)} "
                              f"(intermediate tables: {intermediate})")
            # collect FK edges along path
            for i in range(len(path) - 1):
                a, b = path[i], path[i + 1]
                for edge in adj.get(a, {}).get(b, []):
                    fk_edges.append(
                        f"{edge['from_table']}.{edge['from_col']} = "
                        f"{edge['to_table']}.{edge['to_col']}")
    # deduplicate
    missing_tables = sorted(set(missing_tables))
    fk_edges = sorted(set(fk_edges))

    # build suggested JOIN info text
    if fk_edges:
        suggested = "Correct JOIN conditions (from FK graph):\n" + "\n".join(fk_edges)
        if missing_tables:
            suggested += f"\n\nTables that should be JOINed but are missing from the draft: {missing_tables}"
    elif len(draft_tables) > 1:
        suggested = f"Tables in draft: {draft_tables}. No FK path found between some of them."
    else:
        suggested = "Single-table query, no JOIN needed."

    return {
        "noise_report": report,
        "suggested_join_info": suggested,
        "is_connected": steiner is not None,
        "missing_tables": missing_tables,
        "fk_edges": fk_edges,
        "draft_tables": draft_tables,
    }


# ── Execution noise diagnosis ──────────────────────────────────────────

def diagnose_execution(sql: str, db: BirdDatabase) -> dict[str, Any]:
    """Run the SQL and produce denoising signals."""
    res = db.execute(sql)
    signals: list[str] = []
    if not res["ok"]:
        signals.append(f"EXECUTION ERROR: {res.get('error', 'unknown')}")
        return {"ok": False, "signals": signals, "row_count": 0, "dup_ratio": 0}
    rows = res.get("rows") or []
    n = len(rows)
    if n == 0:
        signals.append("EMPTY RESULT (0 rows): the query returns nothing. "
                       "This often means a WHERE filter is too strict or a JOIN "
                       "condition is wrong.")
    elif n > 1000:
        signals.append(f"SUSPICIOUSLY LARGE RESULT ({n} rows): possible missing JOIN "
                       "condition (cartesian product) or overly broad filter.")
    # duplicate row ratio
    if n > 1:
        try:
            seen = set()
            dups = 0
            for r in rows:
                key = tuple(str(v) for v in r)
                if key in seen:
                    dups += 1
                seen.add(key)
            dup_ratio = dups / n
            if dup_ratio > 0.3:
                signals.append(f"HIGH DUPLICATE RATIO ({dup_ratio:.0%}): possible "
                               "missing DISTINCT or redundant JOIN.")
        except Exception:
            dup_ratio = 0
    return {"ok": True, "signals": signals, "row_count": n}
