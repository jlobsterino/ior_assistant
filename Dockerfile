# -----------------------------------------------------------------
# ИОР-помощник - Production image
#
# Сборка:    docker build -t ior-assistant:latest .
# Запуск:    docker run -p 8000:8000 --env-file .env ior-assistant:latest
# -----------------------------------------------------------------
FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ENV=prod

WORKDIR /app

# System deps (для openpyxl/pandas/numpy)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libstdc++6 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# Code
COPY backend/ ./backend/
COPY frontend/ ./frontend/
COPY knowledge_base/ ./knowledge_base/
COPY run-prod.sh .
RUN chmod +x run-prod.sh

# Runtime
RUN mkdir -p data/generated_files logs

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -fSs http://localhost:8000/api/health/detail || exit 1

CMD ["python", "-m", "uvicorn", "backend.main:app", \
     "--host", "0.0.0.0", "--port", "8000", "--log-level", "info"]