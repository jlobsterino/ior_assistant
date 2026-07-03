#!/usr/bin/env bash
# -----------------------------------------------------------------
# Запуск в JupyterHub / DataLab (внутренняя сеть банка)
# Автоматически определяет JupyterHub-юзера и подставляет --root-path.
# -----------------------------------------------------------------
set -e
cd "$(dirname "$0")"
export PYTHONPATH="$(pwd)${PYTHONPATH:+:$PYTHONPATH}"    # корень проекта на sys.path: backend.* импортируется при любом запуске

PORT="${APP_PORT:-8000}"
JPY_USER="${JUPYTERHUB_USER:-$(whoami)}"
PROXY_PATH="/user/${JPY_USER}/proxy/${PORT}"

export APP_ENV=prod
export JUPYTER_PROXY_PATH="${PROXY_PATH}"
# Критично для JupyterHub: иначе uvicorn-логи буферизируются и
# их не видно в реальном времени.
export PYTHONUNBUFFERED=1

# Отключаем core dumps: при краше JVM (Spark) / Papermill-субпроцесса
# Linux иначе пишет огромный файл core.<PID> (полный снимок памяти,
# гигабайты) в рабочую папку. Нам пост-мортемы не нужны.
ulimit -c 0

# venv должен быть уже создан и зависимости установлены
# (см. setup_and_run.ipynb - шаг 1).
if [ -d ".venv" ]; then
  source .venv/bin/activate
fi

echo
echo "▶ ИОР-помощник . DataLab proxy mode"
echo "  Proxy path : ${PROXY_PATH}"
echo "  URL        : https://jupyterhub-datalab.apps.prom-datalab.ca.sbrf.ru${PROXY_PATH}/"
echo "  Health     : ${PROXY_PATH}/api/health/detail"
echo

python -m uvicorn backend.main:app \
  --host 0.0.0.0 \
  --port "${PORT}" \
  --root-path "${PROXY_PATH}" \
  --log-level "${APP_LOG_LEVEL:-info}" \
  --timeout-keep-alive 30