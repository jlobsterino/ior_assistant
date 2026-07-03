"""
Список и перезагрузка навыков (для UI welcome-cards и modal).
"""
from __future__ import annotations

from fastapi import APIRouter

from backend.skills.registry import get_registry

router = APIRouter(prefix="/api/skills", tags=["skills"])


# SVG-path для иконки каждого навыка
_SKILL_ICONS: dict[str, str] = {
    "ior_period_pao_sberbank_v2":
        "M3 20h18M7 20v-7M12 20v-11",
    "report_period_specific_ior_v2":
        "M11 11m-7 0a7 7 0 1 0 14 0a7 7 0 1 0 -14 0M20 20l-3.5-3.5",
    "vozmeshenie_ior_v2":
        "M3 12h18M11 5l-7 7 7 7",
    "financial_consequences_ior_v2":
        "M12 2v20M17 5h9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6",
    "ior_nonfinancial_consequences_v2":
        "M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.38 8.38 0 0 1 9-3.8 8.38 8.38 0 0 1 3.8.9L21 3l-1.9 5.7a8.38 8.38 0 0 1 .9 3.8z",
    "deleted_ior_v2":
        "M3 6h18M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2",
}

# Эталонные placeholder'ы для welcome-cards (с конкретикой для UX и mock-LLM)
_SKILL_PLACEHOLDERS: dict[str, str] = {
    "ior_period_pao_sberbank_v2":      "Выгрузи ИОР за 2025 год по СБ",
    "report_period_specific_ior_v2":   "Покажи всё про инцидент EVE-5092355",
    "vozmeshenie_ior_v2":              "Возмещения по ИОР за Q1 2025",
    "financial_consequences_ior_v2":   "Финансовые последствия по ИОР за 2025",
    "ior_nonfinancial_consequences_v2":"Нефинансовые последствия по ИОР за 2025",
    "deleted_ior_v2":                  "Удалённые ИОР за 2025 год",
}

# Короткие subtitle для UI (если в MD subtitle слишком длинный или не сгенерился)
_SKILL_SUBTITLES: dict[str, str] = {
    "ior_period_pao_sberbank_v2":      "Сводный отчёт по ПАО Сбербанк",
    "report_period_specific_ior_v2":   "Полная информация по EVE-XXXXXXX",
    "vozmeshenie_ior_v2":              "Recovery по ИОР за период",
    "financial_consequences_ior_v2":   "Детализация потерь по типам",
    "ior_nonfinancial_consequences_v2":"Репутация, регулятор, клиенты",
    "deleted_ior_v2":                  "Журнал изменений статусов",
}

# Порядок отображения в welcome
_SKILL_ORDER: list[str] = [
    "ior_period_pao_sberbank_v2",
    "report_period_specific_ior_v2",
    "vozmeshenie_ior_v2",
    "financial_consequences_ior_v2",
    "ior_nonfinancial_consequences_v2",
    "deleted_ior_v2",
]


def _enrich(skill_dict: dict) -> dict:
    sid = skill_dict.get("id") or skill_dict.get("skill_id")
    skill_dict["icon_path"] = _SKILL_ICONS.get(sid, "")
    # Эталонный placeholder (если задан) — приоритет над тем, что вытащил парсер
    if sid in _SKILL_PLACEHOLDERS:
        skill_dict["placeholder"] = _SKILL_PLACEHOLDERS[sid]
    # Короткий subtitle (если задан)
    if sid in _SKILL_SUBTITLES:
        skill_dict["subtitle"] = _SKILL_SUBTITLES[sid]
    return skill_dict


def _sort_key(s: dict) -> int:
    sid = s.get("id") or s.get("skill_id")
    try:
        return _SKILL_ORDER.index(sid)
    except ValueError:
        return 999


@router.get("")
async def list_skills():
    registry = get_registry()
    items = [_enrich(s.to_dict()) for s in registry.list_all()]
    items.sort(key=_sort_key)
    return {"skills": items, "count": len(items)}


@router.post("/reload")
async def reload_skills():
    registry = get_registry()
    count = registry.reload()
    return {"loaded": count}