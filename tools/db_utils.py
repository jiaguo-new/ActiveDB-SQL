"""Minimal read-only database tool set for BIRD/Sqlite evaluation."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class BirdDatabase:
    """Read-only SQLite wrapper for a single BIRD database."""

    def __init__(self, db_id: str, db_root: Path | str, timeout: float = 30.0, max_rows: int = 100):
        self.db_id = db_id
        self.db_root = Path(db_root)
        self.timeout = timeout
        self.max_rows = max_rows
        self.db_path = self._find_db_file()

    def _find_db_file(self) -> Path:
        candidates = [
            self.db_root / self.db_id / f"{self.db_id}.sqlite",
            self.db_root / self.db_id / f"{self.db_id}.db",
            self.db_root / f"{self.db_id}.sqlite",
            self.db_root / f"{self.db_id}.db",
        ]
        for c in candidates:
            if c.exists():
                return c
        raise FileNotFoundError(f"No SQLite file found for db_id={self.db_id} under {self.db_root}")

    def _connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
        return conn

    def list_tables(self) -> list[str]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        return [r[0] for r in rows]

    def get_schema(self, tables: list[str] | None = None) -> str:
        with self._connection() as conn:
            if tables is None:
                tables = self.list_tables()
            parts = []
            for table in tables:
                try:
                    ddl = conn.execute(
                        f"SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
                    ).fetchone()
                    if ddl and ddl[0]:
                        parts.append(ddl[0])
                except Exception as e:
                    parts.append(f"-- error reading {table}: {e}")
        return "\n".join(parts)

    def execute(self, sql: str) -> dict[str, Any]:
        """Execute a read-only query and return results with metadata."""
        result: dict[str, Any] = {"sql": sql, "ok": False, "rows": None, "error": None, "truncated": False}
        # Safety: block DML/DDL
        upper = sql.strip().upper()
        if not upper.startswith("SELECT"):
            result["error"] = "Only SELECT statements are allowed."
            return result
        try:
            with self._connection() as conn:
                cur = conn.execute(sql)
                rows = cur.fetchmany(self.max_rows + 1)
                if len(rows) > self.max_rows:
                    rows = rows[: self.max_rows]
                    result["truncated"] = True
                result["rows"] = rows
                result["ok"] = True
        except Exception as e:
            result["error"] = str(e)
        return result

    def get_foreign_keys(self, tables: list[str] | None = None) -> list[dict[str, Any]]:
        """Return foreign-key relationships for the given tables."""
        with self._connection() as conn:
            if tables is None:
                tables = self.list_tables()
            fks = []
            for table in tables:
                try:
                    rows = conn.execute(f"PRAGMA foreign_key_list(`{table}`)").fetchall()
                    for r in rows:
                        fks.append({
                            "table": table,
                            "id": r[0],
                            "seq": r[1],
                            "referenced_table": r[2],
                            "from_column": r[3],
                            "to_column": r[4],
                        })
                except Exception as e:
                    fks.append({"table": table, "error": str(e)})
        return fks

    def get_column_samples(
        self, table: str, column: str, limit: int = 5
    ) -> list[Any]:
        """Return a few distinct sample values from a column."""
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT DISTINCT `{column}` FROM `{table}` WHERE `{column}` IS NOT NULL LIMIT ?",
                (limit,),
            ).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------
    # Schema-Linking helpers (E2v2): column-level pruning + FK closure
    # ------------------------------------------------------------------

    def get_table_columns(self, table: str) -> list[dict[str, str]]:
        """Return [{name, type}] for a table via PRAGMA table_info."""
        with self._connection() as conn:
            try:
                rows = conn.execute(f"PRAGMA table_info(`{table}`)").fetchall()
            except Exception:
                return []
        # PRAGMA table_info: cid, name, type, notnull, dflt_value, pk
        return [{"name": r[1], "type": r[2] or ""} for r in rows]

    def get_schema_subset(
        self,
        tables: list[str],
        columns: dict[str, list[str]] | None = None,
    ) -> str:
        """Build a compact column-level DDL for only the selected tables/columns.

        Emits one ``CREATE TABLE t (col TYPE, ...);`` line per table, restricted
        to the requested columns (falls back to all columns when a table is not
        in *columns* or its column list is empty).  Non-existent tables/columns
        are silently dropped.  This is the core noise-reduction primitive for
        schema-linking: the stage-2 generator only sees relevant columns.
        """
        columns = columns or {}
        all_tables = set(self.list_tables())
        parts: list[str] = []
        for table in tables:
            if table not in all_tables:
                continue
            table_cols = self.get_table_columns(table)
            if not table_cols:
                continue
            wanted = columns.get(table, [])
            if wanted:
                # keep only requested columns that actually exist, preserve order
                wanted_set = set(wanted)
                table_cols = [c for c in table_cols if c["name"] in wanted_set]
                if not table_cols:
                    # requested columns none exist -> fall back to all (avoid empty)
                    table_cols = self.get_table_columns(table)
            col_defs = ", ".join(f"`{c['name']}` {c['type']}".strip() for c in table_cols)
            parts.append(f"CREATE TABLE `{table}` ({col_defs});")
        return "\n".join(parts)

    def fk_closure(self, seed_tables: list[str]) -> list[str]:
        """One-hop foreign-key closure: add tables reachable via FK from seed.

        Deterministic, compliance-safe (only reads PRAGMA).  Prevents broken
        join paths when the stage-1 selection omits an intermediate table.
        """
        all_fks = self.get_foreign_keys()
        seed = set(seed_tables)
        frontier = set(seed)
        while frontier:
            nxt: set[str] = set()
            for fk in all_fks:
                t = fk.get("table")
                rt = fk.get("referenced_table")
                if t in frontier and rt and rt not in seed:
                    nxt.add(rt)
                if rt in frontier and t and t not in seed:
                    nxt.add(t)
            seed |= nxt
            frontier = nxt
        # preserve original seed order (only real tables), then sorted additions
        all_tables_set = set(self.list_tables())
        ordered = [t for t in seed_tables if t in all_tables_set]
        ordered += sorted(seed - set(seed_tables))
        return ordered
