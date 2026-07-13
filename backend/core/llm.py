"""
GigaChat-сервис с rate-limiter.

Прямой HTTPх-клиент к GigaChat API (OpenAI-совместимый /v1/chat/completions),
БЕЗ langchain. Это устраняет цепочку транзитивных зависимостей
(langchain-core -> langchain_protocol -> typing.TypedDict(extra_items=...),
которая не работает на Python 3.12 без typing-extensions >= 4.13.

Rate-limiter: 1 запрос в gigachat_delay_sec секунд (см. .env, по умолчанию 6с)
- как в GigaChatApi.ipynb.

Если GIGACHAT_API_URL или JPY_API_TOKEN не заданы - режим заглушки.
"""
from __future__ import annotations

import json
import logging
import os
import time
from threading import Lock
from typing import Any, AsyncIterator, Optional

from backend.config import get_settings

logger = logging.getLogger(__name__)

_invoke_lock = Lock()
_last_invoke: float = 0.0


def _wait_rate_limit(delay_sec: float) -> None:
    """Глобальная задержка между вызовами LLM."""
    global _last_invoke
    with _invoke_lock:
        elapsed = time.perf_counter() - _last_invoke
        if elapsed < delay_sec:
            time.sleep(delay_sec - elapsed)


class GigaChatService:
    """Прямой HTTPх-клиент к GigaChat (OpenAI-compatible) + rate-limiter."""

    def __init__(self) -> None:
        self.cfg = get_settings()
        self._available = False
        self._init_llm()

    def _init_llm(self) -> None:
        api_url = self.cfg.gigachat_api_url or os.getenv("GIGACHAT_API_URL")
        token = self.cfg.jpy_api_token or os.getenv("JPY_API_TOKEN")

        if not api_url or not token:
            logger.warning(
                "[LLM] GigaChat не настроен (нет GIGACHAT_API_URL или JPY_API_TOKEN). "
                "Используется mock-режим."
            )
            self._available = False
            return

        try:
            import httpx
        except ImportError:
            logger.error("[LLM] httpx не установлен - pip install httpx")
            self._available = False
            return

        # /v1/chat/completions - Sber's GigaChat поддерживает OpenAI-compatible API.
        base = api_url.rstrip("/")
        self._chat_url = (base if base.endswith("/chat/completions")
                          else base + "/chat/completions")

        self._client = httpx.Client(
            timeout=httpx.Timeout(60.0, connect=10.0),
            verify=False,   # корп. сертификаты - verify=False как в langchain-варианте
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        self._available = True
        logger.info("[LLM] GigaChat HTTP-клиент готов: %s, model=%s",
                    self._chat_url, self.cfg.gigachat_model)

    @property
    def available(self) -> bool:
        return self._available

    def invoke(self, messages: list[dict[str, str]],
               temperature: Optional[float] = None) -> str:
        """Один синхронный вызов LLM.

        messages: [{"role": "system|user|assistant", "content": "..."}]
        temperature=0 для детерминированного планирования (необязательный).
        """
        if not self._available:
            return _mock_response(messages)

        _wait_rate_limit(self.cfg.gigachat_delay_sec)

        payload = {
            "model": self.cfg.gigachat_model,
            "messages": messages,
            "temperature": (temperature if temperature is not None
                            else self.cfg.gigachat_temperature),
            "stream": False,
        }
        import httpx
        retries = 3
        backoff = 2.0
        
        for attempt in range(retries):
            _wait_rate_limit(self.cfg.gigachat_delay_sec)
            try:
                resp = self._client.post(self._chat_url, json=payload)
                if resp.status_code == 429:
                    if attempt < retries - 1:
                        logger.warning("[LLM] Получен ответ 429 Too Many Requests. Попытка %d из %d. Ожидание %.1f сек...",
                                       attempt + 1, retries, backoff)
                        time.sleep(backoff)
                        backoff *= 2.0
                        continue
                resp.raise_for_status()
                data = resp.json()
                # OpenAI-compatible: {"choices":[{"message":{"content":"..."}}]}
                choices = data.get("choices") or []
                if choices and isinstance(choices, list):
                    msg = choices[0].get("message") or {}
                    return msg.get("content", "")
                # GigaChat иногда возвращает {"response": "..."} как fallback
                if "response" in data:
                    return data["response"]
                logger.warning("[LLM] Неожиданный формат ответа: %s",
                               json.dumps(data, ensure_ascii=False)[:300])
                return ""
            except httpx.HTTPStatusError as status_err:
                if status_err.response.status_code == 429 and attempt < retries - 1:
                    logger.warning("[LLM] HTTPStatusError 429. Попытка %d из %d. Ожидание %.1f сек...",
                                   attempt + 1, retries, backoff)
                    time.sleep(backoff)
                    backoff *= 2.0
                    continue
                logger.exception("[LLM] HTTP status error: %s", status_err)
                return f"[Ошибка LLM: {status_err}]"
            except Exception as e:
                logger.exception("[LLM] Ошибка вызова GigaChat: %s", e)
                return f"[Ошибка LLM: {e}]"
            finally:
                global _last_invoke
                with _invoke_lock:
                    _last_invoke = time.perf_counter()

    def invoke_json(self, messages: list[dict[str, str]],
                    fallback: Optional[dict] = None) -> dict:
        """Вызов LLM с разбором JSON-ответа.

        Робастен к типичным ошибкам LLM:
        • ```json ... ``` обёртка
        • Текст до/после JSON-объекта
        • Control characters внутри строк (русский reasoning часто
          ломает json.loads из-за непечатных символов)
        • Smart quotes (« » ' '), trailing запятые
        • Незакрытые строки - последний resort: regex-extract ключевых полей
        """
        raw = self.invoke(messages)
        parsed = _parse_llm_json(raw)
        if parsed is not None:
            return parsed

        # - Не получилось распарсить даже robust-методами -
        # Последний шанс: regex-извлечем skill_id / confidence /
        # любые другие плоские поля. Это спасает от ситуации, когда
        # GigaChat выдал верный skill_id, но сломал reasoning-поле.
        fallback_extracted = _regex_extract_flat_fields(raw)
        if fallback_extracted:
            logger.warning(
                "[LLM] JSON битый, но regex-извлёк поля: %s",
                fallback_extracted,
            )
            return fallback_extracted

        logger.warning("[LLM] Не удалось распарсить JSON. Raw: %s", raw[:400])
        return fallback or {"_error": "json_parse_failed", "_raw": raw}


# ------- JSON-парсинг LLM-ответов (вынесено из класса) -------


def _parse_llm_json(raw: str) -> Optional[dict]:
    """Пытается распарсить JSON-объект из LLM-ответа разными способами.

    Возвращает dict или None если ни один способ не сработал.
    """
    import re

    if not raw:
        return None

    # Шаг 1: убрать ```json ... ``` обёртку
    fence = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw, re.DOTALL)
    candidate = fence.group(1) if fence else raw

    # Шаг 2: найти первый сбалансированный {...} объект
    candidate = _find_first_json_object(candidate) or candidate
    candidate = candidate.strip()
    if not candidate.startswith("{"):
        return None

    # Шаг 3: пробуем как есть
    try:
        return json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        pass

    # Шаг 4: чистим control characters внутри строк
    # (главный виновник падений на русском reasoning от GigaChat)
    cleaned = _strip_control_chars(candidate)
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        pass

    # Шаг 5: чистим smart quotes + trailing запятые
    cleaned2 = (cleaned
                .replace("«", '"').replace("»", '"')      # « »
                .replace("“", '"').replace("”", '"')      # “ ”
                .replace("‘", '"').replace("’", '"'))     # ‘ ’
    cleaned2 = re.sub(r",\s*([\]}])", r"\1", cleaned2)
    try:
        return json.loads(cleaned2)
    except (json.JSONDecodeError, ValueError):
        return None


def _find_first_json_object(s: str) -> Optional[str]:
    """Балансируя {}, выделяет первый JSON-объект из текста."""
    start = s.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(s)):
        c = s[i]
        if escape:
            escape = False
            continue
        if c == "\\":
            escape = True
            continue
        if c == '"' and not escape:
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return None


def _strip_control_chars(s: str) -> str:
    """Убирает непечатные ASCII (кроме \\t 1\n 1\г) - частая причина
    падения json.loads на русском reasoning."""
    result = []
    in_string = False
    escape = False
    for c in s:
        if escape:
            result.append(c)
            escape = False
            continue
        if c == "\\":
            result.append(c)
            escape = True
            continue
        if c == '"':
            in_string = not in_string
            result.append(c)
            continue
        if in_string and ord(c) < 0x20 and c not in "\t\n\r":
            continue # пропускаем мусорные control chars внутри строк
        if in_string and c in "\n\r":
            result.append(" ") # переводы строк внутри строки - пробел
            continue
        result.append(c)
    return "".join(result)

def _regex_extract_flat_fields(raw: str) -> dict:
    """Спасательный круг: regex-вытаскивает плоские поля даже из
    сломанного JSON. Возвращает то, что удалось найти."""
    import re
    result: dict = {}
    # skill_id: строковое значение
    m = re.search(r'"skill_id"\s*:\s*"([^"]+)"', raw)
    if m:
        result["skill_id"] = m.group(1)
    # confidence: число
    m = re.search(r'"confidence"\s*:\s*([0-9.]+)', raw)
    if m:
        try:
            result["confidence"] = float(m.group(1))
        except ValueError:
            pass
    # incdnt_sid: EVE-... (для извлечения параметров)
    m = re.search(r'"incdnt_sid"\s*:\s*"(EVE-[0-9A-Za-z\-]+)"', raw)
    if m:
        result["incdnt_sid"] = m.group(1)
    # incdnt_entry_dt_begin/end: даты вида YYYY-MM-DD
    for key in ("incdnt_entry_dt_begin", "incdnt_entry_dt_end"):
        m = re.search(rf'"{key}"\s*:\s*"(\d{{4}}-\d{{2}}-\d{{2}})"', raw)
        if m:
            result[key] = m.group(1)
    return result


# — Mock-режим (статический метод, вне класса) ————————————————————————


def _mock_response(messages: list[dict[str, str]]) -> str:
    """Простая заглушка — возвращает что-то осмысленное на основе последнего сообщения."""
    if not messages:
        return "Нет сообщений."
    last = messages[-1]["content"].lower()
    sys_prompt = (messages[0].get("content", "") if messages else "").lower()

    # — Agent v2: Controller ReAct loop mock —
    if "агент-аналитик" in sys_prompt or "строго один json-объект" in sys_prompt:
        import json as _json
        import re
        q_match = re.search(r'запрос пользователя:\s*"([^"]+)"', messages[-1].get("content", ""))
        user_q = (q_match.group(1) if q_match else messages[-1].get("content", "")).lower()

        # Если в запросе пользователя EVE-номер, запускаем preset досье
        if re.search(r"\beve-\d+", user_q):
            sid_m = re.search(r"\beve-\d+", user_q, re.IGNORECASE)
            sid = sid_m.group(0).upper() if sid_m else "EVE-0000000"
            return _json.dumps({
                "thought": "Запрос по конкретному SID, собираю досье",
                "action": "run_preset",
                "args": {
                    "skill_id": "report_period_specific_ior_v2",
                    "params": {"incdnt_sid": sid}
                }
            }, ensure_ascii=False)

        # Если в запросе возмещения
        if "возмещ" in user_q or "возврат" in user_q:
            return _json.dumps({
                "thought": "Запрос по возмещениям, запускаю пресет",
                "action": "run_preset",
                "args": {
                    "skill_id": "vozmeshenie_ior_v2",
                    "params": {"incdnt_entry_dt_begin": "2025-01-01", "incdnt_entry_dt_end": "2025-12-31"}
                }
            }, ensure_ascii=False)

        # Если в запросе нефинансовые
        if "нефин" in user_q or "репутац" in user_q or "регулятор" in user_q:
            return _json.dumps({
                "thought": "Запрос по нефинансовым последствиям, запускаю пресет",
                "action": "run_preset",
                "args": {
                    "skill_id": "ior_nonfinancial_consequences_v2",
                    "params": {"incdnt_entry_dt_begin": "2025-01-01", "incdnt_entry_dt_end": "2025-12-31"}
                }
            }, ensure_ascii=False)

        # Если в запросе финансовые
        if "финансов" in user_q or "прямы" in user_q or "косвен" in user_q:
            return _json.dumps({
                "thought": "Запрос по финансовым последствиям, запускаю пресет",
                "action": "run_preset",
                "args": {
                    "skill_id": "financial_consequences_ior_v2",
                    "params": {"incdnt_entry_dt_begin": "2025-01-01", "incdnt_entry_dt_end": "2025-12-31"}
                }
            }, ensure_ascii=False)

        # По умолчанию - общая выгрузка по всем ИОРам (run_query_spec)
        return _json.dumps({
            "thought": "Выполняю выгрузку инцидентов за период",
            "action": "run_query_spec",
            "args": {
                "spec": {
                    "version": 1,
                    "source": {"table": "d6_base_of_knowledge_ior"},
                    "filters": [{"kind": "period", "intent": {"text": "Q1 2025"}, "column": "incdnt_entry_dt", "required": True}],
                    "output": {"format": "excel", "name": "Выгрузка ИОР Q1 2025"}
                }
            }
        }, ensure_ascii=False)

    # — Agent v2: Planner-промпт —
    # Детект: system prompt содержит «планировщик», user prompt содержит
    # «запрос пользователя:» в конце. Возвращаем минимальный валидный план.
    if "планировщик" in sys_prompt or "rationale" in sys_prompt:
        import re
        q_match = re.search(r"#? запрос пользователя:\s*\n?(.+?)(?:\n|$)",
                            messages[-1].get("content", ""), re.IGNORECASE | re.DOTALL)
        user_q = (q_match.group(1).strip() if q_match else "").lower()

        # — Multi-step: "топ-N" / "незакрытые" / "больше X" / "по сумме" —
        # Это запросы которые НЕ покрыты preset'ами и должны идти через
        # query + filter + top_n + export.
        wants_top = bool(re.search(r"топ[-\s]*(?:\d+)?|самы[ех]\s+(?:дорог|больш|круп)", user_q))
        wants_unclosed = "незакры" in user_q or "не закры" in user_q
        wants_filter_sum = bool(re.search(r"больше\s+\d+|>\s*\d+|\s*свыше\s+\d+|\s*более\s+\d+", user_q))

        if wants_top or wants_unclosed or wants_filter_sum:
            import json as _json
            # ТБ-фильтр
            where = {}
            if "сзб" in user_q or "северо" in user_q:
                where["org_struct_lvl_2_name_like"] = "%Северо-Западный%"
            elif "сиб" in user_q or "сибир" in user_q:
                where["org_struct_lvl_2_name_like"] = "%Сибирский%"
            elif "юэб" in user_q or "юго-за" in user_q:
                where["org_struct_lvl_2_name_like"] = "%Юго-Западный%"

            # Период
            year_m = re.search(r"\b(20\d{2})\b", user_q)
            year = int(year_m.group(1)) if year_m else 2025
            q1 = "q1" in user_q or ("первы" in user_q and "кварт" in user_q)
            q2 = "q2" in user_q or ("втор" in user_q and "кварт" in user_q)
            q3 = "q3" in user_q or ("трет" in user_q and "кварт" in user_q)
            q4 = "q4" in user_q or ("четвёрт" in user_q and "кварт" in user_q)
            if q1:
                where["incdnt_entry_dt"] = {">=": f"{year}-01-01", "<": f"{year}-04-01"}
            elif q2:
                where["incdnt_entry_dt"] = {">=": f"{year}-04-01", "<": f"{year}-07-01"}
            elif q3:
                where["incdnt_entry_dt"] = {">=": f"{year}-07-01", "<": f"{year}-10-01"}
            elif q4:
                where["incdnt_entry_dt"] = {">=": f"{year}-10-01", "<": f"{year+1}-01-01"}
            else:
                where["incdnt_entry_dt"] = {">=": f"{year}-01-01", "<": f"{year+1}-01-01"}

            n_match = re.search(r"топ[-\s]*(\d+)", user_q)
            n = int(n_match.group(1)) if n_match else 10

            steps = [
                {
                    "id": "s1", "tool": "query",
                    "args": {
                        "table": "d6_base_of_knowledge_ior",
                        "where": where,
                        "columns": ["incdnt_sid", "incdnt_entry_dt",
                                    "org_struct_lvl_2_name",
                                    "incdnt_type_lvl1_name",
                                    "incdnt_type_lvl2_name",
                                    "incdnt_sum", "incdnt_status_name"],
                        "order_by": "incdnt_sum", "order_desc": True,
                        "limit": 5000,
                    },
                    "depends_on": [], "produces": "df_1"
                },
                {
                    "id": "s2", "tool": "top_n",
                    "args": {"df_id": "df_1", "by": "incdnt_sum", "n": n},
                    "depends_on": ["s1"], "produces": "df_2"
                }
            ]
            last_df = "df_2"
            last_step = "s2"
            if wants_unclosed:
                steps.append({
                    "id": "s3", "tool": "filter_df",
                    "args": {"df_id": "df_2",
                             "where": "incdnt_status_name != 'Закрыт'"},
                    "depends_on": ["s2"], "produces": "df_3"
                })
                last_df = "df_3"
                last_step = "s3"
            steps.append({
                "id": f"s{len(steps)+1}", "tool": "export_excel",
                "args": {"df_id": last_df,
                         "name": f"Топ-{n} "
                                 + ("незакрытых " if wants_unclosed else "")
                                 + "ИОР"},
                "depends_on": [last_step], "produces": "file_1"
            })
            plan = {
                "rationale": ("Композит: query -> top_n"
                              + (" + filter (незакрытые)" if wants_unclosed else "")
                              + " -> export."),
                "steps": steps,
                "expected_duration_sec": 15,
            }
            return _json.dumps(plan, ensure_ascii=False)

        # SID-запрос -> preset досье
        if re.search(r"\beve-\d+", user_q):
            sid_m = re.search(r"\beve-\d+", messages[-1].get("content", ""),
                              re.IGNORECASE)
            sid = sid_m.group(0).upper() if sid_m else "EVE-0000000"
            return ( '{"rationale":"Запрос по конкретному SID - preset досье",'
                     '"steps":[{"id":"s1","tool":"run_preset","args":'
                     '"{\\"skill_id\\":\\"report_period_specific_ior_v2\\",'
                     f'\\"params\\":{{\\"incdnt_sid\\":\\"{sid}\\"}}",'
                     '"depends_on":[],"produces":"file_1"}],'
                     '"expected_duration_sec":30}' )

        # Возмещения
        if "возмещ" in user_q or "возврат" in user_q:
            return ('{"rationale":"Возмещения за период","steps":[{"id":"s1", '
                    '"tool":"run_preset","args":"{\\"skill_id\\":\\"vozmeshenie_ior_v2\\", '
                    '\\"params\\":{\\"incdnt_entry_dt_begin\\":\\"2025-01-01\\", '
                    '\\"incdnt_entry_dt_end\\":\\"2025-12-31\\"}}", "depends_on":[], '
                    '"produces":"file_1"}],"expected_duration_sec":60}')

        # Удалённые
        if "удал" in user_q:
            return ('{"rationale":"Удалённые ИОР за период","steps":[{"id":"s1", '
                    '"tool":"run_preset","args":"{\\"skill_id\\":\\"deleted_ior_v2\\", '
                    '\\"params\\":{\\"incdnt_entry_dt_begin\\":\\"2025-01-01\\", '
                    '\\"incdnt_entry_dt_end\\":\\"2025-03-31\\"}}", "depends_on":[], '
                    '"produces":"file_1"}],"expected_duration_sec":90}')

        # Нефин
        if "нефин" in user_q or "репутац" in user_q or "регулятор" in user_q:
            return ('{"rationale":"Нефин. последствия за период","steps":[{"id":"s1", '
                    '"tool":"run_preset","args":"{\\"skill_id\\":\\"ior_nonfinancial_consequences_v2\\", '
                    '\\"params\\":{\\"incdnt_entry_dt_begin\\":\\"2025-01-01\\", '
                    '\\"incdnt_entry_dt_end\\":\\"2025-12-31\\"}}", "depends_on":[], '
                    '"produces":"file_1"}],"expected_duration_sec":60}')

        # Фин
        if "финансов" in user_q or "прямы" in user_q or "косвен" in user_q:
            return ('{"rationale":"Фин. последствия за период","steps":[{"id":"s1", '
                    '"tool":"run_preset","args":"{\\"skill_id\\":\\"financial_consequences_ior_v2\\", '
                    '\\"params\\":{\\"incdnt_entry_dt_begin\\":\\"2025-01-01\\", '
                    '\\"incdnt_entry_dt_end\\":\\"2025-12-31\\"}}", "depends_on":[], '
                    '"produces":"file_1"}],"expected_duration_sec":60}')

        # Default — общий отчёт за год
        return ('{"rationale":"Общая выгрузка ИОР за период","steps":[{"id":"s1", '
                '"tool":"run_preset","args":"{\\"skill_id\\":\\"ior_period_pao_sberbank_v2\\", '
                '\\"params\\":{\\"incdnt_entry_dt_begin\\":\\"2025-01-01\\", '
                '\\"incdnt_entry_dt_end\\":\\"2025-12-31\\"}}", "depends_on":[], '
                '"produces":"file_1"}],"expected_duration_sec":60}')

    # — Agent v2: Reflector-промпт —
    if "исправляешь ошибки" in sys_prompt or "correction" in sys_prompt:
        return '{"action":"skip","reasoning":"mock reflector - skip"}'

    # — Agent v2: Narrator-промпт —
    if "аудитор-аналитик" in sys_prompt:
        return ("Готово. Запрос обработан, отчёт сформирован — файл "
                "прикреплён ниже. _GigaChat в mock-режиме, реальный "
                "текст придёт когда подключим API._")

    # Имитация маршрутизации — ищем триггеры из пользовательского запроса.
    # Чтобы не путать с «выгрузи», цепляемся за «запрос пользователя:» блок.
    if "skill" in last and "json" in last:
        import re
        q_match = re.search(r'запрос пользователя:\s*"([^"]+)"', last)
        user_q = (q_match.group(1) if q_match else last).lower()
        if re.search(r"\beve-\d+", user_q):
            return '{"skill_id": "report_period_specific_ior_v2", "confidence": 0.95, "reasoning": "mock SID"}'
        if "удал" in user_q or "снесли" in user_q or "deleted" in user_q:
            return '{"skill_id": "deleted_ior_v2", "confidence": 0.9, "reasoning": "mock"}'
        if "возмещ" in user_q or "возврат" in user_q or "компенсац" in user_q:
            return '{"skill_id": "vozmeshenie_ior_v2", "confidence": 0.9, "reasoning": "mock"}'
        if "нефин" in user_q or "репутац" in user_q or "регулятор" in user_q or "жалоб" in user_q:
            return '{"skill_id": "ior_nonfinancial_consequences_v2", "confidence": 0.9, "reasoning": "mock"}'
        if "прямы" in user_q or "косвен" in user_q or "третьих лиц" in user_q or "финансов" in user_q:
            return '{"skill_id": "financial_consequences_ior_v2", "confidence": 0.85, "reasoning": "mock"}'
        return '{"skill_id": "ior_period_pao_sberbank_v2", "confidence": 0.7, "reasoning": "mock (default)"}'

    if "извлеки параметры" in last or ("параметр" in last and "schema" in last):
        import re
        q_match = re.search(r'запрос пользователя:\s*"([^"]+)"', last)
        user_q = (q_match.group(1) if q_match else last)
        params: dict = {}

        sid_match = re.search(r"\bEVE-\d+\b", user_q, re.IGNORECASE)
        if sid_match:
            params["incdnt_sid"] = sid_match.group(0).upper()

        lower = user_q.lower()
        begin = end = None
        year_match = re.search(r"\b(20\d{2})\b", user_q)
        if "q1" in lower or "1 кварт" in lower:
            y = year_match.group(1) if year_match else "2025"
            begin, end = f"{y}-01-01", f"{y}-03-31"
        elif "q2" in lower or "2 кварт" in lower:
            y = year_match.group(1) if year_match else "2025"
            begin, end = f"{y}-04-01", f"{y}-06-30"
        elif "q3" in lower or "3 кварт" in lower:
            y = year_match.group(1) if year_match else "2025"
            begin, end = f"{y}-07-01", f"{y}-09-30"
        elif "q4" in lower or "4 кварт" in lower:
            y = year_match.group(1) if year_match else "2025"
            begin, end = f"{y}-10-01", f"{y}-12-31"
        elif "январ" in lower:
            y = year_match.group(1) if year_match else "2025"
            begin, end = f"{y}-01-01", f"{y}-01-31"
        elif year_match:
            y = year_match.group(1)
            begin, end = f"{y}-01-01", f"{y}-12-31"

        if begin and "incdnt_entry_dt_begin" not in params:
            params["incdnt_entry_dt_begin"] = begin
            params["incdnt_entry_dt_end"] = end

        return json.dumps(params, ensure_ascii=False)

    # обычный ответ
    return (
        "🤖 _GigaChat отключён (mock-режим)._\n\n"
        "Это заглушка для разработки. После настройки `.env` (GIGACHAT_API_URL + JPY_API_TOKEN) "
        "тут будет реальный ответ ассистента."
    )


# — Ollama backend (Local development) ————————————————————————————


class FireworksService:
    """Fireworks.ai LLM-backend (OpenAI-compatible /v1/chat/completions).

    Хостит большие модели — qwen2.5-72b-instruct, deepseek-v3, llama-3.3-70b
    и т.д. Сильно лучше Ollama qwen-7b для structured output.

    Конфиг:
      FIREWORKS_API_KEY=fw_xxxxx
      FIREWORKS_MODEL=accounts/fireworks/models/qwen2p5-72b-instruct (default)
      FIREWORKS_BASE_URL=https://api.fireworks.ai/inference/v1 (default)
      FIREWORKS_TIMEOUT=60 (default)

    Поддерживает response_format={"type": "json_object"} для надёжного JSON.
    """

    DEFAULT_MODEL = "accounts/fireworks/models/deepseek-v2-pro"
    DEFAULT_BASE_URL = "https://api.fireworks.ai/inference/v1"

    def __init__(self) -> None:
        self.api_key = os.environ.get("FIREWORKS_API_KEY", "").strip()
        self.base_url = os.environ.get(
            "FIREWORKS_BASE_URL", self.DEFAULT_BASE_URL,
        ).rstrip("/")
        self.model = os.environ.get("FIREWORKS_MODEL", self.DEFAULT_MODEL)
        self.timeout = float(os.environ.get("FIREWORKS_TIMEOUT", "90"))
        self.temperature = float(
            os.environ.get("FIREWORKS_TEMPERATURE", "0.1"),
        )
        self._client = None
        self._available = False
        self._init()

    def _init(self) -> None:
        if not self.api_key:
            logger.warning(
                "[LLM/Fireworks] FIREWORKS_API_KEY не задан — fallback в mock",
            )
            return
        try:
            import httpx
        except ImportError:
            logger.error("[LLM/Fireworks] httpx не установлен")
            return
        
        self._client = httpx.Client(
            timeout=self.timeout,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        self._available = True
        # ключ не логируем — только хвост
        tail = self.api_key[-6:] if len(self.api_key) >= 6 else "***"
        logger.info("[LLM/Fireworks] готов: %s, model=%s, key=...%s",
                    self.base_url, self.model, tail)

    @property
    def available(self) -> bool:
        return self._available

    def invoke(self, messages: list[dict[str, str]],
               temperature: Optional[float] = None) -> str:
        """Один синхронный вызов. JSON-mode подключаем если в last user-msg
        упомянуто 'json'/'верни строго' или в system-prompt 'rationale'.
        temperature=0 для детерминированного планирования (необязательный)."""
        if not self._available:
            return _mock_response(messages)

        last = (messages[-1].get("content", "") if messages else "").lower()
        first_sys = messages[0].get("content", "").lower() if messages else ""
        wants_json = (
            "json" in last
            or ("верни" in last and "строго" in last)
            or "rationale" in first_sys
            or "агент-аналитик" in first_sys
            or "correction" in first_sys
            or "планировщик" in first_sys
        )

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": (temperature if temperature is not None 
                            else self.temperature),
            "max_tokens": 4096,
        }
        if wants_json:
            payload["response_format"] = {"type": "json_object"}

        try:
            r = self._client.post(
                f"{self.base_url}/chat/completions", json=payload,
            )
            if r.status_code == 401:
                logger.error(
                    "[LLM/Fireworks] 401 Unauthorized — проверь FIREWORKS_API_KEY. "
                    "Fallback в mock.",
                )
                return _mock_response(messages)
            if r.status_code == 404:
                logger.error(
                    "[LLM/Fireworks] 404 — модель %r не найдена. "
                    "Список: https://fireworks.ai/models", self.model,
                )
                return _mock_response(messages)
            if r.status_code == 429:
                logger.warning(
                    "[LLM/Fireworks] 429 rate-limited — fallback в mock",
                )
                return _mock_response(messages)
            r.raise_for_status()
            data = r.json()
            choices = data.get("choices") or []
            if not choices:
                logger.warning("[LLM/Fireworks] пустой ответ: %s", data)
                return _mock_response(messages)
            msg = choices[0].get("message") or {}
            return msg.get("content", "") or ""
        except Exception as e:  # noqa: BLE001
            logger.warning("[LLM/Fireworks] ошибка (%s) — fallback в mock", e)
            return _mock_response(messages)

    def invoke_json(self, messages, fallback=None):
        """Аналог GigaChatService.invoke_json — пытается распарсить JSON."""
        raw = self.invoke(messages)
        parsed = _parse_llm_json(raw)
        if parsed is not None:
            return parsed
        fallback_extracted = _regex_extract_flat_fields(raw)
        if fallback_extracted:
            return fallback_extracted
        logger.warning("[LLM/Fireworks] не распарсил JSON. Raw: %s", raw[:400])
        return fallback or {"_error": "json_parse_failed", "_raw": raw}


class OllamaService:
    """Локальный LLM-backend через Ollama (для разработки на личном PC).

    Использует Ollama HTTP API - /api/chat с параметром format='json' для
    надёжного JSON-output (нужен Planner/Reflector). Совместим по API
    с GigaChatService — invoke() / invoke_json() / .available.

    Установка:
      brew install ollama
      ollama pull qwen2.5:7b-instruct    # ~4.7 GB, отлично для M-серии
      # или для 16GB+: qwen2.5:14b-instruct
    """

    def __init__(self) -> None:
        self.base_url = os.environ.get("OLLAMA_BASE_URL",
                                       "http://localhost:11434").rstrip("/")
        self.model = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b-instruct")
        self.timeout = float(os.environ.get("OLLAMA_TIMEOUT", "120"))
        self._client = None
        self._available = False
        self._init()

    def _init(self) -> None:
        try:
            import httpx
        except ImportError:
            logger.error("[LLM/Ollama] httpx не установлен")
            return
        self._client = httpx.Client(timeout=self.timeout)
        # Проверка что Ollama жив (быстрый ping)
        try:
            r = self._client.get(f"{self.base_url}/api/tags")
            if r.status_code != 200:
                logger.warning(
                    "[LLM/Ollama] /api/tags вернул %d — Ollama не отвечает",
                    r.status_code,
                )
                return
            models = [m.get("name") for m in r.json().get("models", [])]
            if not any(self.model in (m or "") for m in models):
                logger.warning(
                    "[LLM/Ollama] модель %s не загружена. Доступны: %s. "
                    "Запустите: ollama pull %s",
                    self.model, models, self.model,
                )
            # Всё равно ставим available=True — Ollama может pull on demand
            self._available = True
            logger.info("[LLM/Ollama] готов: %s, model=%s",
                        self.base_url, self.model)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "[LLM/Ollama] недоступен (%s). Запустите: brew services start ollama",
                e,
            )

    @property
    def available(self) -> bool:
        return self._available

    def invoke(self, messages: list[dict[str, str]],
               temperature: Optional[float] = None) -> str:
        """Один синхронный вызов Ollama. При 404 (модель ещё не скачана)
        — fallback на mock (это даёт работу даже пока ollama pull идёт).
        temperature=0 для детерминированного планирования (необязательный)."""
        if not self._available:
            return _mock_response(messages)

        last = (messages[-1].get("content", "") if messages else "").lower()
        wants_json = (
            "json" in last
            or ("верни" in last and "строго" in last)
            or any("планировщик" in m.get("content", "").lower()
                   or "rationale" in m.get("content", "").lower()
                   or "агент-аналитик" in m.get("content", "").lower()
                   or "correction" in m.get("content", "").lower()
                   for m in messages[:1])
        )

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": (temperature if temperature is not None 
                                        else 0.1)},
        }
        if wants_json:
            payload["format"] = "json"

        try:
            r = self._client.post(f"{self.base_url}/api/chat", json=payload)
            if r.status_code == 404:
                logger.warning(
                    "[LLM/Ollama] модель %s не найдена (404). Fallback в mock. "
                    "Запустите: ollama pull %s", self.model, self.model,
                )
                return _mock_response(messages)
            r.raise_for_status()
            data = r.json()
            msg = data.get("message") or {}
            return msg.get("content", "") or ""
        except Exception as e:  # noqa: BLE001
            logger.warning("[LLM/Ollama] ошибка (%s) — fallback в mock", e)
            return _mock_response(messages)

    def invoke_json(self, messages, fallback=None):
        """Аналог GigaChatService.invoke_json."""
        raw = self.invoke(messages)
        parsed = _parse_llm_json(raw)
        if parsed is not None:
            return parsed
        fallback_extracted = _regex_extract_flat_fields(raw)
        if fallback_extracted:
            return fallback_extracted
        logger.warning("[LLM/Ollama] не распарсил JSON. Raw: %s", raw[:400])
        return fallback or {"_error": "json_parse_failed", "_raw": raw}


# — Singleton + Factory —————————————————————————————————————————————


_service = None


def get_llm():
    """Выбор LLM-backend'а по env.

    LLM_BACKEND=fireworks -> Fireworks.ai (hosted, OpenAI-compatible)
    LLM_BACKEND=ollama    -> Ollama (local, для разработки)
    LLM_BACKEND=gigachat  -> GigaChat (prod в Сбере)
    (по умолчанию: fireworks если задан FIREWORKS_API_KEY;
                   ollama если APP_ENV=local;
                   gigachat иначе)
    """
    global _service
    if _service is not None:
        return _service

    backend = (os.environ.get("LLM_BACKEND") or "").lower()
    if not backend:
        if os.environ.get("FIREWORKS_API_KEY"):
            backend = "fireworks"
        elif os.environ.get("APP_ENV") == "local":
            backend = "ollama"
        else:
            backend = "gigachat"

    if backend == "gigachat":
        _service = GigaChatService()
    elif backend == "fireworks":
        _service = FireworksService()
    elif backend == "ollama":
        _service = OllamaService()
    else:
        logger.warning("[LLM] Неизвестный бэкенд %r, использую gigachat", backend)
        _service = GigaChatService()

    logger.info("[LLM] backend = %s", backend)
    return _service


def reset_llm() -> None:
    """Для тестов — пересоздать singleton."""
    global _service
    _service = None