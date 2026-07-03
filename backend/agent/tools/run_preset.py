"""
Tool: run_preset(skill_id, params) — запустить один из 6 готовых
ipynb-скриптов БД через Papermill (legacy notebook runner).

Это back-compat-обёртка: планировщик может в одном шаге плана
обратиться к проверенному скрипту, когда задача попадает под один из
готовых отчётов («Выгрузи ИОР за период», «Полное досье EVE-...» и т.д.).

Для нетривиальных запросов planner соберёт цепочку из atomic-tools
(query / filter / top_n / join / export).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import pandas as pd

from backend.agent.tools.base import Tool, ToolResult
from backend.agent.tools.registry import REGISTRY
from backend.skills.registry import get_registry as get_skill_registry
from backend.skills.runners.notebook_runner import get_runner

logger = logging.getLogger(__name__)

_SKILL_REGISTRY = None


def _skill_ids() -> list[str]:
    global _SKILL_REGISTRY
    try:
        _SKILL_REGISTRY = _SKILL_REGISTRY or get_skill_registry()
        return [s.skill_id for s in _SKILL_REGISTRY.list_all()]
    except Exception:
        return []


async def run_preset(ctx, skill_id: str, params: dict | None = None, emit=None) -> ToolResult:
    """Запускает Papermill-скрипт. Регистрирует получившийся xlsx как файл
    и data как dataframe (для возможной пост-обработки).
    """
    emit = getattr(ctx, "emit", None) or emit
    skill_registry = get_skill_registry()
    skill = skill_registry.get(skill_id)
    if skill is None:
        return ToolResult(
            ok=False,
            error=f"Skill {skill_id!r} не найден. Доступны: {_skill_ids()}"
        )
    
    import inspect
    loop = asyncio.get_running_loop()

    if emit:
        def sync_emit(phase):
            payload = {
                "phase": phase.name,
                "label": phase.label,
                "data": phase.data
            }
            if inspect.iscoroutinefunction(emit):
                coro = emit("notebook_phase", payload)
                asyncio.run_coroutine_threadsafe(coro, loop)
            else:
                loop.call_soon_threadsafe(emit, "notebook_phase", payload)
    else:
        sync_emit = lambda _phase: None

    runner = get_runner()
    # Runner синхронный (Papermill блокирующий) — гоним через to_thread
    def _do():
        return runner.run_phased(
            skill_id=skill_id,
            notebook_path=skill.notebook_path,
            params=params or {},
            emit=sync_emit,
        )
    result = await asyncio.to_thread(_do)

    if result.error:
        return ToolResult(
            ok=False,
            error=f"preset {skill_id} упал: {result.error}",
            duration_ms=result.duration_ms,
        )

    # Регистрируем файл + (если есть) dataframe в session state
    output: dict = {"skill_id": skill_id, "skill_title": skill.title}

    if result.excel_path and result.excel_path.exists():
        f = ctx.register_file(
            name=result.excel_filename,
            path=str(result.excel_path),
            size_bytes=result.excel_path.stat().st_size,
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        output["file_id"] = f.file_id
        output["filename"] = f.name
        output["rows"] = result.rows
        output["size"] = result.excel_meta.get("size") if result.excel_meta else None

    # Подгружаем как pandas DF для возможного post-processing
    try:
        df = pd.read_excel(result.excel_path, engine="openpyxl")
        meta = ctx.register_dataframe(
            df,
            description=f"Результат preset'а {skill.title} "
                        f"(params: {params})",
            created_by=f"run_preset:{skill_id}",
        )
        output["df_id"] = meta.df_id
    except Exception as e:  # noqa: BLE001
        logger.warning("[run_preset] не смог прочитать xlsx как DF: %s", e)

    # Пробрасываем stats / dossier / followups для UI
    output["stats"] = result.stats or {}
    if result.dossier:
        output["dossier"] = result.dossier
    if result.followups:
        output["followups"] = result.followups
    if result.text:
        output["text"] = result.text
        output["narrative"] = result.text

    # CSV-альтернатива
    if result.csv_path and result.csv_path.exists():
        output["has_csv"] = True

    return ToolResult(
        ok=True,
        output=output,
        summary=f"preset {skill_id}: {result.rows} строк, "
                f"файл {result.excel_filename!r}",
        duration_ms=result.duration_ms,
    )


# Регистрация в каталоге
REGISTRY.register(Tool(
    name="run_preset",
    description=(
        "Запустить один из готовых отчётов (preset notebook через Papermill). "
        "Используй когда задача попадает под один из готовых скриптов: "
        "'ior_period_pao_sberbank_v2', 'report_period_specific_ior_v2', "
        "'vozmeshenie_ior_v2', 'financial_consequences_ior_v2', "
        "'ior_nonfinancial_consequences_v2', 'deleted_ior_v2', "
        "'ior_hypothesis_v2'. "
        "Каждый принимает свой набор параметров (см. описание skill'а)."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "skill_id": {
                "type": "string",
                "description": "ID одного из готовых скриптов",
            },
            "params": {
                "type": "object",
                "description": (
                    "Параметры скрипта. Для period-скриптов: "
                    "{'incdnt_entry_dt_begin': 'YYYY-MM-DD', "
                    "'incdnt_entry_dt_end': 'YYYY-MM-DD'}. "
                    "Для ior_hypothesis_v2: {'incdnt_entry_dt_begin': 'YYYY-MM-DD', "
                    "'incdnt_entry_dt_end': 'YYYY-MM-DD', 'status_filter': '...', 'tb_filter': '...', 'block_filter': '...'}. "
                    "Для report_specific: {'incdnt_sid': 'EVE-NNNNNNN'}. "
                    "Опционально: filters, org_prefixes."
                ),
            },
        },
        "required": ["skill_id"],
    },
    returns="{file_id, df_id, rows, stats, dossier?, narrative?}",
    run=run_preset,
    category="presets",
))