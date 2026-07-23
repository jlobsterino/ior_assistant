#!/usr/bin/env python3
# --------------------------------------------------------------------------
# Генератор scripts/value_catalog_probe.ipynb
#
# Снимает с РЕАЛЬНОЙ витрины Spark «каталог значений» для резолвера запросов:
# по фильтр-релевантным КАТЕГОРИАЛЬНЫМ колонкам — реальные distinct-значения
# (топ по частоте) + доля заполнения (filled_pct) + кардинальность. Это нужно,
# чтобы пересобрать kb_schema и каталог из реальных данных (а не из синтетики)
# и научить инструмент определять «в какой колонке/уровне лежит это значение».
#
# Запуск: python scripts/build_value_catalog_notebook.py
# --------------------------------------------------------------------------
import json
import os

CELLS = []


def md(t):
    CELLS.append({"cell_type": "markdown", "metadata": {},
                  "source": t.splitlines(keepends=True)})


def code(t):
    CELLS.append({"cell_type": "code", "metadata": {}, "execution_count": None,
                  "outputs": [], "source": t.rstrip("\n").splitlines(keepends=True)})


# — Cell 0: заголовок + конфиденциальность ——————————————————————————————————————
md(r"""# Каталог значений витрины – для резолвера запросов ИОР-помощника

Снимает **справочники категорий** по фильтр-релевантным колонкам реальной витрины,
чтобы пересобрать `kb_schema` и каталог значений из реальных данных.

## ⚠ Конфиденциальность – что собирается, а что НЕТ

**Собирается (безопасно прислать):**
- доля заполнения колонок (`filled_pct` – агрегат),
- кардинальность (`approx_count_distinct` – число),
- **топ-N значений категорий-справочников** (оргструктура, процессы, типы событий,
  профили риска, виды потерь, статусы) + их частоты — это перечни-классификаторы.

**НЕ собирается:**
- ни клиентских/персональных данных, ни ФИО (`incdnt_detection_person_name` исключена),
- ни текстов описаний (`incdnt_summary_descr_txt`/`full_descr_txt`),
- ни сумм/ID/номеров счетов/документов (по суммам – только % заполнения).

В конце печатается один **JSON-блок** – скопируй его целиком и пришли.

### Зачем
Текущая `kb_schema` собрана из синтетического генератора: территориальный банк указан
в `org_struct_lvl_2_name`, а в реальной витрине он в `lvl_3`; `incdnt_sum` считается «итого
потерь», а реально почти пуст. Этот каталог покажет **реальные уровни и значения**, чтобы
резолвер определял правильную колонку под запрос пользователя.
""")

# — Cell 1: конфигурация ——————————————————————————————————————————————————————
code(r'''# — Конфигурация: какие колонки собирать (белый список, без PII/текста/сумм) —
SCHEMA = "arnsdpsbx_t_team_sva_oarb_4"
T = lambda name: f"{SCHEMA}.{name}"

# КАТЕГОРИАЛЬНЫЕ колонки-справочники: distinct-значения + filled_pct.
CATEGORICAL = {
    T("d6_base_of_knowledge_ior"): [
        # оргструктура — ВСЕ уровни (чтобы увидеть, где реально ТБ / департамент)
        "org_struct_lvl_2_name", "org_struct_lvl_3_name", "org_struct_lvl_4_name",
        "org_struct_lvl_5_name", "org_struct_lvl_6_name", "org_struct_lvl_7_name",
        "org_struct_lvl_8_name", "org_struct_lvl_9_name", "org_struct_lvl_10_name",
        # процессы (Событие — обычно lvl_4)
        "process_lvl_1_name", "process_lvl_2_name", "process_lvl_3_name", "process_lvl_4_name",
        # функц. блок (Дивизион/Департамент/Центр)
        "funct_block_lvl_2_name", "funct_block_lvl_3_name", "funct_block_lvl_4_name",
        # продукты (Событие)
        "clntpth_lvl_4_name",
        # риск-профиль и типы
        "risk_profile_id", "risk_profile_name",
        "incdnt_type_lvl_1_name", "incdnt_type_lvl_2_name",
        "incdnt_status_name",
        "src_type_lvl_1_name", "src_type_lvl_2_name",
        "incdnt_client_type_name", "incdnt_source_name",
        "busn_area_lvl_1_name", "busn_area_lvl_2_name",
        # флаги риска (distinct = Y/N) – нужны для «риск поведения» и т.п.
        "incdnt_behavior_risk_flag", "incdnt_security_risk_flag",
        "incdnt_infrmtn_sys_risk_flag", "incdnt_model_risk_flag", "incdnt_autoreg_flag",
    ],
    T("d6_base_of_knowledge_incident_fin_impact"): [
        "fin_impact_type_name", "fin_impact_kind_name", "fin_impact_monitoring_flag",
    ],
    T("d6_base_of_knowledge_incident_nonfin_impact"): [
        "nonfin_impact_kind_name", "nonfin_impact_influence_class_name",
    ],
    T("d6_base_of_knowledge_incident_recovery"): [
        "recovery_type_name",
    ],
    T("d6_base_of_knowledge_incident_stts_chng"): [
        "incdnt_status_name", "stts_chng_action_name", "stts_chng_action_code",
    ],
}

# СУММОВЫЕ/числовые колонки – только filled_pct (понять, где реальные суммы, а где пусто).
AMOUNT_FILL = {
    T("d6_base_of_knowledge_ior"): [
        "incdnt_sum", "incdnt_drct_dmg_sum", "incdnt_indrct_dmg_sum",
        "incdnt_unrlzd_dmg_sum", "incdnt_thrd_prt_sum", "incdnt_gain_sum",
        "recovery_rub_amt_aggr",
    ],
    T("d6_base_of_knowledge_incident_fin_impact"): ["fin_impact_rub_amt"],
    T("d6_base_of_knowledge_incident_recovery"): ["recovery_rub_amt"],
}

CAP = 500             # макс. значений на колонку (топ по частоте)
HIGH_CARD = 800       # выше — пометить high_card (кандидат в text-концепт, не в каталог)
print("Категориальных колонок:", sum(len(v) for v in CATEGORICAL.values()),
      "| суммовых для filled_pct:", sum(len(v) for v in AMOUNT_FILL.values()))
''')

# — Cell 2: spark + helpers ———————————————————————————————————————————————————
code(r'''# — SparkSession + хелперы ————————————————————————————————————————————————————
import json
from datetime import datetime
try:
    spark
    print("Используем существующую SparkSession.")
except NameError:
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.appName("ior_value_catalog").enableHiveSupport().getOrCreate()
    print("Создана новая SparkSession.")
print("Spark:", spark.version)

REPORT = {
    "generated_at": datetime.now().isoformat(timespec="seconds"),
    "schema": SCHEMA,
    "columns": {},      # "table.col" -> {filled_pct, distinct_approx, high_card, top_values:[{value, count}]}
    "amount_fill": {},  # "table.col" -> filled_pct
    "errors": [],
}

def _err(where, e):
    msg = "{}: {}: {}".format(where, type(e).__name__, e)
    REPORT["errors"].append(msg); print(" [ERR]", msg)

def fill_rates(table, cols):
    """Доля заполнения сразу для всех колонок таблицы — один проход."""
    sel = ["COUNT(*) AS _total"] + ["COUNT(`{}`) AS `{}`".format(c, c) for c in cols]
    row = spark.sql("SELECT {} FROM {}".format(", ".join(sel), table)).collect()[0]
    total = row["_total"] or 1
    return {c: round(100.0 * (row[c] or 0) / total, 2) for c in cols}
''')

# — Cell 3: filled_pct (категориальные + суммовые) ————————————————————————————
code(r'''# — 1) Доля заполнения по таблицам (категориальные + суммовые) ————————————————
print("== filled_pct ==")
fill_cache = {}
for table, cols in CATEGORICAL.items():
    try:
        fr = fill_rates(table, cols); fill_cache[table] = fr
        print("\n" + table.split(".")[-1])
        for c in cols:
            print("  {:6.2f}% {}".format(fr[c], c))
    except Exception as e:
        _err("fill " + table, e)

print("\n== суммовые (filled_pct) ==")
for table, cols in AMOUNT_FILL.items():
    try:
        fr = fill_rates(table, cols)
        for c in cols:
            key = "{}.".format(table.split(".")[-1]) + c
            REPORT["amount_fill"][key] = fr[c]
            print("  {:6.2f}% {}".format(fr[c], key))
    except Exception as e:
        _err("amount fill " + table, e)
''')

# — Cell 4: distinct top-values по категориальным —————————————————————————————
code(r'''# — 2) Каталог значений: топ-N distinct по каждой категориальной колонке ———
print("== каталог значений (топ по частоте) == ")
for table, cols in CATEGORICAL.items():
    tshort = table.split(".")[-1]
    for c in cols:
        key = "{}.{}".format(tshort, c)
        try:
            dc = spark.sql("SELECT approx_count_distinct(`{}`) AS n FROM {}".format(c, table)).collect()[0]["n"]
            rows = spark.sql(
                "SELECT `{}` AS v, COUNT(*) AS n FROM {} GROUP BY `{}` ORDER BY n DESC LIMIT {}".format(c, table, c, CAP)
            ).collect()
            top = [{"value": r["v"], "count": r["n"]} for r in rows if r["v"] is not None]
            REPORT["columns"][key] = {
                "filled_pct": fill_cache.get(table, {}).get(c),
                "distinct_approx": int(dc),
                "high_card": bool(dc > HIGH_CARD),
                "top_values": top,
            }
            flag = " [HIGH_CARD]" if dc > HIGH_CARD else ""
            preview = ", ".join(["{!r}({})".format(t["value"], t["count"]) for t in top[:6]])
            print("{:50s} distinct={:<6d} {} {}".format(key, int(dc), flag, preview))
        except Exception as e:
            _err("catalog " + key, e)
''')

# — Cell 5: org_struct по уровням рядом + итоговый JSON ———————————————————————
code(r'''# — 3) Где реально ТБ/департамент: org_struct по уровням (топ-8) ——————————————
print("== org_struct: топ-значения по уровням (видно, на каком уровне ТБ) == ")
for lvl in range(2, 11):
    key = "d6_base_of_knowledge_ior.org_struct_lvl_{}_name".format(lvl)
    info = REPORT["columns"].get(key)
    if not info: continue
    vals = ", ".join([str(t["value"]) for t in info["top_values"][:8]])
    print("  lvl_{:<2d} (заполнен {}%, distinct≈{}): {}".format(
        lvl, info.get("filled_pct"), info.get("distinct_approx"), vals))

print("\n\n" + "#" * 64)
print("# ИТОГОВЫЙ JSON – СКОПИРУЙ ВЕСЬ БЛОК НИЖЕ И ПРИШЛИ")
print("# (только справочники категорий + % заполнения, без клиентских данных)")
print("#" * 64 + "\n")
print(json.dumps(REPORT, ensure_ascii=False, indent=1, default=str))
''')

# — сборка nbformat-4 ——————————————————————————————————————————————————————————
NB = {
    "cells": CELLS,
    "metadata": {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"name": "python"},
    },
    "nbformat": 4, "nbformat_minor": 5,
}

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "value_catalog_probe.ipynb")
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(NB, f, ensure_ascii=False, indent=1)
print("Записан блокнот:", OUT, "(ячеек: {})".format(len(CELLS)))