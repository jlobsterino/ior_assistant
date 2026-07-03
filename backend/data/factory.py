"""
Выбор data-backend'а по APP_ENV / DATA_BACKEND env.

  APP_ENV=local               -> DuckDBStore
  APP_ENV=prod                -> SparkHiveStore
  DATA_BACKEND=duckdb (override) -> DuckDBStore
  DATA_BACKEND=spark  (override) -> SparkHiveStore

В prod на DataLab можно поставить DATA_BACKEND=duckdb если хочется
прогнать sanity без Hive (например, схема витрины не доступна).
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from backend.config import get_settings
from backend.data.base import IORDatastore

logger = logging.getLogger(__name__)

_store: Optional[IORDatastore] = None
_lock = threading.Lock()


def get_data_store() -> IORDatastore:
    global _store
    with _lock:
        if _store is not None:
            return _store
        cfg = get_settings()
        backend = (os.environ.get("DATA_BACKEND") or "").lower()
        if not backend:
            backend = "duckdb" if cfg.app_env == "local" else "spark"

        # ----- ЗАЩИТА: НЕ давать использовать синтетический DuckDB в prod -------
        # Иначе юзер видит "ИОР за 2025 - 1000 строк" из сгенерированных
        # тестовых данных, думая что это реальная БД. Чтобы прорваться через
        # этот firewall - поставить ALLOW_DUCKDB_IN_PROD=1 (например для
        # smoke-теста БЕЗ Hive).
        if backend == "duckdb" and cfg.app_env != "local":
            if not os.environ.get("ALLOW_DUCKDB_IN_PROD"):
                raise RuntimeError(
                    "❌ DATA_BACKEND=duckdb выбран в НЕ-local окружении "
                    f"(APP_ENV={cfg.app_env}). Это синтетическая тестовая "
                    "БД из scripts/gen_local_data.py - данные ФЕЙКОВЫЕ.\n"
                    "В prod должен быть DATA_BACKEND=spark (Hive БД).\n\n"
                    "Что делать:\n"
                    "  1. Убери из .env строку DATA_BACKEND=duckdb (или\n"
                    "     поставь DATA_BACKEND=spark), и убедись что\n"
                    "     APP_ENV=prod.\n"
                    "  2. Перезапусти сервер.\n\n"
                    "Если ты ТОЧНО хочешь смоук-тест на синтетике -\n"
                    "выставь ALLOW_DUCKDB_IN_PROD=1 (на свой риск)."
                )
            logger.warning(
                "\n"
                "⚠️ ⚠️ ⚠️ ⚠️ ⚠️ ⚠️ ⚠️ ⚠️ ⚠️ ⚠️ ⚠️ ⚠️ ⚠️\n"
                "⚠️ DATA_BACKEND=duckdb в prod (ALLOW_DUCKDB_IN_PROD=1)\n"
                "⚠️ ВСЕ ДАННЫЕ – СИНТЕТИЧЕСКИЕ, НЕ ИЗ HIVE БД!\n"
                "⚠️ ⚠️ ⚠️ ⚠️ ⚠️ ⚠️ ⚠️ ⚠️ ⚠️ ⚠️ ⚠️ ⚠️ ⚠️"
            )

        if backend == "duckdb":
            from backend.data.duckdb_store import DuckDBStore

            _store = DuckDBStore()
            # Дополнительно для Local - расскажем что это синтетика
            if cfg.app_env == "local":
                logger.info(
                    "[data] backend = duckdb (LOCAL, synthetic data "
                    "from scripts/gen_local_data.py)"
                )
            else:
                logger.warning(
                    "[data] backend = duckdb (SYNTHETIC - "
                    "ALLOW_DUCKDB_IN_PROD bypass)"
                )
        elif backend == "spark":
            from backend.data.spark_store import SparkHiveStore

            _store = SparkHiveStore()
            logger.info(
                "[data] backend = spark (Hive БД "
                '"arnsdpsbx_t_team_sva_oarb_4")'
            )
        else:
            raise ValueError(f"DATA_BACKEND={backend!r} unknown")
        return _store


def reset_data_store() -> None:
    """Для тестов – пересоздать singleton."""
    global _store
    with _lock:
        if _store is not None:
            try:
                _store.close()
            except Exception:
                pass
            _store = None