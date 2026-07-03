#!/usr/bin/env python3
"""
Генератор синтетической БЗ ИОР для локальной разработки.

Читает backend/agent/schema/kb_schema.yaml и заполняет 5 таблиц
реалистичными данными в data/local_kb.duckdb:

  * d6_base_of_knowledge_ior                  - 10 000 инцидентов
  * d6_base_of_knowledge_incident_recovery    - ~15 000 возмещений
  * d6_base_of_knowledge_incident_fin_impact  - ~25 000 fin_impact'ов
  * d6_base_of_knowledge_incident_nonfin_impact - ~3 000 nonfin
  * d6_base_of_knowledge_incident_stts_chng   - ~30 000 status events

Скорость: ~5 сек на М-серии Mac. Размер БД: ~30 МБ.

Запуск:
  python3 scripts/gen_local_data.py [--rows-ior 10000] [--seed 42]
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.agent.schema import get_schema, reload_schema  # noqa: E402

# --- Реалистичные категориальные значения -----------------------------

TB_NAMES = [
    "Северо-Западный банк", "Сибирский банк", "Среднерусский банк",
    "Уральский банк", "Поволжский банк", "Юго-Западный банк",
    "Волго-Вятский банк", "Дальневосточный банк", "Московский банк",
    "Байкальский банк", "Центрально-Чернозёмный банк",
]

TB_LVL_3 = [
    'Блок "Сеть продаж"', 'Блок "Технологии"', 'Блок "Корпоративные клиенты"',
    'Блок "Розничный бизнес"', 'Блок "Управление рисками"',
]

TYPE_LVL_1 = [
    "Технические сбои", "Ошибка ввода данных", "Внешнее мошенничество",
    "Внутреннее мошенничество", "Нарушение процесса",
    "Ошибки персонала", "Действия третьих лиц",
    "1. Ошибки персонала и недостатки процессов",
]

TYPE_LVL_2 = [
    "Сбой ПО внешней системы", "Сбой инфраструктуры",
    "Опечатка в реквизитах", "Дублирование операции",
    "Фишинг", "Социальная инженерия", "Хищение средств",
    "Подделка документов", "Несоблюдение регламента",
    "Нарушение SLA", "Неверная классификация клиента",
]

PROCESS_LVL_4 = [
    "П1227 Кредитование ФЛ – Выдача", "Кредитные карты – Авторизация",
    "Дебетовые карты – Возврат", "РКО ФЛ – Перевод",
    "Депозиты ФЛ – Открытие вклада", "Ипотечное кредитование – Регистрация",
    "Потребительское кредитование – Одобрение",
    "ДБО – Авторизация платежа", "Кассовое обслуживание – Приём наличных",
    "ВЭД – Конверсия", "Карты МПС – Эмиссия",
]

STATUSES = ["Утверждён", "Закрыт", "Исследование", "Возмещение",
            "Черновик", "Согласование"]

SRC_LVL_1 = ["Система мониторинга", "Сотрудник банка", "Клиент",
             "Аудит", "Регулятор", "Внешний контрагент"]

FIN_KINDS = [
    "Ошибка операции - двойное списание",
    "Штрафные пени по ставке ЦБ",
    "Расходы на ИБ-реагирование",
    "Компенсация клиенту по жалобе",
    "Потеря по операциям мошенничества",
    "Возврат комиссии",
    "Восстановление за счёт страховки",
    "Возмещение со стороны сотрудника",
]

NONFIN_KINDS = [
    "Жалобы и обращения клиентов",
    "Освещение в СМИ",
    "Воздействие со стороны регулятора",
    "Утечка защищаемой информации",
    "Ущерб репутации",
    "Угроза непрерывности деятельности",
]

NONFIN_INFLUENCE = ["Низкий", "Средний", "Высокий", "Очень высокий"]

RECOVERY_TYPES = [
    "Восстановление при техническом сбое",
    "Компенсация от сотрудника", "Страховое возмещение",
    "Возврат третьих лиц", "Регрессный иск",
]

CURRENCY_CODES = ["RUB", "RUB", "RUB", "RUB", "USD", "EUR", "CNY"]

STTS_ACTIONS = [
    ("CREATE", "Создание ИОР"),
    ("APPROVE", "Утверждение"),
    ("DELETE", "Удаление после утверждения"),
    ("UPDATE", "Изменение полей"),
    ("CLOSE", "Закрытие"),
    ("RECOVER", "Регистрация возмещения"),
]

USER_NUMS = [f"SBT{i:08d}" for i in range(100, 200)]

# --- Генераторы -------------------------------------------------------


def _rand_dt(start: datetime, days_range: int) -> datetime:
    return start + timedelta(days=random.randint(0, days_range),
                             hours=random.randint(0, 23),
                             minutes=random.randint(0, 59))


def gen_ior(n: int = 10_000, seed: int = 42) -> pd.DataFrame:
    """Сгенерировать main-таблицу ИОР."""
    random.seed(seed)
    rows = []
    base = datetime(2025, 1, 1)
    for i in range(n):
        entry_dt = _rand_dt(base, 365)
        detection_dt = entry_dt - timedelta(days=random.randint(0, 5))
        start_dt = detection_dt - timedelta(hours=random.randint(0, 48))
        validated = entry_dt + timedelta(days=random.randint(1, 30))
        sum_total = round(random.uniform(0, 5_000_000), 2) if random.random() > 0.3 else 0
        drct = round(sum_total * random.uniform(0.4, 0.9), 2)
        indrct = round(sum_total * random.uniform(0, 0.1), 2)
        unrlzd = round(random.uniform(0, 500_000), 2) if random.random() > 0.8 else 0
        thrd = round(random.uniform(0, 200_000), 2) if random.random() > 0.85 else 0
        gain = round(random.uniform(0, 100_000), 2) if random.random() > 0.95 else 0
        recovery = round(drct * random.uniform(0.1, 0.95), 2) if drct > 0 and random.random() > 0.4 else 0

        tb_lvl_2 = random.choice(TB_NAMES)
        tb_lvl_3 = random.choice(TB_LVL_3)
        rows.append({
            "incdnt_sid": f"EVE-{5_000_000 + i:07d}",
            "incdnt_id": 10_000_000 + i,
            "incdnt_status_name": random.choices(STATUSES, weights=[40, 30, 15, 5, 5, 5])[0],
            "incdnt_autoreg_flag": random.choice(["Y", "N"]),
            "incdnt_detection_person_name": random.choice([
                "Система", "Иванов И.И.", "Петров П.П.", "Сидоров С.С.",
                "Клиент", "Регулятор",
            ]),
            "incdnt_source_name": random.choice([
                "Мониторинг АБС", "Кол-центр", "Обращение клиента",
                "Внутренний аудит", "ЦБ РФ",
            ]),
            "src_type_lvl_1_name": random.choice(SRC_LVL_1),
            "src_type_lvl_2_name": random.choice(["Прямой канал", "Косвенный", "Авто"]),
            "incdnt_type_lvl_1_name": random.choice(TYPE_LVL_1),
            "incdnt_type_lvl_2_name": random.choice(TYPE_LVL_2),
            "incdnt_detection_dt": detection_dt,
            "incdnt_start_dt": start_dt,
            "incdnt_entry_dt": entry_dt,
            "incdnt_first_validated_dttm": validated,
            "incdnt_last_validate_dttm": validated + timedelta(days=random.randint(0, 60)),
            "risk_profile_id": f"RP-{random.randint(1, 50):03d}",
            "risk_profile_name": random.choice([
                "Информационная безопасность", "Кредитные риски",
                "Операционная стабильность", "Регуляторные риски",
                "Поведенческие риски",
                "Штрафные санкции",
            ]),
            "incdnt_client_type_name": random.choice([
                "Физическое лицо", "Юридическое лицо",
                "ИП", "Сотрудник банка", "Контрагент",
            ]),
            "incdnt_mistake_cnt": random.randint(1, 20),
            "incdnt_appl_num": f"APP-{random.randint(10000, 99999)}" if random.random() > 0.7 else None,
            "incdnt_agr_num": str(random.randint(1_000_000_000, 9_999_999_999)) if random.random() > 0.7 else None,
            "incdnt_agr_sid": f"AGR-{random.randint(100000, 999999)}" if random.random() > 0.7 else None,
            "incdnt_summary_descr_txt": random.choice([
                f"Двойное списание у клиента {random.randint(100, 999)}",
                "Сбой при авторизации платежа",
                "Ошибочная регистрация операции",
                "Несанкционированный доступ к данным",
                f"Жалоба клиента на качество обслуживания #{random.randint(1000, 9999)}",
                "Регуляторное предписание ЦБ",
                "Утечка PII",
                "Простой сервиса > SLA",
            ]),
            "incdnt_full_descr_txt": "Полное описание инцидента (синтетика). " * 5,
            "org_struct_id": f"ORG-{random.randint(1, 200):04d}",
            "org_struct_lvl_2_name": tb_lvl_3,
            "org_struct_lvl_3_name": tb_lvl_2,
            "org_struct_lvl_4_name": f"Дивизион {random.randint(1, 30)}",
            "org_struct_lvl_5_name": f"Управление {random.randint(1, 50)}",
            "org_struct_lvl_6_name": f"Отдел {random.randint(1, 80)}",
            "org_struct_lvl_7_name": f"Группа {random.randint(1, 100)}",
            "org_struct_lvl_8_name": None,
            "org_struct_lvl_9_name": None,
            "org_struct_lvl_10_name": None,
            "funct_block_id": f"FB-{random.randint(1, 50):03d}",
            "funct_block_lvl_2_name": random.choice([
                "Розничный бизнес", "Корпоративный бизнес",
                "Технологии", "Управление рисками", "Поддержка",
            ]),
            "funct_block_lvl_3_name": f"Дирекция {random.randint(1, 15)}",
            "funct_block_lvl_4_name": f"Управление {random.randint(1, 40)}",
            "process_lvl_1_name": "Банковские операции",
            "process_lvl_2_name": random.choice([
                "Розничные операции", "Корпоративные операции",
                "Расчёты", "Кредитование",
            ]),
            "process_lvl_3_name": random.choice([
                "ФЛ", "ЮЛ", "Эквайринг", "ДБО",
            ]),
            "process_lvl_4_name": random.choice(PROCESS_LVL_4),
            "clntpth_lvl_4_name": random.choice([
                "Цифровой клиентский путь", "Офлайн обслуживание",
                "Кол-центр", "Сайт банка",
            ]),
            "busn_area_id": f"BA-{random.randint(1, 20):03d}",
            "busn_area_lvl_1_name": random.choice([
                "Корпоративные финансы", "Торговля и продажи",
                "Розничный банковский бизнес", "Коммерческое банковское дело",
                "Платежи и расчёты", "Агентские услуги",
                "Управление активами", "Розничные брокерские услуги",
            ]),
            "busn_area_lvl_2_name": "Базель-II категория",
            "incdnt_security_risk_flag": random.choice([True, False]),
            "incdnt_infrmtn_sys_risk_flag": random.choice([True, False]),
            "incdnt_behavior_risk_flag": random.choice([True, False]),
            "incdnt_model_risk_flag": random.choice([True, False]),
            "incdnt_sum": sum_total,
            "incdnt_drct_dmg_sum": drct,
            "incdnt_drct_dmg_cred_rub_amt": round(drct * random.uniform(0, 0.3), 2),
            "incdnt_drct_dmg_noncred_rub_amt": round(drct * random.uniform(0.7, 1.0), 2),
            "incdnt_indrct_dmg_sum": indrct,
            "incdnt_indrct_dmg_cred_rub_amt": round(indrct * random.uniform(0, 0.3), 2),
            "incdnt_indrct_dmg_noncred_rub_amt": round(indrct * random.uniform(0.7, 1.0), 2),
            "incdnt_unrlzd_dmg_sum": unrlzd,
            "incdnt_unrlzd_dmg_cred_rub_amt": 0,
            "incdnt_unrlzd_dmg_noncred_rub_amt": unrlzd,
            "incdnt_thrd_prt_dmg_sum": thrd,
            "incdnt_thrd_prt_cred_rub_amt": 0,
            "incdnt_thrd_prt_noncred_rub_amt": thrd,
            "incdnt_gain_sum": gain,
            "incdnt_gain_cred_rub_amt": 0,
            "incdnt_gain_noncred_rub_amt": gain,
            "recovery_rub_amt": recovery,
        })
    return pd.DataFrame(rows)


FIN_IMPACT_TYPES = [
    "Прямая потеря", "Косвенная потеря", "Нереализовавшаяся потеря",
    "Потеря третьих лиц", "Прибыль",
]


def gen_fin_impact(ior_df: pd.DataFrame, avg_per_ior: float = 2.5) -> pd.DataFrame:
    """Финансовые последствия. Связь с инцидентом через incdnt_id (FK).
    fin_impact_id - собственный PK строки fin_impact (НЕ FK к инциденту)."""
    rows = []
    fi_counter = 0
    for _, ior in ior_df.iterrows():
        if ior["incdnt_drct_dmg_sum"] == 0 and ior["incdnt_indrct_dmg_sum"] == 0:
            continue
        n = max(1, int(random.gauss(avg_per_ior, 1.0)))
        for j in range(n):
            amt = round(random.uniform(1e3, 1e6), 2)
            fi_counter += 1
            rows.append({
                "incdnt_id": ior["incdnt_id"],                 # FK к main.incdnt_id
                "fin_impact_id": 50_000_000 + fi_counter,      # собственный PK строки
                "fin_impact_type_name": random.choice(FIN_IMPACT_TYPES),
                "fin_impact_sid": f"FI-{fi_counter:08d}",
                "fin_impact_rub_amt": amt,
                "fin_impact_local_crncy_code": "RUB",
                "fin_impact_detection_dt": ior["incdnt_detection_dt"],
                "fin_impact_creation_dttm": ior["incdnt_entry_dt"] + timedelta(days=random.randint(0, 10)),
                "fin_impact_account_num": f"40817810{random.randint(100000000000, 999999999999)}",
                "fin_impact_docum_num": f"D-{random.randint(100000, 999999)}",
                "fin_impact_reg_dt": ior["incdnt_entry_dt"] + timedelta(days=random.randint(0, 30)),
                "fin_impact_crncy_code": random.choice(CURRENCY_CODES),
                "fin_impact_kind_name": random.choice(FIN_KINDS),
                "fin_impact_monitoring_flag": random.choice(["Y", "N"]),
                "fin_impact_local_ccy_amt": amt,
                "fin_impact_ccy_amt": amt,
                "fi_busn_area_id": None,
                "fi_org_struct_id": None,
            })
    return pd.DataFrame(rows)


def gen_recovery(ior_df: pd.DataFrame) -> pd.DataFrame:
    """Возмещения (FK: incdnt_id)."""
    rows = []
    for _, ior in ior_df.iterrows():
        if ior["recovery_rub_amt"] == 0:
            continue
        n = random.choices([1, 2, 3], weights=[60, 30, 10])[0]
        per_amt = ior["recovery_rub_amt"] / n
        for _ in range(n):
            rows.append({
                "incdnt_id": ior["incdnt_id"],
                "recovery_sid": f"REC-{len(rows):08d}",
                "recovery_type_name": random.choice(RECOVERY_TYPES),
                "recovery_local_crncy_code": "RUB",
                "recovery_src_account_num": f"40702810{random.randint(100000000000, 999999999999)}",
                "recovery_doc_num": f"R-{random.randint(100000, 999999)}",
                "recovery_crncy_code": random.choice(CURRENCY_CODES),
                "recovery_creation_dttm": ior["incdnt_entry_dt"] + timedelta(days=random.randint(5, 60)),
                "recovery_reg_dt": ior["incdnt_entry_dt"] + timedelta(days=random.randint(10, 90)),
                "recovery_ccy_amt": round(per_amt, 2),
                "recovery_rub_amt": round(per_amt, 2),
                "recovery_local_ccy_amt": round(per_amt, 2),
            })
    return pd.DataFrame(rows)


def gen_nonfin_impact(ior_df: pd.DataFrame) -> pd.DataFrame:
    """Нефин. последствия (FK: incdnt_id), не у каждого ИОР."""
    rows = []
    for _, ior in ior_df.iterrows():
        if random.random() < 0.7:
            continue
        for _ in range(random.choices([1, 2], weights=[80, 20])[0]):
            rows.append({
                "incdnt_id": ior["incdnt_id"],
                "nonfin_impact_sid": f"NFI-{len(rows):08d}",
                "nonfin_impact_kind_name": random.choice(NONFIN_KINDS),
                "nonfin_impact_influence_class_name": random.choice(NONFIN_INFLUENCE),
            })
    return pd.DataFrame(rows)


def gen_status_chng(ior_df: pd.DataFrame) -> pd.DataFrame:
    """История статусов (FK: incdnt_id)."""
    rows = []
    for _, ior in ior_df.iterrows():
        # У каждого ИОР минимум CREATE + APPROVE
        events = ["CREATE", "APPROVE"]
        # Случайно добавим UPDATE / DELETE / CLOSE
        if random.random() > 0.5:
            events.append("UPDATE")
        if ior["incdnt_status_name"] == "Закрыт":
            events.append("CLOSE")
        if ior["incdnt_status_name"] == "Удалён" or random.random() < 0.05:
            events.append("DELETE")

        for k, action_code in enumerate(events):
            action_name = dict(STTS_ACTIONS).get(action_code, action_code)
            rows.append({
                "stts_chng_action_code": action_code,
                "incdnt_id": ior["incdnt_id"],
                "incdnt_status_name": ior["incdnt_status_name"],
                "incdnt_status_code": ior["incdnt_status_name"][:3].upper(),
                "stts_chng_action_name": action_name,
                "stts_chng_comment_txt": random.choice([
                    "", "Согласовано с риск-координатором",
                    "По запросу регулятора", "Дубликат - удалён",
                    "Обновлены параметры", "Закрыто - возмещение получено",
                ]),
                "stts_chng_action_dttm": ior["incdnt_entry_dt"] + timedelta(
                    days=k * random.randint(1, 7),
                    hours=random.randint(0, 23),
                ),
                "stts_chng_user_num": random.choice(USER_NUMS),
                "start_dt": ior["incdnt_entry_dt"],
            })
    return pd.DataFrame(rows)


# --- Main -------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows-ior", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--db", default=str(ROOT / "data" / "local_kb.duckdb"))
    args = parser.parse_args()

    reload_schema()
    schema = get_schema()

    print(f"-> Генерирую {args.rows_ior:,} ИОР (seed={args.seed})...")
    random.seed(args.seed)

    df_ior = gen_ior(n=args.rows_ior, seed=args.seed)
    df_fin = gen_fin_impact(df_ior)
    df_rec = gen_recovery(df_ior)
    df_nonfin = gen_nonfin_impact(df_ior)
    df_stts = gen_status_chng(df_ior)

    print(f"  * main:       {len(df_ior):,} rows x {len(df_ior.columns)} cols")
    print(f"  * fin_impact: {len(df_fin):,} rows x {len(df_fin.columns)} cols")
    print(f"  * recovery:   {len(df_rec):,} rows x {len(df_rec.columns)} cols")
    print(f"  * nonfin:     {len(df_nonfin):,} rows")
    print(f"  * stts:       {len(df_stts):,} rows")

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    conn = duckdb.connect(str(db_path))
    print(f"\n-> Записываю в {db_path}...")
    for table_name, df in [
        ("d6_base_of_knowledge_ior", df_ior),
        ("d6_base_of_knowledge_incident_fin_impact", df_fin),
        ("d6_base_of_knowledge_incident_recovery", df_rec),
        ("d6_base_of_knowledge_incident_nonfin_impact", df_nonfin),
        ("d6_base_of_knowledge_incident_stts_chng", df_stts),
    ]:
        # DuckDB сам выведет схему по df, имена/типы будут совпадать
        # со spark-схемой по факту значений
        conn.register("_tmp", df)
        conn.execute(f"CREATE TABLE {table_name} AS SELECT * FROM _tmp")
        conn.unregister("_tmp")
        print(f"  ✓ {table_name}")

    # Indexes для ускорения query
    print("\n-> Создаю индексы...")
    indexes = [
        "CREATE INDEX idx_ior_sid ON d6_base_of_knowledge_ior(incdnt_sid)",
        "CREATE INDEX idx_ior_id ON d6_base_of_knowledge_ior(incdnt_id)",
        "CREATE INDEX idx_ior_dt ON d6_base_of_knowledge_ior(incdnt_entry_dt)",
        "CREATE INDEX idx_ior_tb ON d6_base_of_knowledge_ior(org_struct_lvl_2_name)",
        "CREATE INDEX idx_rec_id ON d6_base_of_knowledge_incident_recovery(incdnt_id)",
        "CREATE INDEX idx_fin_id ON d6_base_of_knowledge_incident_fin_impact(incdnt_id)",
        "CREATE INDEX idx_nonfin_id ON d6_base_of_knowledge_incident_nonfin_impact(incdnt_id)",
        "CREATE INDEX idx_stts_id ON d6_base_of_knowledge_incident_stts_chng(incdnt_id)",
    ]
    for sql in indexes:
        try:
            conn.execute(sql)
        except Exception as e:
            print(f"  ! {e}")

    conn.close()

    size_mb = db_path.stat().st_size / 1024 / 1024
    print(f"\n✓ Готово: {db_path} ({size_mb:.1f} МБ)")
    print(f"  Использование: APP_ENV=local DATA_BACKEND=duckdb ./run-local.sh")


if __name__ == "__main__":
    main()