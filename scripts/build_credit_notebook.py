# Builder credit-калькулятора -> .ipynb.
#
# Собирает notebook расчёта потерь по кредиту из единого источника
# scripts/credit_calc_source.py (полные расчётные функции ФЛ/ЮЛ ММБ/ЮЛ КСБ,
# формулы РВПС/залога без изменений; I/O через PARAMS, без интерактива).
#
# Запуск: python scripts/build_credit_notebook.py
"""
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "scripts" / "credit_calc_source.py"
OUT = ROOT / "knowledge_base" / "notebooks" / "credit_no_way_collect_debt_v2.ipynb"


def md(text):
    return {"cell_type": "markdown", "metadata": {},
            "source": text.splitlines(keepends=True)}


def code(text, tags=None):
    meta = {"tags": tags} if tags else {}
    return {"cell_type": "code", "execution_count": None, "metadata": meta,
            "outputs": [], "source": text.rstrip("\n").splitlines(keepends=True)}


# — Логика из исходника: вырезаем хедер-заглушку (spark=None/PARAMS={}),
#   она в ноутбуке инжектится отдельной parameters-ячейкой + Spark init.
src = SRC.read_text(encoding="utf-8")
# отрезаем всё до первого справочника (digital_risk_profile)
marker = "# — Справочники"
logic = src[src.index(marker):]
# импорты, нужные логике (в ноутбуке Spark-ячейка их частично даёт)
logic = "from decimal import Decimal\nimport pandas as pd\nNDM = 'arnsdpsbx_t_team_sva_oarb_4.'\n\n" + logic

CELLS = []

CELLS.append(md(
    "# Расчёт потерь: невозможность взыскания задолженности по кредиту\n"
    "\n"
    "> Полные расчётные функции (ФЛ / ЮЛ ММБ / ЮЛ КСБ). I/O через `PARAMS` "
    "(без интерактивного ввода). Формулы РВПС/залога/коэффициентов — без \n"
    "изменений. Требует проверки на реальных кредитных витринах перед \n"
    "боевым использованием.\n"
))

CELLS.append(code(
    '# Параметры (papermill заменяет эту ячейку). Без интерактивного ввода.\n'
    'PARAMS = {\n'
    '    "client_type": "3",            # 1=ФЛ | 2=КСБ | 3=ММБ\n'
    '    "id_credit": "52802479370431", # ID кредита / номер договора КК\n'
    '    "incident_date": "2026-01-22", # ГГГГ-ММ-ДД\n'
    '    "risk_profile_code": "DRP-10047",\n'
    '    "deviation_code": "44",\n'
    '    "factor_codes": [],            # список кодов факторов ОР (1-4)\n'
    '    "drp_10027_type": None,        # "1"=Утрата | "2"=Неоформление\n'
    '    "zalog_overact_amount": None,  # сумма завышения залога\n'
    '    "vivod_sredstv_pct": None,     # % вывода средств\n'
    '    "vivod_sredstv_amount": None,  # сумма вывода средств\n'
    '}\n',
    tags=["parameters"],
))

CELLS.append(code(
    'import os, sys\n'
    'from pyspark.sql import SparkSession\n'
    'from pyspark import SparkContext, SparkConf, HiveContext\n'
    'import pyspark.sql.functions as f\n'
    'import pyspark.sql.types as t\n'
    'import pandas as pd\n'
    'import numpy as np\n'
    'import datetime\n'
    '\n'
    'conf = SparkConf()\n'
    'conf.setAll([\n'
    '    ("spark.hadoop.hive.mapred.support.subdirectories", "true"),\n'
    '    ("spark.hadoop.mapreduce.input.fileinputformat.input.dir.recursive", "true"),\n'
    '    ("spark.sql.hive.convertMetastoreParquet", "false"),\n'
    '    ("spark.sql.parquet.writeLegacyFormat", "true"),\n'
    '    ("spark.sql.shuffle.partitions", "200"),\n'
    '])\n'
    'spark = SparkSession.builder.config(conf=conf).enableHiveSupport().getOrCreate()\n'
    'print(\'Spark готов\')\n'
))

CELLS.append(md(
    "## Справочники и расчётные функции\n"
    "> Источник — `scripts/credit_calc_source.py` (единый файл логики, \n"
    "проходит py_compile). Формулы РВПС/залога без изменений.\n"
))

CELLS.append(code(logic))

CELLS.append(md("## Запуск расчёта и выгрузка результата\n"))
CELLS.append(code(
    "result_row = main(spark, PARAMS)\n"
    "\n"
    "# Выгрузка результата в Excel (рантайм run_preset ожидает .xlsx)\n"
    "_safe_id = str(PARAMS.get('id_credit', 'credit')).replace('/', '-')\n"
    "file_name = f'Расчёт потерь по кредиту {_safe_id}.xlsx'\n"
    "write_result_excel(result_row, file_name)\n"
))

CELLS.append(md("## Завершение\n"))
CELLS.append(code("spark.stop()\n"))

nb = {
    "cells": CELLS,
    "metadata": {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"name": "python", "version": "3.x"},
    },
    "nbformat": 4,
    "nbformat_minor": 5,
}

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(nb, ensure_ascii=False, indent=1), encoding="utf-8")
print(f"✓ Создан {OUT} ({len(CELLS)} ячеек)")