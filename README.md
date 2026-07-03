# **\# ИОР-помощник**

AI-чат для аудитора розничного бизнеса: выгрузки по ИОР из БД, статистика, поиск, расчёты по СМ 4467\.

┌─────────────────────────────────────────────────────────────┐  
│ Frontend (React 18 \+ Babel standalone, offline-ready)       │  
│   Welcome / Sidebar / Chat / Timeline / Dossier / Stats     │  
│   WebSocket-клиент (в DataLab WAF буферит HTTP \- только WS  │  
│   проходит без буферизации)                                 │  
└─────────────────────────────────────────────────────────────┘  
                              │  
                              │ WebSocket /api/chat/ws  (+SSE fallback)  
                              ▼  
┌─────────────────────────────────────────────────────────────┐  
│ Backend (FastAPI \+ Starlette)                               │  
│   WS /api/chat/ws   POST /api/chat/sessions                 │  
│   GET /api/sessions   GET /api/skills   GET /api/files/\*    │  
│   GET /api/health/detail                                    │  
│                                                             │  
│   Skill Registry \-\> MD-парсер (knowledge\_base/scripts/\*.md) │  
│   Notebook Runner \- IN-PROCESS exec() (одна общая SparkSession│  
│                     переиспользуется между запросами)       │  
│   Spark StatusTracker poller \-\> real-time progress no stages│  
│   Excel writer: xlsxwriter \+ constant\_memory (10x быстрее   │  
│                 openpyxl \+ \~50MB RAM вместо 4-8GB на 800k строк)│  
│   Excel Inspector (pandas \-\> stats \+ 5x6 preview)           │  
│   GigaChat-3-Ultra (httpx, прямой HTTP-клиент) c rate-limiter│  
│   SQLite (NFS-safe: vfs=unix-none, journal=DELETE)          │  
└─────────────────────────────────────────────────────────────┘  
                              │  
               ┌──────────────┴──────────────┐  
               ▼                             ▼  
       Local-mode (mock)             prod-mode (Spark local\[\*\] \+ GigaChat)

## **Два режима работы**

### **🧑‍💻 Local (для разработки на РС)**

* **mock-runner** генерирует demo-Excel с правдоподобными данными  
* **GigaChat опционален** (без токена работает заглушка-роутер)  
* **Hot-reload** через uvicorn  
* Не требует Spark / Hadoop

cd ior-assistant  
cp .env.example .env  
\# APP\_ENV=local уже стоит по умолчанию  
./run-local.sh  
\# \-\> http://localhost:8000

### **🚀 Prod (корпоративная среда)**

* **In-process notebook execution** – .ipynb парсится, code-ячейки исполняются прямо в backend-процессе через exec(). Это позволяет переиспользовать ОДНУ SparkSession.builder.getOrCreate() между всеми запросами (нет 5-10с JVM startup per request).  
* **Real GigaChat** – маршрутизация и текст ответа через корп. API (прямой httpx, без langchain – на Python 3.12 в DataLab langchain тянет несовместимый langchain\_protocol)  
* **xlsxwriter \+ Arrow** – на 800к строк запись Excel в 5-10х быстрее  
* **Phased progress** – UI видит реальный Spark progress (stage 7 \- 67%) и live-обновляемый размер записываемого файла

cd ior-assistant  
cp .env.example .env

\# Отредактировать .env:  
\#  APP\_ENV=prod  
\#  GIGACHAT\_API\_URL=https://gigachat-api.ca.sbrf.ru/v1  
\#  JPY\_API\_TOKEN=...  
\#  SPARK\_MASTER=local\[\*\]   \# Spark поднимается локально в контейнере  
\#  \# SPARK\_HOME / sys.path-инъекция больше НЕ нужны – pyspark из pip

./run-prod.sh   \# или: UVICORN\_WORKERS=4 ./run-prod.sh

или через **Docker**:

docker build \-t ior-assistant:latest .  
docker compose up \-d  
\# либо одной командой:  
docker run \-p 8000:8000 \--env-file .env ior-assistant:latest

### **🧪 JupyterHub / DataLab (за reverse-proxy \+ WAF)**

Корп. DataLab пробрасывает запросы через путь /user/\<username\>/proxy/\<port\>/, плюс перед прокси стоит WAF который **буферит ВСЕ HTTP-responses целиком**. Поэтому в DataLab основной транспорт – **WebSocket**, который WAF не трогает.

**Способ 1 – через setup\_and\_run.ipynb:**

1\. Открыть setup\_and\_run.ipynb в Jupyter  
2\. Шаг 1: Ввести Nexus-токен \-\> pip install \-r requirements.txt  
3\. Шаг 2: .env заполнится автоматически (APP\_ENV=prod \+ GigaChat из окружения)  
4\. Шаг 3: запустить две последние ячейки (одна выводит ссылку, вторая держит сервер)  
5\. Открыть ссылку из вывода

**Способ 2 – из терминала DataLab:**

./run-datalab.sh  
\# Подхватывает JUPYTERHUB\_USER \-\> формирует root\_path  
\# Открыть: https://jupyterhub-datalab.apps.prom-datalab.ca.sbrf.ru/user/\<you\>/proxy/8000/

**Под капотом:**

* FastAPI создаётся с root\_path \= JUPYTER\_PROXY\_PATH  
* Uvicorn запускается с \--root-path \<тот же путь\>  
* Frontend все fetch(...) идут по **относительным URL**, чтобы корректно работать за прокси без изменения base  
* WebSocket-URL строится из window.location \+ wss:// (тоже относительный \- root\_path подставляется автоматически)  
* SQLite NFS-safe: WAL-mode патчится в DELETE через бинарную правку заголовка .db файла, vfs=unix-none отключает fcntl-локи

## **Структура проекта**

ior-assistant/  
├── backend/  
│   ├── main.py                     ← FastAPI entry \+ startup banner  
│   ├── config.py                   ← APP\_ENV \+ автодефект mock/real  
│   ├── api/routes/  
│   │   ├── chat.py                 ← WS /api/chat/ws (default)  
│   │   │                           \+ POST /api/chat/sessions (SSE fallback)  
│   │   ├── files.py                ← /download, /status, /csv  
│   │   └── sessions.py, skills.py, health.py  
│   ├── agent/flow.py               ← оркестрация (phased \+ WS-relay)  
│   ├── core/  
│   │   ├── llm.py                  ← GigaChat (прямой httpx, без Langchain)  
│   │   └── prompts.py              ← системные промпты  
│   ├── skills/  
│   │   ├── registry.py             ← auto-discovery MD из knowledge\_base/scripts/  
│   │   ├── md\_parser.py            ← парсит 16-секционные MD  
│   │   └── runners/  
│   │       ├── notebook\_runner.py  ← in-process exec() \+ phased progress  
│   │       │                       \+ Spark StatusTracker poller  
│   │       │                       \+ xlsx-writer progress poller  
│   │       │                       \+ Cancel via setJobGroup  
│   │       └── excel\_inspector.py  ← pandas → stats \+ 5x6 sample  
│   └── storage/database.py         ← SQLite NFS-safe (vfs=unix-none)  
├── frontend/  
│   ├── index.html                  ← React 18 \+ Babel standalone (offline)  
│   ├── styles.css                  ← editorial restraint (IBM Plex \+ emerald)  
│   ├── ior-app.jsx                 ← главный App \+ WebSocket-клиент  
│   └── messages.jsx                ← UserMsg, AssistantMsg, Stats, Dossier,  
│                                     Excel (preparing/ready/failed),  
│                                     WarningBanner, Followups  
│   ├── skills-modal.jsx, data.jsx, tweaks-panel.jsx  
│   ├── vendor/                     ← React, ReactDOM, Babel (локально, offline)  
│   └── assets/fonts/               ← 42 IBM Plex woff2 (Sans/Mono/Serif)  
├── knowledge\_base/  
│   ├── mapping/ИОР\_Mapping\_разделы.md  
│   ├── scripts/                    ← 6 MD-спецификаций для LLM-роутера  
│   └── notebooks/                  ← 6 .ipynb (новая схема БЗ d6\_\*)  
├── data/                           ← runtime: sessions.db, generated\_files/  
├── docs/  
│   └── CONTEXT\_HANDOFF.md  
├── .env.example  
├── requirements.txt  
├── run-local.sh, run-prod.sh, run-datalab.sh  
├── setup\_and\_run.ipynb             ← DataLab Launcher  
└── Dockerfile, docker-compose.yml

## **API**

### **WebSocket (default для DataLab)**

| URL | Описание |
| :---- | :---- |
| WS /api/chat/ws | Основной транспорт – стрим событий через WebSocket |

**Протокол:**

Client \-\> first frame: {"message": "...", "session\_id": "..." | null}  
Client \-\> cancel frame: {"cancel": true}               \# отмена Spark job  
Server \-\> text frames: {"event": "\<name\>", "data": \<json\>}

### **HTTP**

| Метод | URL | Описание |
| :---- | :---- | :---- |
| POST | /api/chat/sessions | Создать сессию |
| POST | /api/chat/stream | SSE fallback (для local-режима без WAF) |
| GET | /api/sessions | Список сессий (с группировкой по дате) |
| GET | /api/sessions/{id} | История одной сессии |
| DELETE | /api/sessions/{id} | Архивировать |
| GET | /api/skills | Список навыков |
| POST | /api/skills/reload | Перечитать MD без перезапуска |
| GET | /api/files/{id} | Скачать сгенерированный Excel |
| GET | /api/files/{id}/csv | Скачать CSV-альтернативу (10х меньше) |
| GET | /api/files/{id}/status | Polling статус генерации (preparing/ready/failed) |
| GET | /api/health | Простой healthcheck |
| GET | /api/health/detail | Детальный статус: GigaChat / Notebook Runner / БД / Skills |

## **События стрима (WS / SSE)**

| Event | Payload | Что делает в UI |
| :---- | :---- | :---- |
| status | {steps: \[{step, label, time, status}\]} | Timeline pipeline |
| skill | {skill\_id, title, type, confidence} | Tag в topbar сообщения |
| metadata | {stats: {...}} | StatsBlock |
| dossier | {sid, timeline, amounts, fin\_impacts, flags, ...} | Dossier-компонент |
| file\_pending | {skill\_id, total\_rows, status: 'preparing'} | Карточка Excel со spinner |
| file\_progress | {bytes\_written, name} | Обновление "записано Х МБ" |
| file | {file\_id, name, size, rows, columns, sample, has\_csv, status: 'ready'} | Финальная карточка с кнопкой Скачать |
| warning | {level: 'info'|'high', message} | Жёлтый/красный баннер для больших выгрузок |
| followups | {items: \[{label, prompt}\]} | Chips с предложенными вопросами |
| clarification | {question, options} | Уточнение от LLM |
| token | {text} | Стриминг финального текста ответа |
| cancelled | {was\_active} | Подтверждение отмены |
| ping | {} | Heartbeat (UI игнорирует) |
| done | {skill\_id, file\_id, duration\_ms} | Финал |
| error | {message} | Ошибка |

## **Расширение \- как добавить новый отчёт**

1. Положить .ipynb в knowledge\_base/notebooks/. Требования:  
   * Cell с переменными должен иметь tag parameters (для Papermill-style инъекции)  
   * Spark init должен делать getOrCreate() (для переиспользования сессии)  
   * Финальная ячейка должна делать df.to\_excel(filename, engine='xlsxwriter', engine\_kwargs={'options': {'constant\_memory': True}})  
2. Создать .md в knowledge\_base/scripts/ (16 секций \- см. существующие).  
3. Перезапустить backend **или** curl \-X POST localhost:8000/api/skills/reload.  
4. Skill автоматически появится в Welcome-cards и Skills-modal.

## **Запуск тестов и проверка**

\# Проверка статуса всех компонентов  
curl http://localhost:8000/api/health/detail | jq

\# Список навыков  
curl http://localhost:8000/api/skills | jq

\# WebSocket-тест через wscat (npm install \-g wscat):  
wscat \-c ws://localhost:8000/api/chat/ws  
\> {"message": "Покажи всё про инцидент EVE-5092355"}  
\# Получим стрим из {"event":"...", "data":...} JSON-объектов

## **Контекст проекта**

Подробный handoff-документ: [**docs/CONTEXT\_HANDOFF.md**](http://docs.google.com/docs/CONTEXT_HANDOFF.md) – кто что делает, история переписки с СРБ, открытые вопросы, roadmap.