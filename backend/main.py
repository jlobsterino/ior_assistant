"""
ИОР-помощник — FastAPI entry point.

Поддерживает запуск за reverse-proxy (JupyterHub / DataLab):
  при `JUPYTER_PROXY_PATH=/user/<u>/proxy/8000` FastAPI получает root_path,
  а uvicorn запускается с `--root-path <тот же>`.
  Frontend использует относительные URL — будет работать корректно.

Lifespan-стратегия:
 * startup ДОЛЖЕН быть быстрым (<2 сек), иначе JupyterHub-прокси
   возвращает 502 еще до того, как uvicorn успеет принять запрос.
 * Поэтому LLM (GigaChat) инициализируется ЛЕНИВО — при первом вызове
   из /api/chat/stream, а не в lifespan.
 * Каждый шаг логируется в отдельную строку с принудительным flush,
   чтобы в JupyterHub cell output не «слипались» строки.
"""

from __future__ import annotations

import sys
import subprocess
import logging
import os
import traceback
from contextlib import asynccontextmanager
from pathlib import Path

# Загружаем .env в process env ДО импорта модулей читающих os.environ
# (backend.core.llm читает LLM_BACKEND/FIREWORKS_API_KEY напрямую).
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from backend.api.routes.chat import router as chat_router
from backend.api.routes.credit import router as credit_router
from backend.api.routes.files import router as files_router
from backend.api.routes.explore import router as explore_router
from backend.api.routes.health import router as health_router
from backend.api.routes.reports import router as reports_router
from backend.api.routes.sessions import router as sessions_router
from backend.api.routes.skills import router as skills_router
from backend.config import get_settings
from backend.skills.registry import get_registry
from backend.storage.database import init_db

# — Logging ————————————————————————————————————————————————————————————

# В JupyterHub stdout может буферизоваться. Используем stderr (unbuffered
# в Python по умолчанию) + force=True чтобы перезаписать любые хэндлеры,
# которые мог поставить uvicorn до нас.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
    force=True,
)
logger = logging.getLogger("ior-assistant")


def _flush() -> None:
    """Принудительный flush, чтобы логи появлялись в JupyterHub cell сразу."""
    try:
        sys.stderr.flush()
        sys.stdout.flush()
    except Exception:
        pass


# — Lifespan ————————————————————————————————————————————————————————————


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Быстрый startup. Любая ошибка ЛОГИРУЕТСЯ, но не валит uvicorn —
    приложение поднимется, а ошибку видно в /api/health/detail.
    """
    try:
        cfg = get_settings()

        # Шаг 1: директории
        logger.info("[App] [1/3] Создание директорий...")
        _flush()
        cfg.files_path.mkdir(parents=True, exist_ok=True)
        (cfg.base_dir / "data").mkdir(parents=True, exist_ok=True)
        (cfg.base_dir / "logs").mkdir(parents=True, exist_ok=True)

        # Шаг 2: БД (SQLite create_all – миллисекунды)
        logger.info("[App] [2/3] Инициализация БД...")
        _flush()
        try:
            init_db()
        except Exception as e:
            logger.exception("[App] init_db() упал: %s", e)

        # Шаг 3: Skill Registry (только парсинг MD, без сети)
        logger.info("[App] [3/3] Загрузка навыков из %s", cfg.kb_scripts_path)
        _flush()
        try:
            registry = get_registry()
            skills = registry.list_all()
            logger.info("[App] Загружено навыков: %d", len(skills))
            for s in skills:
                logger.info(
                    "[App]  * %s [%s] notebook=%s",
                    s.skill_id,
                    s.type,
                    s.notebook_path.name if s.notebook_path else "-",
                )
        except Exception as e:
            logger.exception("[App] Не удалось загрузить навыки: %s", e)
            skills = []

        # — Сводка по режиму ————————————————————————————————————————————————————
        # LLM сюда не грузим: инициализация GigaChat может блокировать
        # на сетевом запросе и не дать uvicorn принять connection,
        # из-за чего JupyterHub-прокси возвращает 502.
        # Определяем статус LLM просто по env vars.
        llm_status = "ON (lazy)" if cfg.gigachat_available else "OFF (mock)"

        # Дополнительно показываем backend'ы – критично для prod, чтобы
        # юзер сразу видел: тянем ли мы данные из реальной Hive БД
        # или из синтетического DuckDB.
        data_backend_env = (os.environ.get("DATA_BACKEND") or "").lower() or \
                           ("duckdb" if cfg.app_env == "local" else "spark")
        llm_backend_env = (os.environ.get("LLM_BACKEND") or "").lower() or \
                          ("fireworks" if os.environ.get("FIREWORKS_API_KEY")
                           else "ollama" if cfg.app_env == "local"
                           else "gigachat")

        data_label = {
            "spark": "spark (Hive БЗ arndspdsbx_t_team_sva_oarb_4) — РЕАЛЬНЫЕ ДАННЫЕ",
            "duckdb": "duckdb (data/local_kb.duckdb) — ⚠ СИНТЕТИКА из gen_local_data.py",
        }.get(data_backend_env, data_backend_env)

        banner = "=" * 64
        logger.info(banner)
        logger.info("[App] ИОР-помощник — запущен")
        logger.info("[App] Режим:          APP_ENV=%s (prod=%s)", cfg.app_env, cfg.is_prod)
        logger.info("[App] Notebook runner: %s", "MOCK" if cfg.use_mock_runner else "REAL (Papermill)")
        logger.info("[App] LLM backend:     %s", llm_backend_env)
        logger.info("[App] Data backend:    %s", data_label)
        logger.info("[App] GigaChat:        %s", llm_status)
        logger.info("[App] Skills:          %d загружено", len(skills))
        logger.info("[App] Listen:          http://%s:%d", cfg.app_host, cfg.app_port)
        if app.root_path:
            logger.info("[App] Root-path:       %s", app.root_path)
        logger.info("[App] Health:          /api/health  /api/health/detail")
        logger.info(banner)

        # Защита от случайного duckdb в prod (см. data/factory.py).
        # Не валим startup, просто эмитим warning — get_data_store()
        # при первом вызове бросит RuntimeError.
        if cfg.app_env != "local" and data_backend_env == "duckdb":
            logger.error("⚠  CONFIG ERROR: DATA_BACKEND=duckdb в prod — "
                         "это СИНТЕТИКА! См. .env. Исправьте на spark.")

        # Pre-warm Spark в фоне (только prod) — первый запрос юзера тогда
        # не платит cold-start (~30-60с на JVM + Hive metastore).
        # Только если spark уже выбран как backend.
        if data_backend_env == "spark":
            import asyncio as _aio

            async def _prewarm_spark():
                import time
                try:
                    t0 = time.perf_counter()
                    logger.info("[Warmup] Spark pre-warm запущен в фоне...")
                    store = await _aio.to_thread(
                        __import__("backend.data.factory",
                                   fromlist=["get_data_store"]).get_data_store
                    )
                    # SELECT 1 – самый быстрый sanity-запрос
                    await _aio.to_thread(
                        lambda: store._get_spark().sql("SELECT 1").collect()
                        if hasattr(store, "_get_spark") else None
                    )
                    elapsed = time.perf_counter() - t0
                    logger.info("[Warmup] ✓ Spark прогрет за %.1fc — "
                                "первый запрос юзера будет быстрым",
                                elapsed)

                    # Обогащаем схему живыми enum-значениями из БД,
                    # чтобы агент знал реальный список статусов/типов
                    # (например "Удалён", которого нет в YAML).
                    try:
                        from backend.agent.schema.loader import (
                            enrich_schema_with_real_enums,
                        )
                        n = await _aio.to_thread(
                            enrich_schema_with_real_enums, store,
                        )
                        logger.info("[Warmup] ✓ Schema обогащена: "
                                    "%d enum-колонок из БД", n)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("[Warmup] Schema enrich не удался "
                                       "(не критично): %s", e)
                except Exception as e:  # noqa: BLE001
                    logger.warning("[Warmup] Spark pre-warm не удался "
                                   "(не критично): %s", e)

            _aio.create_task(_prewarm_spark())
            _flush()
    except Exception as e:
        # Никогда не валим startup — JupyterHub в этом случае показывает 502.
        # Лучше поднять app в degraded-режиме, а юзер увидит ошибку в health.
        logger.error("[App] КРИТИЧЕСКАЯ ОШИБКА startup: %s\n%s",
                     e, traceback.format_exc())
        _flush()

    yield
    logger.info("[App] Остановка.")
    _flush()


# — Application —————————————————————————————————————————————————————————


def create_app() -> FastAPI:
    # JupyterHub/DataLab reverse-proxy: путь префикса задаётся через env.
    # uvicorn должен быть запущен с --root-path тем же значением.
    root_path = os.environ.get("JUPYTER_PROXY_PATH", "").rstrip("/")
    if root_path:
        logger.info("[App] root_path=%s (reverse-proxy режим)", root_path)

    app = FastAPI(
        title="ИОР-помощник",
        description="AI-помощник аудитора розничного бизнеса Сбера",
        version="0.1.0",
        lifespan=lifespan,
        root_path=root_path,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(chat_router)
    app.include_router(sessions_router)
    app.include_router(files_router)
    app.include_router(skills_router)
    app.include_router(reports_router)
    app.include_router(explore_router)
    app.include_router(health_router)
    app.include_router(credit_router)

    # Cache-control для frontend: в local-режиме (dev) выключаем кэш для
    # .jsx/.css/.html — иначе браузер хранит старую версию и фиксы не видны
    # после edit'а. В prod — длинный кэш (1 час) — статика стабильная.
    _local_mode = (get_settings().app_env == "local")
    _frontend_dir = Path(__file__).parent.parent / "frontend"

    class CacheControlStaticFiles(StaticFiles):
        def __init__(self, *args, local_mode: bool = False, **kwargs):
            self.local_mode = local_mode
            super().__init__(*args, **kwargs)

        async def get_response(self, path: str, scope):
            response = await super().get_response(path, scope)
            if (path.endswith(".jsx") or path.endswith(".css")
                    or path.endswith(".html") or path == ""):
                if self.local_mode:
                    response.headers["Cache-Control"] = "no-store, must-revalidate"
                    response.headers["Pragma"] = "no-cache"
                else:
                    response.headers["Cache-Control"] = "public, max-age=3600"
            return response

    # Auto cache-bust для .jsx (Babel-inline трансформ держит файлы в памяти
    # по URL — без ?v=mtime после edit'а UI зависает на старой версии).
    # Только в local-режиме, для prod статика стабильная и кэшируется.
    if _local_mode and _frontend_dir.exists():
        from fastapi.responses import HTMLResponse
        import re as _re

        @app.get("/", include_in_schema=False)
        async def _index_with_cachebust():
            html_path = _frontend_dir / "index.html"
            text = html_path.read_text(encoding="utf-8")

            def _bump(match):
                fname = match.group(1)
                f = _frontend_dir / fname
                mtime = int(f.stat().st_mtime) if f.exists() else 0
                return f'src="{fname}?v={mtime}"'

            # Заменяем src="*.jsx" -> src="*.jsx?v=<mtime>"
            text = _re.sub(r'src="([^"]+\.jsx)"', _bump, text)

            # Аналогично для css link href
            def _bump_href(match):
                fname = match.group(1)
                f = _frontend_dir / fname
                mtime = int(f.stat().st_mtime) if f.exists() else 0
                return f'href="{fname}?v={mtime}"'

            text = _re.sub(r'href="([^"]+\.css)"', _bump_href, text)
            return HTMLResponse(text)

    # Статика (frontend)
    if _frontend_dir.exists():
        app.mount("/", CacheControlStaticFiles(directory=str(_frontend_dir), html=True, local_mode=_local_mode), name="frontend")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    cfg = get_settings()
    uvicorn.run(
        "backend.main:app",
        host=cfg.app_host,
        port=cfg.app_port,
        reload=False,
        log_level=cfg.app_log_level.lower(),
    )