"""
Детальный healthcheck — для prod-deployment и локальной отладки.
Показывает статус GigaChat, Papermill, БД, Skill Registry.
"""
from __future__ import annotations

import logging
import shutil

from fastapi import APIRouter

from backend.config import get_settings
from backend.skills.registry import get_registry
from backend.storage.database import get_db, SessionModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/health", tags=["health"])


def _check_gigachat() -> dict:
    cfg = get_settings()
    if not cfg.gigachat_available:
        return {"status": "disabled", "mode": "mock",
                "reason": "GIGACHAT_API_URL или JPY_API_TOKEN не заданы"}
    try:
        from backend.core.llm import get_llm
        llm = get_llm()
        return {
            "status": "ok" if llm.available else "error",
            "mode": "real" if llm.available else "mock",
            "model": cfg.gigachat_model,
            "url": cfg.gigachat_api_url,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _check_notebook_runner() -> dict:
    """Проверка in-process notebook runner (nbformat для парсинга .ipynb)."""
    cfg = get_settings()
    if cfg.use_mock_runner:
        return {"status": "disabled", "mode": "mock",
                "reason": "APP_ENV=local или MOCK_NOTEBOOK_EXECUTION=true"}
    try:
        import nbformat
        return {"status": "ok", "mode": "in-process",
                "nbformat_version": nbformat.__version__}
    except ImportError:
        return {"status": "error", "error": "nbformat не установлен"}


def _check_pyspark() -> dict:
    cfg = get_settings()
    if cfg.use_mock_runner:
        return {"status": "disabled", "reason": "mock-режим"}
    try:
        import pyspark
        return {
            "status": "ok",
            "version": pyspark.__version__,
            "master": cfg.spark_master,
        }
    except ImportError:
        return {"status": "warn", "error": "pyspark не установлен (необходим в prod)"}


def _check_db() -> dict:
    try:
        with get_db() as db:
            cnt = db.query(SessionModel).count()
        return {"status": "ok", "sessions": cnt}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _check_skills() -> dict:
    try:
        reg = get_registry()
        skills = reg.list_all()
        return {
            "status": "ok",
            "count": len(skills),
            "ids": [s.skill_id for s in skills],
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _check_disk() -> dict:
    cfg = get_settings()
    try:
        total, used, free = shutil.disk_usage(cfg.files_path)
        return {
            "status": "ok",
            "files_dir": str(cfg.files_path),
            "free_gb": round(free / (1024 ** 3), 2),
            "total_gb": round(total / (1024 ** 3), 2),
        }
    except Exception as e:
        return {"status": "warn", "error": str(e)}


@router.get("")
async def health():
    """Простой health: { status: ok }"""
    return {"status": "ok"}


@router.get("/detail")
async def health_detail():
    """Расширенный health: статус каждого компонента + общий вердикт."""
    import os
    cfg = get_settings()
    data_backend = (os.environ.get("DATA_BACKEND") or "").lower() or \
                   ("duckdb" if cfg.app_env == "local" else "spark")
    llm_backend = (os.environ.get("LLM_BACKEND") or "").lower() or \
                  ("fireworks" if os.environ.get("FIREWORKS_API_KEY")
                   else "ollama" if cfg.app_env == "local"
                   else "gigachat")

    parts = {
        "app_env": cfg.app_env,
        "is_prod": cfg.is_prod,
        "use_mock_runner": cfg.use_mock_runner,
        "data_backend": data_backend,
        "data_backend_source": (
            "spark - Hive БЗ arnsdpsbx_t_team_sva_oarb_4 (РЕАЛЬНЫЕ данные)"
            if data_backend == "spark"
            else "duckdb - data/local_kb.duckdb (⚠ СИНТЕТИКА из gen_local_data.py)"
        ),
        "llm_backend": llm_backend,
        # ⚠ если в prod видишь data_backend=duckdb - пофикси .env
        "warning": (
            "DATA_BACKEND=duckdb в prod - отдаются СИНТЕТИЧЕСКИЕ данные!"
            if cfg.app_env != "local" and data_backend == "duckdb"
            else None
        ),
        "components": {
            "gigachat": _check_gigachat(),
            "notebook_runner": _check_notebook_runner(),
            "pyspark": _check_pyspark(),
            "database": _check_db(),
            "skill_registry": _check_skills(),
            "disk": _check_disk(),
        },
    }
    # Общий статус: error если хоть один компонент error
    statuses = [c.get("status") for c in parts["components"].values()]
    if "error" in statuses:
        parts["status"] = "error"
    elif "warn" in statuses:
        parts["status"] = "degraded"
    else:
        parts["status"] = "ok"
    return parts