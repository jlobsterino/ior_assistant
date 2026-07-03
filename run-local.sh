#!/usr/bin/env bash
# -----------------------------------------------------------------
# Локальный запуск (для разработки на РС, без Spark)
# Использует mock-runner и фоллбек-LLM.
# -----------------------------------------------------------------
set -e
cd "$(dirname "$0")"
export PYTHONPATH="$(pwd)${PYTHONPATH:+:$PYTHONPATH}"    # корень проекта на sys.path: backend.* импортируется при любом запуске

if [ ! -d ".venv" ]; then
  echo "▶ Создаю venv..."
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install --quiet -r requirements.txt

# Гарантируем local-режим
export APP_ENV=local
# Принудительно mock (на случай если в .env указано иное)
export MOCK_NOTEBOOK_EXECUTION=true

echo
echo "▶ Запуск ior-assistant в локальном режиме (mock)"
echo "  Открыть: http://localhost:${APP_PORT:-8000}"
echo

python -m uvicorn backend.main:app \
  --host "${APP_HOST:-127.0.0.1}" \
  --port "${APP_PORT:-8000}" \
  --reload