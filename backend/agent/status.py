"""
status – человеческие формулировки статусов агента (детерминированные).

Модели нерассуждающие, поэтому статус-тексты НЕ берём из «мыслей» модели, а
строим детерминированно из структуры действия/QuerySpec. Так точнее, стабильнее
и премиальнее. Чистый офлайн-безопасный модуль (только stdlib).

Используется контроллером и компилятором для эмиссии `activity`-событий, которые
фронт рисует премиальной лентой (shimmer на активном шаге, числа на завершённых).
"""
from __future__ import annotations

from typing import Optional

# Человеческие имена таблиц витрины.
TABLE_HUMAN = {
    "d6_base_of_knowledge_ior": "инциденты",
    "d6_base_of_knowledge_incident_fin_impact": "финансовый эффект",
    "d6_base_of_knowledge_incident_recovery": "возмещения",
    "d6_base_of_knowledge_incident_nonfin_impact": "нефинансовый эффект",
    "d6_base_of_knowledge_incident_stts_chng": "история статусов",
}

# Человеческие имена частых колонок группировки/фильтра.
COLUMN_HUMAN = {
    "process_lvl_4_name": "процессам",
    "process_lvl_3_name": "процессам",
    "org_struct_lvl_3_name": "территориальным банкам",
    "org_struct_lvl_4_name": "подразделениям",
    "funct_block_lvl_3_name": "функциональным блокам",
    "incdnt_type_lvl_1_name": "типам событий",
    "risk_profile_name": "профилям риска",
    "incdnt_status_name": "статусам",
}

_OP_HUMAN = {"gt": ">", "gte": "≥", "lt": "<", "lte": "≤", "ne": "≠", "eq": "=",
             ">": ">", ">=": "≥", "<": "<", "<=": "≤", "=": "=", "==": "="}


def human_table(name: str) -> str:
    return TABLE_HUMAN.get(name, name)


def human_column(name: str) -> str:
    return COLUMN_HUMAN.get(name, name)


def fmt_int(n) -> str:
    """12658 -> '12 658' (узкие неразрывные пробелы делает фронт; тут обычные)."""
    try:
        return f"{int(n):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(n)


def fmt_money(v) -> str:
    """1000000 -> '1 млн', 1500000 -> '1,5 млн' – компактно для статусов."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(v) >= 1_000_000_000:
        return f"{v / 1_000_000_000:.1f}".rstrip("0").rstrip(".") + " млрд"
    if abs(v) >= 1_000_000:
        return f"{v / 1_000_000:.1f}".rstrip("0").rstrip(".") + " млн"
    if abs(v) >= 1_000:
        return f"{v / 1_000:.0f} тыс"
    return fmt_int(v)


def _filters(spec: dict) -> list:
    out = []
    for f in (spec.get("filters") or []):
        if not isinstance(f, dict):
            continue
        kind = (f.get("kind") or "").lower()
        if kind == "period":
            intent = f.get("intent")
            txt = intent.get("text") if isinstance(intent, dict) else (intent or "")
            out.append(("period", str(txt or "период")))
        elif kind == "categorical":
            out.append(("cat", str(f.get("value") or "")))
        elif kind == "range":
            op = _OP_HUMAN.get((f.get("op") or "gt").lower(), ">")
            out.append(("range", f"{f.get('column')}{op}{fmt_money(f.get('value'))}"))
        elif kind == "like":
            out.append(("text", f"текст «{str(f.get('value') or '').strip('%')}»"))
    return out


def describe_spec(spec: dict) -> str:
    """Однострочное человеческое описание выгрузки из QuerySpec.

    Пример: «Инциденты за Q1 2026 • Волго-Вятский банк • direct_loss > 1 млн •
    по процессам • + возмещения • чистая потеря».
    """
    if not isinstance(spec, dict):
        return "Выгрузка"
    parts: list[str] = ["Инциденты"]
    fl = _filters(spec)
    period = next((v for k, v in fl if k == "period"), None)
    if period:
        parts.append(f"за {period}")
    for k, v in fl:
        if k == "cat":
            parts.append(v)
        elif k == "range":
            parts.append(v)
        elif k == "text":
            parts.append(v)
    agg = spec.get("aggregate") or {}
    gb = agg.get("group_by") or []
    if gb:
        parts.append("по " + ", ".join(human_column(c) for c in gb))
    joins = (spec.get("source") or {}).get("joins") or []
    extra = [human_table(j.get("table")) for j in joins if isinstance(j, dict)]
    if extra:
        parts.append("+ " + ", ".join(extra))
    dm = [m.get("as") for m in (spec.get("derived_metrics") or []) if m.get("as")]
    if dm:
        parts.append(", ".join(dm))
    return " • ".join(parts)


# Человеческие заголовки действий контроллера.
_ACTION_TITLE = {
    "search_values": "Ищу значение в данных",
    "distinct_values": "Смотрю значения колонки",
    "describe_schema": "Изучаю структуру",
    "probe": "Проверяю наличие данных",
    "get_ior_details": "Собираю досье инцидента",
    "export_excel": "Формирую Excel",
    "export_csv": "Формирую CSV",
}


def humanize_action(action: str, args: dict) -> tuple[str, Optional[str]]:
    """(title, detail) для activity-события по выбранному действию."""
    args = args or {}
    if action == "run_query_spec":
        spec = args.get("spec", args)
        return "Собираю выгрузку", describe_spec(spec)
    if action == "search_values":
        return "Ищу значение в данных", str(args.get("query") or "")
    if action == "distinct_values":
        return "Смотрю значения колонки", str(args.get("column") or "")
    if action in ("export_excel", "export_csv"):
        return _ACTION_TITLE[action], str(args.get("name") or "")
    if action == "get_ior_details":
        return "Собираю досье инцидента", str(args.get("incdnt_sid") or "")
    if action == "final":
        return "Готовлю ответ", None
    if action == "ask_user":
        return "Уточняю у пользователя", str(args.get("question") or "")
    return _ACTION_TITLE.get(action, action), None


def understand_summary(grounding, period_label: Optional[str], route: str) -> Optional[str]:
    """Ранний человеческий итог «что понял» из граунда/периода (до вызова модели)."""
    bits: list[str] = []
    if period_label:
        bits.append(period_label)
    seen = set()
    for h in (grounding or []):
        col, val = h.get("column"), h.get("value")
        key = (col, val)
        if not val or key in seen:
            continue
        seen.add(key)
        bits.append(str(val))
        if len(bits) >= 4:
            break
    return " • ".join(bits) if bits else None