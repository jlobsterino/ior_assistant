#!/usr/bin/env bash
# -----------------------------------------------------------------
# Production-запуск (корпоративная среда со Spark)
# Использует реальный Papermill + GigaChat.
# -----------------------------------------------------------------
set -e
cd "$(dirname "$0")"
export PYTHONPATH="$(pwd)${PYTHONPATH:+:$PYTHONPATH}"    # корень проекта на sys.path: backend.* импортируется при любом запуске

source .venv/bin/activate

# Гарантируем prod-режим
export APP_ENV=prod
unset MOCK_NOTEBOOK_EXECUTION    # автодетект -> real

# Отключаем core dumps: при краше JVM (Spark) / Papermill Linux иначе
# пишет огромный core.<PID> (снимок памяти, гигабайты) в рабочую папку.
ulimit -c 0

# Spark поднимается локально в контейнере (master=local[*]), submit на
# YARN больше не используется - SPARK_HOME/sys.path не нужны.
# pyspark берётся из обычного pip install (см. requirements.txt).

# Проверка обязательных переменных
if [ -z "${GIGACHAT_API_URL:-}" ] || [ -z "${JPY_API_TOKEN:-}" ]; then
  echo "⚠  GIGACHAT_API_URL / JPY_API_TOKEN не заданы - LLM будет работать в mock-режиме"
fi

echo
echo "▶ Запуск ior-assistant в PROD-режиме (real Papermill + GigaChat)"
echo "  Listen: http://${APP_HOST:-0.0.0.0}:${APP_PORT:-8000}"
echo "  Health: http://${APP_HOST:-0.0.0.0}:${APP_PORT:-8000}/api/health/detail"
echo

# Несколько воркеров для prod
WORKERS="${UVICORN_WORKERS:-1}"

if [ "$WORKERS" -gt 1 ]; then
  python -m gunicorn backend.main:app \
    -k uvicorn.workers.UvicornWorker \
    --workers "$WORKERS" \
    --bind "${APP_HOST:-0.0.0.0}:${APP_PORT:-8000}" \
    --timeout 900 \
    --log-level "${APP_LOG_LEVEL:-info}"
else
  python -m uvicorn backend.main:app \
    --host "${APP_HOST:-0.0.0.0}" \
    --port "${APP_PORT:-8000}" \
    --log-level "${APP_LOG_LEVEL:-info}"
fi