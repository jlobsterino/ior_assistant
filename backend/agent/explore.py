"""
explore - обзор базы знаний для пользователя (П7): таблицы, колонки, справочники.

Чтобы аудитор формулировал запрос предметно, а не угадывал. Берём структуру из
schema-загрузчика и реальные значения из каталога (kb_value_catalog) - без Spark,
быстро. Офлайн-безопасный модуль.
"""
from __future__ import annotations

from typing import Optional

from backend.agent.resolve.catalog import load_catalog
from backend.agent.schema import get_schema
from backend.agent.status import human_table


def schema_overview() -> list:
    """[{table, title, rows, columns:[{name, type, filled_pct, has_values}]}]"""
    schema = get_schema()
    cat = load_catalog().get("columns", {})
    out: list = []
    for name, t in schema.tables.items():
        cols = []
        for c in t.columns:
            ci = cat.get(f"{name}.{c.name}") or {}
            cols.append({
                "name": c.name, "type": c.type,
                "filled_pct": c.filled_pct if c.filled_pct is not None else ci.get("filled_pct"),
                "has_values": bool(ci.get("values")),
            })
        out.append({"table": name, "title": human_table(name),
                    "rows": t.row_count, "columns": cols})
    return out


def column_values(table: str, column: str, limit: int = 100,
                  contains: Optional[str] = None) -> dict:
    """Справочник значений колонки из каталога (реальные distinct + counts)."""
    cat = load_catalog().get("columns", {})
    info = cat.get(f"{table}.{column}") or {}
    vals = info.get("values") or []
    counts = info.get("counts") or {}
    if contains:
        sub = contains.lower()
        vals = [v for v in vals if sub in str(v).lower()]
    items = [{"value": str(v), "count": counts.get(v, counts.get(str(v)))}
             for v in vals[:limit]]
    return {"table": table, "column": column,
            "filled_pct": info.get("filled_pct"),
            "values": items, "total": len(info.get("values") or []),
            "shown": len(items)}