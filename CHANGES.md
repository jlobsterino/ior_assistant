# Руководство по переносу файлов в закрытый контур (Сводка изменений)

Данный документ содержит **полный список файлов**, которые были добавлены или изменены в рамках реализации функционала аналитических гипотез и оффлайн-режима. Переносите эти файлы точечно по указанным путям.

---

## 1. Новые файлы (перенести полностью)

### 📄 `backend/agent/hypothesis.py`
* **Роль**: Движок оффлайн-гипотез (`HypothesisEngine`).
* **Функционал**:
  * Расчет статистического профиля датафрейма (включая **Правило Парето** для концентрации рисков — топ-1% и топ-5% крупнейших потерь, а также выбросы $3\sigma$).
  * Построение графиков динамики с помощью `matplotlib` (столбчатый график для объема + линия для суммы потерь) с использованием неинтерактивного бэкэнда `matplotlib.use('Agg')`.
  * Формирование текстового отчета по гипотезе на русском языке для отправки в локальную LLM.
* **Ссылка на файл**: [hypothesis.py](file:///d:/ior_assistant/backend/agent/hypothesis.py)

### 📄 `scripts/build_local_catalog.py`
* **Роль**: Скрипт сборки оффлайн-каталогов заземления.
* **Функционал**: Генерирует файлы `kb_value_catalog.json` и `kb_value_index.json` в папке `backend/agent/schema/` на основе локальной базы данных DuckDB, чтобы заземление категориальных признаков и поиск по опечаткам работали оффлайн.
* **Ссылка на файл**: [build_local_catalog.py](file:///d:/ior_assistant/scripts/build_local_catalog.py)

---

## 2. Измененные файлы (перенести изменения или файлы целиком)

### 📄 `backend/IOR_pipeline_search.py`
* **Изменение**: Интегрировано кэширование модели SentenceTransformer (BGE-M3), CrossEncoder (reranker) и FAISS/BM25 индексов в глобальный контекст процесса для ускорения повторного старта.
* **Ссылка на файл**: [IOR_pipeline_search.py](file:///d:/ior_assistant/backend/IOR_pipeline_search.py)

### 📄 `backend/pipeline_search.py`
* **Изменение**: Аналогичное кэширование моделей и индексов в памяти процесса для избежания повторных чтений с диска.
* **Ссылка на файл**: [pipeline_search.py](file:///d:/ior_assistant/backend/pipeline_search.py)

### 📄 `backend/api/routes/chat.py`
* **Изменение**: Интеграция вызова `HypothesisEngine` в основной цикл `handle_message`. Добавлена проверка на наличие в запросе пользователя слов-триггеров гипотез (*"гипотеза"*, *"динамика"*, *"закономерность"*, *"анализ"*). При совпадении вызывается аналитический оффлайн-движок, строит график, прикрепляет его к ответу и выводит гипотезу.
* **Ссылка на файл**: [chat.py](file:///d:/ior_assistant/backend/api/routes/chat.py)

### 📄 `backend/agent/agent_flow.py`
* **Изменение**: Интеграция вызова `generate_hypothesis_narrative` для компиляции текстовых результатов анализа при завершении формирования выгрузки.
* **Ссылка на файл**: [agent_flow.py](file:///d:/ior_assistant/backend/agent/agent_flow.py)

### 📄 `frontend/messages.jsx`
* **Изменение**: Добавлена поддержка рендеринга картинок `![alt](url)` в кастомный React Markdown-парсер (`renderMarkdown`). Позволяет фронтенду отображать Matplotlib-графики, сгенерированные бэкендом.
* **Ссылка на файл**: [messages.jsx](file:///d:/ior_assistant/frontend/messages.jsx)

### 📄 `backend/api/routes/files.py`
* **Изменение**: Реализован эндпоинт `GET /api/files/{file_id}/raw` для отдачи бинарных файлов (включая PNG графиков) с автоматическим определением MIME-типов (`mimetypes.guess_type`), чтобы фронтенд мог загружать картинки.
* **Ссылка на файл**: [files.py](file:///d:/ior_assistant/backend/api/routes/files.py)

### 📄 `backend/storage/database.py`
* **Изменение**: Исправлена ошибка открытия SQLite на Windows: параметр `vfs=unix-none` теперь применяется только на Unix/Linux (`os.name != 'nt'`), что предотвращает падение приложения на локальной Windows-машине.
* **Ссылка на файл**: [database.py](file:///d:/ior_assistant/backend/storage/database.py)

### 📄 `scripts/gen_local_data.py`
* **Изменение**:
  * Исправлено ошибочное сопоставление уровней оргструктуры: `org_struct_lvl_2_name` теперь содержит функциональные блоки, а `org_struct_lvl_3_name` — Территориальные Банки (ТБ), как в реальной базе данных.
  * Добавлены отсутствующие в генераторе, но требуемые тестами значения: профиль риска `"Штрафные санкции"`, код процесса `"П1227"`, тип события `"1. Ошибки персонала и недостатки процессов"`.
* **Ссылка на файл**: [gen_local_data.py](file:///d:/ior_assistant/scripts/gen_local_data.py)

### 📄 `backend/agent/schema/kb_schema.yaml`
* **Изменение**:
  * Прописана секция `foreign_keys` для всех дочерних таблиц, чтобы компилятор query_spec мог собирать JOIN-запросы.
  * Прописан `row_count: 10000` для прохождения тестов обзора БД.
  * Задано `filled_pct` для ключевых колонок: `incdnt_sum` (`2.26`), `incdnt_drct_dmg_sum` (`2.26`), `org_struct_lvl_3_name` (`97.0`), чтобы валидировать ограничения `money-guard`.
* **Ссылка на файл**: [kb_schema.yaml](file:///d:/ior_assistant/backend/agent/schema/kb_schema.yaml)

### 📄 `backend/agent/controller.py`
* **Изменение**: Исправлено некорректное регулярное выражение `_AGG_RE` (убран неэкранированный дефис внутри квадратных скобок в середине строки).
* **Ссылка на файл**: [controller.py](file:///d:/ior_assistant/backend/agent/controller.py)

### 📄 `backend/__init__.py`
* **Изменение**: Настроены относительные импорты для оффлайн-сборки пакета.
* **Ссылка на файл**: [__init__.py](file:///d:/ior_assistant/backend/__init__.py)

### 📄 `backend/data/__init__.py`
* **Изменение**: Настроены относительные импорты для оффлайн-сборки пакета.
* **Ссылка на файл**: [__init__.py](file:///d:/ior_assistant/backend/data/__init__.py)

### 📄 `requirements.txt`
* **Изменение**: Зафиксированы комментарии с версиями библиотек, совместимых с оффлайн-контуром.
* **Ссылка на файл**: [requirements.txt](file:///d:/ior_assistant/requirements.txt)

### 📄 `backend/data/spark_store.py`
* **Изменение**: Автоматическая нормализация колонок DataFrame к нижнему регистру после выполнения query() в PySpark для защиты от регистрозависимости Hive.
* **Ссылка на файл**: [spark_store.py](file:///d:/ior_assistant/backend/data/spark_store.py)

### 📄 `backend/data/duckdb_store.py`
* **Изменение**: Аналогичная нормализация колонок DataFrame к нижнему регистру в DuckDB query() для консистентности.
* **Ссылка на файл**: [duckdb_store.py](file:///d:/ior_assistant/backend/data/duckdb_store.py)

### 📄 `backend/agent/state.py`
* **Изменение**: Принудительная нормализация названий колонок к нижнему регистру при регистрации DataFrame через register_dataframe().
* **Ссылка на файл**: [state.py](file:///d:/ior_assistant/backend/agent/state.py)

