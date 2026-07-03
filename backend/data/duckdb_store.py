"""
DuckDBStore - local backend для разработки на личном MacBook.

DuckDB – embedded аналитическая БД (как SQLite, но колоночная +
прекрасно жуёт SQL ANSI). Размер базы для нашего объёма (5 таблиц
* 10-100k строк * ~80 колонок) – единицы МБ.

База генерится отдельным скриптом scripts/gen_local_data.py (читает
backend/agent/schema/kb_schema.yaml и заполняет реалистичными данными).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import pandas as pd

from backend.agent.schema import get_schema
from backend.data.base import build_where_clauses

logger = logging.getLogger(__name__)

_DEFAULT_DB_PATH = os.environ.get(
    "LOCAL_DB_PATH",
    str(Path(__file__).resolve().parents[2] / "data" / "local_kb.duckdb"),
)


class DuckDBStore:
    """Embedded DuckDB. Один файл – `data/local_kb.duckdb`."""

    def __init__(self, db_path: Optional[str] = None) -> None:
        self.db_path = db_path or _DEFAULT_DB_PATH
        self._conn = None
        self._schema = get_schema()

    def _get_conn(self):
        if self._conn is not None:
            return self._conn
        import duckdb

        p = Path(self.db_path)
        if not p.exists():
            raise FileNotFoundError(
                f"Local DuckDB не найдена: {p}.\n"
                f"Сгенерируй её: python scripts/gen_local_data.py"
            )
        # read_only=True – БД только читается (агент в неё не пишет, она
        # генерится отдельным скриптом). DuckDB разрешает МНОГО read-only
        # коннектов одновременно – сервер + тесты/инструменты не конфликтуют
        # за file-lock (read_only=False позволяет только один процесс).
        self._conn = duckdb.connect(str(p), read_only=True)
        logger.info("[DuckDBStore] connected (read-only): %s", p)
        return self._conn

    def list_tables(self) -> list[str]:
        return self._schema.table_names()

    def get_table_schema(self, table: str) -> dict:
        t = self._schema.get(table)
        if t is None:
            return {}
        return {c.name: c.type for c in t.columns}

    def describe_table(self, table: str) -> list[tuple[str, str]]:
        """Список (имя_колонки, тип) из реальной таблицы. Для schema-кроулера.
        table – короткое имя (DuckDB) либо full_name (берём последний сегмент)."""
        short = table.split(".")[-1]
        conn = self._get_conn()
        rows = conn.execute(f"PRAGMA table_info({short})").fetchall()
        # PRAGMA table_info: (cid, name, type, notnull, dflt, pk)
        return [(r[1], str(r[2]).lower()) for r in rows]

    def list_db_tables(self, prefix: str = "d6_") -> list[str]:
        """Реальные таблицы в БД с префиксом. Для авто-дискавери."""
        conn = self._get_conn()
        rows = conn.execute("SHOW TABLES").fetchall()
        return [r[0] for r in rows if r[0].startswith(prefix)]

    def query(
        self,
        *,
        table: str,
        where: Optional[dict] = None,
        columns: Optional[list[str]] = None,
        order_by: Optional[str] = None,
        order_desc: bool = True,
        limit: int = 100_000,
    ) -> pd.DataFrame:
        t = self._schema.get(table)
        if t is None:
            raise ValueError(f"таблица {table!r} нет в schema")
        col_types = {c.name: c.type for c in t.columns}
        valid = set(col_types)

        if columns:
            bad = [c for c in columns if c not in valid]
            if bad:
                raise ValueError(f"колонки {bad} нет в {table}")

        # LLM иногда передаёт "col DESC" одной строкой – split + override order_desc
        if order_by is not None:
            if not isinstance(order_by, str):
                raise ValueError(
                    f"order_by должен быть string (имя колонки), "
                    f"получили {type(order_by).__name__}={order_by!r}"
                )
            parts = order_by.strip().split()
            if len(parts) == 2 and parts[1].upper() in ("DESC", "ASC"):
                order_by = parts[0]
                order_desc = parts[1].upper() == "DESC"
            if order_by not in valid:
                raise ValueError(f"order_by: колонки {order_by!r} нет в {table}")

        try:
            limit = min(
                int(limit) if limit is not None else 100_000, 2_000_000
            )
        except (TypeError, ValueError):
            limit = 100_000

        # Имя таблицы в DuckDB – короткое (без DB_PREFIX)
        select_cols = ", ".join(columns) if columns else "*"
        clauses = build_where_clauses(where, col_types)
        where_sql = " AND ".join(clauses) if clauses else "1=1"
        order_sql = ""
        if order_by:
            order_sql = f" ORDER BY {order_by} {'DESC' if order_desc else 'ASC'}"

        sql = (
            f"SELECT {select_cols} FROM {table} "
            f"WHERE {where_sql}{order_sql} LIMIT {limit}"
        )
        logger.info("[DuckDBStore] %s", sql)
        conn = self._get_conn()
        df = conn.execute(sql).fetchdf()
        try:
            df.columns = [str(c).lower() for c in df.columns]
        except Exception as e:
            logger.warning("[DuckDBStore] Failed to normalize columns: %s", e)
        return df

    def fetch_distinct_values(
        self,
        *,
        table: str,
        column: str,
        max_values: int = 50,
    ) -> list[str]:
        """SELECT DISTINCT column FROM table LIMIT max_values.
        Используется для enum-обогащения схема из РЕАЛЬНОЙ БД (не из YAML).
        """
        t = self._schema.get(table)
        if t is None:
            return []
        if column not in {c.name for c in t.columns}:
            return []
        sql = (
            f"SELECT DISTINCT {column} FROM {table} "
            f"WHERE {column} IS NOT NULL "
            f"ORDER BY {column} LIMIT {int(max_values)}"
        )
        conn = self._get_conn()
        rows = conn.execute(sql).fetchall()
        return [str(r[0]) for r in rows if r[0] is not None]

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None