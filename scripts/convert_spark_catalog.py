#!/usr/bin/env python3
"""
convert_spark_catalog.py - конвертер JSON отчёта Spark/Hive в формат kb_value_catalog.json.

Использование:
  1. Выполните scripts/value_catalog_probe.ipynb на прод-кластере Spark (в DataLab).
  2. Скопируйте итоговый JSON-вывод в файл (например, spark_report.json).
  3. Запустите этот скрипт:
     python scripts/convert_spark_catalog.py spark_report.json
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_DIR = ROOT / "backend" / "agent" / "schema"

def main():
    if len(sys.argv) < 2:
        print("Использование: python scripts/convert_spark_catalog.py <путь_к_spark_report.json>")
        sys.exit(1)

    spark_path = Path(sys.argv[1])
    if not spark_path.exists():
        print(f"Ошибка: Файл {spark_path} не найден.")
        sys.exit(1)

    catalog_path = SCHEMA_DIR / "kb_value_catalog.json"

    print(f"Чтение отчёта Spark из: {spark_path}...")
    with open(spark_path, "r", encoding="utf-8") as f:
        report = json.load(f)

    catalog = {"columns": {}}
    columns_data = report.get("columns", {})

    print(f"Конвертация {len(columns_data)} колонок...")
    for full_col_name, info in columns_data.items():
        top_values = info.get("top_values", [])
        
        values = []
        counts = {}
        for item in top_values:
            val = str(item.get("value", ""))
            cnt = int(item.get("count", 0))
            if val:
                values.append(val)
                counts[val] = cnt

        catalog["columns"][full_col_name] = {
            "filled_pct": info.get("filled_pct", 100.0),
            "values": values,
            "counts": counts
        }

    # Записываем каталог
    with open(catalog_path, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)
    print(f"Успешно записан каталог в {catalog_path}")

    # Запускаем построение индекса
    from build_index_from_catalog import main as build_index
    print("Регенерация инвертированного индекса...")
    build_index()

if __name__ == "__main__":
    main()
