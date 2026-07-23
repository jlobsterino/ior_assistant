"""
IORDataStore - abstract interface для доступа к БД ИОР.

Спецификация:
 • query(table, where, columns?, order_by?, limit) -> pandas.DataFrame
 • get_table_schema(table) -> dict (для валидации в tools)
 • list_tables() -> list[str]
 • close() – для cleanup

Backend'ы (SparkHiveStore / DuckDBStore) реализуют только эти методы.
Tools никогда напрямую не дёргают Spark или DuckDB.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

import pandas as pd


@dataclass
class WhereClause:
    """Промежуточное представление WHERE-условия - компилируется в SQL
    разными backend'ами по-разному (Spark vs DuckDB).

    Поддерживаемые операторы:
      {col: value}                    -> col = value (или IS NULL)
      {col: [v1, v2]}                 -> col IN (...)
      {col: {">=": x, "<": y}}        -> range
      {col: {"like": "%pattern%"}}    -> LIKE
      {col__like: "%pattern%"}        -> shortcut того же
      {"_or": [WhereClause, WhereClause]} -> OR (зарезервировано)
    """

    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dict(self.raw)


class IORDatastore(Protocol):
    """Контракт data-backend'а."""

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
        """SELECT из одной таблицы. Возвращает pandas DataFrame.

        В обоих backend'ах:
         • колонки валидируются по schema
         • limit с hard-cap (max 2M)
         • where конвертится в SQL-предикаты единообразно
        """
        ...

    def list_tables(self) -> list[str]:
        """Короткие имена таблиц БД."""
        ...

    def get_table_schema(self, table: str) -> dict:
        """{column_name: dtype} – для валидации параметров в tools."""
        ...

    def close(self) -> None:
        """Cleanup (DuckDB закрывает connection, Spark – никогда не stop'ит
        – singleton живёт всю жизнь сервера)."""
        ...


# ----- Общие helpers для конвертации where -> SQL ------------------------


def normalize_where(where: Optional[dict]) -> list[tuple[str, str, Any]]:
    """Распаковывает `where` в плоский список (column, op, value)."""
    out: list[tuple[str, str, Any]] = []
    if not where:
        return out
    for key, val in where.items():
        if key == "_or":
            # OR-блок – пока упрощённо
            continue
        if "__" in key:
            col, op_alias = key.rsplit("__", 1)
            mapping = {
                "like": "like",
                "gt": ">",
                "gte": ">=",
                "lt": "<",
                "lte": "<=",
                "ne": "!=",
                "eq": "=",
            }
            op = mapping.get(op_alias, "=")
            out.append((col, op, val))
            continue
        if isinstance(val, dict):
            for op, v in val.items():
                out.append((key, op.lower(), v))
        elif isinstance(val, list):
            out.append((key, "in", val))
        elif val is None:
            out.append((key, "is", None))
        else:
            out.append((key, "=", val))
    return out


def sql_value(v: Any, col_type: str = "string") -> str:
    """Литерал для SQL. Универсально для Spark и DuckDB."""
    if v is None:
        return "NULL"
    t = col_type.lower()
    if "date" in t or "timestamp" in t:
        return "'" + str(v).replace("'", "''") + "'"
    if isinstance(v, str) or "string" in t or "varchar" in t:
        return "'" + str(v).replace("'", "''") + "'"
    return str(v)


def build_where_clauses(
    where: Optional[dict], col_types: dict[str, str]
) -> list[str]:
    """Превращает normalized where в SQL predicates."""
    clauses: list[str] = []
    op_map = {
        "gt": ">", "gte": ">=", "lt": "<", "lte": "<=", "ne": "!=", "eq": "=",
        "like": "like", "in": "in", "is": "is"
    }
    for col, op, val in normalize_where(where):
        op = op_map.get(op.lower(), op)
        col_type = col_types.get(col, "string")
        
        # Специальный маппинг для обращений клиентов (Раздел 11.5 ИОР_mapping_разделы.md)
        if col == "src_type_lvl_2_name" and isinstance(val, str) and "\u043e\u0431\u0440\u0430\u0449\u0435\u043d" in val.lower():
            clauses.append(
                "(incdnt_detection_person_name = '\u041a\u043b\u0438\u0435\u043d\u0442' "
                "OR src_type_lvl_2_name LIKE '%\u043e\u0431\u0440\u0430\u0449\u0435\u043d\u0438%' "
                "OR incdnt_source_name LIKE '%\u043a\u043b\u0438\u0435\u043d\u0442%')"
            )
            continue

        if op == "in":
            vals = ", ".join(sql_value(v, col_type) for v in val)
            clauses.append(f"{col} IN ({vals})")
        elif op == "is" and val is None:
            clauses.append(f"{col} IS NULL")
        elif op == "like":
            clauses.append(f"{col} LIKE {sql_value(val, 'string')}")
        elif op in (">", ">=", "<", "<=", "=", "!=", "<>"):
            clauses.append(f"{col} {op} {sql_value(val, col_type)}")
    return clauses