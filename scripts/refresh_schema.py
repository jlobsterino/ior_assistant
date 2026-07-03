#!/usr/bin/env python3
"""
refresh_schema.py - авто-генерация kb_schema.yaml из реальной БД.

Решает проблему масштабирования: при добавлении новых таблиц в БЗ
(employee, ВНД, кредитные витрины и т.д.) НЕ нужно руками править YAML.
Краулер берёт DESCRIBE TABLE из Hive (prod) или PRAGMA table_info (local
DuckDB), вытаскивает колонки+типы и генерит/обновляет kb_schema.yaml.

КЛЮЧЕВОЕ: MERGE-режим (по умолчанию). Авто-поля (колонки, типы) берутся
из БД, а РУЧНЫЕ метаданные (foreign_keys, description, enum_values,
common_filters, ready_aggregates_in_main) СОХРАНЯЮТСЯ из текущего YAML.
Так краулер подхватывает новые колонки/таблицы, не затирая курируемую
семантику связей.

Использование:
  # prod (Spark/Hive) – обновить схему по реальным таблицам:
  APP_ENV=prod DATA_BACKEND=spark python scripts/refresh_schema.py

  # local (DuckDB) – то же по синтетике:
  python scripts/refresh_schema.py

  # автодискавери всех d6_*-таблиц (а не только из текущего YAML):
  python scripts/refresh_schema.py --discover

  # с подсчётом enum-значений (медленнее, делает SELECT DISTINCT):
  python scripts/refresh_schema.py --with-enums

  # dry-run (показать что изменится, не писать):
  python scripts/refresh_schema.py --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from backend.agent.schema.loader import (  # noqa: E402
    _is_enum_candidate, _yaml_path, get_schema, reload_schema,
)
from backend.data import get_data_store  # noqa: E402

DM_PREFIX = "arnsdpsbx_t_team_sva_oarb_4."


def _load_existing_yaml() -> dict:
    p = _yaml_path()
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _col_to_dict(name: str, type_: str, existing_col: dict | None) -> dict:
    """Колонка для YAML: тип из БД, ручные поля (description/enum_values/
    filled_pct) сохраняются из существующего YAML."""
    out = {"name": name, "type": type_}
    if existing_col:
        for key in ("filled_pct", "description", "enum_values"):
            if existing_col.get(key) is not None:
                out[key] = existing_col[key]
    return out


def refresh(*, discover: bool, with_enums: bool, dry_run: bool, force: bool = False) -> int:
    store = get_data_store()
    existing = _load_existing_yaml()
    existing_tables = existing.get("tables") or {}

    # Guard: DuckDB даёт типы varchar/bigint вместо Hive string/decimal –
    # запись из local затёрла бы курируемые prod-типы. Краулер = prod-tool.
    is_duckdb = type(store).__name__ == "DuckDBStore"
    if is_duckdb and not dry_run and not force:
        print("⚠️ Backend = DuckDB (local). Типы будут DuckDB-flavored "
              "(varchar вместо string) — это затрёт prod-схему.\n"
              "   Для инспекции добавь --dry-run, для prod запусти с "
              "APP_ENV=prod DATA_BACKEND=spark.\n"
              "   Если точно хочешь записать из DuckDB – добавь --force.")
        return 2

    # Какие таблицы обновлять
    if discover and hasattr(store, "list_db_tables"):
        table_names = store.list_db_tables(prefix="d6_")
        # плюс те что уже в YAML (вдруг не d6_-префикс)
        table_names = sorted(set(table_names) | set(existing_tables.keys()))
        print(f"➡️ Автодискавери: {len(table_names)} таблиц с префиксом d6_")
    else:
        table_names = sorted(existing_tables.keys())
        print(f"➡️ Обновляю {len(table_names)} таблиц из текущего YAML")

    if not hasattr(store, "describe_table"):
        print(f"❌ Backend {type(store).__name__} не поддерживает describe_table")
        return 1

    new_tables: dict = {}
    n_new_cols = 0
    for tname in table_names:
        ex = existing_tables.get(tname, {})
        ex_cols = {c["name"]: c for c in (ex.get("columns") or [])}
        try:
            described = store.describe_table(tname)
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ {tname}: describe упал ({e}) — оставляю как было")
            if ex:
                new_tables[tname] = ex
            continue

        if not described:
            if ex:
                new_tables[tname] = ex
            continue

        cols = []
        for cname, ctype in described:
            if cname not in ex_cols:
                n_new_cols += 1
            cols.append(_col_to_dict(cname, ctype, ex_cols.get(cname)))

        # Опциональное обогащение enum'ами
        if with_enums:
            for c in cols:
                # реконструируем мини-объект для эвристики
                class _C:
                    pass
                cc = _C()
                cc.name, cc.type = c["name"], c["type"]
                cc.filled_pct = c.get("filled_pct")
                if c.get("enum_values") is None and _is_enum_candidate(cc):
                    try:
                        vals = store.fetch_distinct_values(
                            table=tname, column=c["name"], max_values=31
                        )
                        if vals and len(vals) <= 30:
                            c["enum_values"] = vals
                    except Exception:  # noqa: BLE001
                        pass

        new_tables[tname] = {
            "full_name": ex.get("full_name", DM_PREFIX + tname),
            "description": ex.get("description", f"Таблица {tname} (авто)"),
            "row_count": ex.get("row_count", 0),
            "columns": cols,
        }
        # Сохраняем ручные foreign_keys
        if ex.get("foreign_keys"):
            new_tables[tname]["foreign_keys"] = ex["foreign_keys"]
        print(f"✔️ {tname}: {len(cols)} колонок")

    # Собираем итоговый YAML, сохраняя секции common_filters / ready_aggregates
    result = {
        "tables": new_tables,
    }
    if existing.get("common_filters"):
        result["common_filters"] = existing["common_filters"]
    if existing.get("ready_aggregates_in_main"):
        result["ready_aggregates_in_main"] = existing["ready_aggregates_in_main"]

    header = (
        "# Auto-generated schema БЗ ИОР для Planner'а агента.\n"
        "# Сгенерировано scripts/refresh_schema.py из реальной БД.\n"
        "# Колонки/типы - авто из DESCRIBE. foreign_keys/description/\n"
        "# enum_values - курируемые вручную, сохраняются при re-run.\n\n"
    )
    body = yaml.safe_dump(result, allow_unicode=True, sort_keys=False,
                          default_flow_style=False, width=120)
    out_text = header + body

    print(f"\n➡️ Итого: {len(new_tables)} таблиц, {n_new_cols} новых колонок")
    if dry_run:
        print("\n=== DRY-RUN (не записываю). Первые 40 строк: ===")
        print("\n".join(out_text.splitlines()[:40]))
        return 0

    _yaml_path().write_text(out_text, encoding="utf-8")
    print(f"✔️ Записано: {_yaml_path()}")

    # Валидация – схема перечитывается
    reload_schema()
    s = get_schema()
    print(f"✔️ Валидация: {len(s.table_names())} таблиц загружено")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--discover", action="store_true",
                    help="автодискавери всех d6_* таблиц из БД (не только из YAML)")
    ap.add_argument("--with-enums", action="store_true",
                    help="подгрузить enum-значения (SELECT DISTINCT, медленнее)")
    ap.add_argument("--dry-run", action="store_true",
                    help="показать результат, не записывать")
    ap.add_argument("--force", action="store_true",
                    help="записать даже из DuckDB (типы будут DuckDB-flavored)")
    args = ap.parse_args()
    return refresh(discover=args.discover, with_enums=args.with_enums,
                   dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    sys.exit(main())