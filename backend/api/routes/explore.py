"""
Эндпоинты обзора базы знаний (П7): структура таблиц/колонок и справочники значений.
Быстро, из schema + каталога (без Spark).
"""

from __future__ import annotations

from fastapi import APIRouter

from backend.agent.explore import column_values, schema_overview

router = APIRouter(prefix="/api/explore", tags=["explore"])


@router.get("/schema")
def get_schema_overview():
    return {"tables": schema_overview()}


@router.get("/values")
def get_column_values(table: str, column: str, contains: str | None = None,
                      limit: int = 100):
    return column_values(table, column, limit=limit, contains=contains)