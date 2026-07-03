"""
Калькулятор потерь по кредиту — структурированный ввод через форму.

POST /api/credit/calculate  -> параметры формы -> расчёт -> Excel + результат
GET  /api/credit/meta       -> справочники для формы (ЦПР, отклонения, ...)

Параметров много и они условные (факторы только для части отклонений,
суммы залога только для DRP-10027 и т.д.), поэтому ввод — через форму,
а не через свободный текст агенту.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/credit", tags=["credit"])

CREDIT_SKILL_ID = "credit_no_way_collect_debt_v2"


class CreditParams(BaseModel):
    client_type: str                  # "1"/"2"/"3" (ФЛ/КСБ/ММБ)
    id_credit: str
    incident_date: Optional[str] = None
    risk_profile_code: str
    deviation_code: str
    factor_codes: list[str] = []
    drp_10027_type: Optional[str] = None
    zalog_overact_amount: Optional[str] = None
    vivod_sredstv_pct: Optional[str] = None
    vivod_sredstv_amount: Optional[str] = None


def _load_dicts() -> dict:
    """Справочники для формы — из единого источника логики."""
    import importlib.util
    src = Path(__file__).resolve().parents[3] / "scripts" / "credit_calc_source.py"
    spec = importlib.util.spec_from_file_location("credit_calc_source", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return {
        "client_type": mod.client_type,
        "digital_risk_profile": mod.digital_risk_profile,
        "deviation_type": mod.deviation_type,
        "DRP_10027_type": mod.DRP_10027_type,
        "factor_op": mod.factor_op,
        # какие отклонения допустимы для ЦПР+сегмента (как в исходном скрипте)
        "allowed_deviations": {
            "DRP-10027": ["28", "29", "30", "31", "32"],
            "DRP-10024": ["34", "35", "36"],
            "DRP-10024|ММБ": ["36", "37", "38", "39", "40"],
            "DRP-10024|КСБ": ["5", "6", "14", "41", "36", "2", "40", "42", "43"],
            "DRP-10047|КСБ": ["44", "45", "46"],
        },
    }


@router.get("/meta")
async def credit_meta():
    """Справочники для формы калькулятора."""
    try:
        return _load_dicts()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Не удалось загрузить справочники: {e}")


@router.post("/calculate")
async def credit_calculate(params: CreditParams):
    """Запускает credit-preset с параметрами формы, возвращает file_id +
    рассчитанные потери (прямые/косвенные/потенциальные)."""
    from backend.skills.registry import get_registry
    from backend.skills.runners.notebook_runner import get_runner
    from backend.storage.database import FileRepo, get_db

    skill = get_registry().get(CREDIT_SKILL_ID)
    if skill is None:
        raise HTTPException(404, f"Skill {CREDIT_SKILL_ID} не зарегистрирован")

    runner = get_runner()
    p = params.model_dump()

    def _do():
        return runner.run_phased(
            skill_id=CREDIT_SKILL_ID,
            notebook_path=skill.notebook_path,
            params=p,
            emit=lambda _phase: None,
        )

    try:
        result = await asyncio.to_thread(_do)
    except Exception as e:  # noqa: BLE001
        logger.exception("[credit] расчёт упал")
        raise HTTPException(500, f"расчёт упал: {e}")

    if getattr(result, "error", None):
        raise HTTPException(500, f"расчёт упал: {result.error}")

    # Читаем рассчитанные потери из Excel (первый ряд)
    losses: dict = {}
    if result.excel_path and Path(result.excel_path).exists():
        try:
            import pandas as pd
            df = pd.read_excel(result.excel_path, engine="openpyxl")
            if not df.empty:
                losses = {k: (None if pd.isna(v) else v)
                          for k, v in df.iloc[0].to_dict().items()}
        except Exception as e:  # noqa: BLE001
            logger.warning("[credit] не прочитал результат: %s", e)

    # Регистрируем файл в БД для скачивания
    file_id = None
    if result.excel_path and Path(result.excel_path).exists():
        try:
            with get_db() as db:
                f = FileRepo.add(
                    db, session_id=None,
                    file_path=str(result.excel_path),
                    file_name=result.excel_filename,
                    size_bytes=Path(result.excel_path).stat().st_size,
                    skill_id=CREDIT_SKILL_ID, status="ready", total_rows=1,
                )
                file_id = f.id
        except Exception as e:  # noqa: BLE001
            logger.warning("[credit] FileRepo.add: %s", e)

    return {
        "ok": True,
        "file_id": file_id,
        "filename": result.excel_filename,
        "losses": losses,
    }