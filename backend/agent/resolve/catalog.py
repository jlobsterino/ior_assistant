"""
catalog — загрузка реального каталога значений витрины.

Источники (сняты с реальной витрины value_catalog_probe):
   schema/kb_value_catalog.json - distinct-значения по категориальным колонкам + filled_pct.
   schema/kb_value_index.json   - inverted: lower(value) -> [{column, value, count}].

Всё кэшируется (lru_cache). Префикс таблицы main опускаем до короткого имени колонки.
"""
from __future__ import annotations

import functools
import json
from pathlib import Path

_SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schema"
MAIN_TABLE = "d6_base_of_knowledge_ior"


@functools.lru_cache(maxsize=1)
def load_catalog() -> dict:
    return json.loads((_SCHEMA_DIR / "kb_value_catalog.json").read_text(encoding="utf-8"))


@functools.lru_cache(maxsize=1)
def load_index() -> dict:
    return json.loads((_SCHEMA_DIR / "kb_value_index.json").read_text(encoding="utf-8"))


def split_col(full: str) -> tuple[str, str]:
    """'d6_base_of_knowledge_ior.org_struct_lvl_3_name' -> ('d6_base_of_knowledge_ior', 'org_struct_lvl_3_name')"""
    table, _, col = full.rpartition(".")
    return table, col


def is_main(full: str) -> bool:
    return full.startswith(MAIN_TABLE + ".")