import requests
import time
import json
import os
import re
import logging

logger = logging.getLogger(__name__)

SUMMARIZE_PROMPT_TEMPLATE = """
Ты - аналитик обращений клиентов банка. Твоя единственная задача - сформировать одну строку текста на основе списка кратких описаний обращений.

ВАЖНО: Ты должен вернуть ТОЛЬКО одну строку текста. Никакого JSON, никакого markdown, никаких списков, никаких заголовков, никаких пояснений.
Только одна строка.

ФОРМА ОТВЕТА (строго):
Ваши обращения по "[тема]", самые частые проблемы: [проблема 1], [проблема 2], [проблема 3].

ПРАВИЛА ФОРМИРОВАНИЯ ОТВЕТА:
1. Вместо [тема] подставь тему из запроса пользователя - коротко, 2-4 слова, в кавычках.
2. Вместо [проблема 1], [проблема 2], [проблема 3] подставь 3-4 самые частые или похожие проблемы из кратких описаний.
3. Каждая проблема - это короткая фраза 3-6 слов, без лишних деталей.
4. Проблемы перечисляются через запятую.
5. В конце строки ставь точку.
6. Используй только кириллицу, никаких специальных символов.
7. Не повторяй одну и ту же проблему дважды, даже если она сформулирована по-разному.
8. Если описаний мало (1-2 штуки) - всё равно выдай строку, просто с меньшим числом проблем.
9. Если все описания об одном и том же - укажи одну проблему.
10. В начале списка описаний даны ТОП-3 самых релевантных обращения - ориентируйся на них при выборе главных проблем, а не на частоту появления.
11. Если в транскрипциях диалогов есть явное решение проблемы (оператор или агент объяснил как решить) - добавь ВТОРОЙ строкой: "Возможное решение: [краткое описание решения]"

ПРИМЕРЫ ПРАВИЛЬНОГО ОТВЕТА:
Ваши обращения по "потере паспорта", самые частые проблемы: задержка восстановления документа, отказ в приеме на работу, некорректные данные.
Ваши обращения по "дебетовым картам", самые частые проблемы: блокировка карты без уведомления, ошибка при переводе средств, невозможность снятия.

ПРИМЕРЫ НЕПРАВИЛЬНОГО ОТВЕТА (никогда так не делай):
- {"result": "Ваши обращения..."} <- нельзя, это JSON
- Вот краткое резюме: Ваши обращения: ... <- нельзя, вводная фраза
- **Ваши обращения...** <- нельзя, markdown
- Ваши обращения по потере паспорта <- нельзя, должна быть в кавычках
- Проблемы клиентов: ... <- нельзя, другой формат

ПОЛУЧИВ СПИСОК ОПИСАНИЙ:
1. Прочитай все описания.
2. Найди повторяющиеся или похожие по смыслу проблемы.
3. Выбери топ 3-4 самых частых.
4. Сформируй одну строку строго по формату выше.
5. Проверь что в ответе нет ничего кроме этой одной строки.
6. Верни строку
"""

SYSTEM_PROMPT_TEMPLATE = """
Системный промт для извлечения параметров поиска

Задача:
Извлеки из пользовательского запроса три ключевых параметра для функции поиска: query, top_k и date_range. Если какой-либо параметр отсутствует, верни None.

Параметры и их описание:

1. query (Строка)
Что это: весь запрос пользователя, по которому будет производиться поиск.
Возвращаемое значение: Строка с текстом запроса.

2. top_k (Целое число)
Что это: количество документов, которое нужно вернуть в ответе.
Как извлекать:
- Ищи в тексте числовые значения, указывающие на количество (например, "покажи 5", "выведи 100", "топ 10").
- Если пользователь просит "все" или "много", или не указывает число - верни None.
- Если число указано в начале запроса (например, "100 обращений"), оно также должно быть извлечено как top_k.
Возвращаемое значение: Целое число (int) или None.

3. date_range (Кортеж строк)
Что это: Временной интервал для фильтрации документов.
Как извлекать:
- Ищи упоминания дат или временных интервалов. Формат дат должен быть строго YYYY-MM-DD.
- Если указаны две даты (например, "с 2023-01-01 по 2023-05-01"), возвращай кортеж (start_date, end_date).
- Если указана только одна дата и есть слова-маркеры ("до", "после", "раньше", "позже"), используй глобальные константы MIN_DATE и MAX_DATE.
- Если указан только год (например, "за 2023 год"), интерпретируй её как начало и конец года.
- Если дата указана без контекста интервала (например, "вчера"), интерпретируй её как начало и конец дня.
- Если пользователь указывает диапазон дат в формате "сегодня" или "вчера", такие запросы не обрабатываются - возвращай None для date_range.
Возвращаемое значение: Кортеж из двух строк (str, str) в формате (start_date, end_date) или None.

Формат вывода:
Верни результат в виде словаря Python в формате JSON.
- Используй только стандартные символы ASCII.
- Не добавляй невидимых символов (zero-width spaces и т.д.).
- Для дат используй строго формат YYYY-MM-DD.
- Если date_range это список, он должен быть в формате ["YYYY-MM-DD", "YYYY-MM-DD"].
Пример: {"query": "обращения по дебетовым картам", "top_k": null, "date_range": ["2025-01-01", "2025-12-31"]}

Примеры:
1. Запрос: "100 обращений по потере паспорта"
   Результат: {"query": "обращения по потере паспорта", "top_k": 100, "date_range": null}

2. Запрос: "Выведи 5 обращений по потере паспорта с 20 мая 2024 по 20 мая 2025"
   Результат: {"query": "обращения по потере паспорта", "top_k": 5, "date_range": ["2024-05-20", "2025-05-20"]}

3. Запрос: "Покажи все обращения за 2023 год"
   Результат: {"query": "обращения", "top_k": null, "date_range": ["2023-01-01", "2023-12-31"]}

4. Запрос: "Выведи обращения по потере паспорта с 10 февраля 2025 года по 10 февраля 2026"
   Результат: {"query": "обращения по потере паспорта", "top_k": null, "date_range": ["2025-02-10", "2026-02-10"]}

5. Запрос: "Выведи 15 обращений по потере паспорта"
   Результат: {"query": "обращения по потере паспорта", "top_k": 15, "date_range": null}

Важные замечания:
* Если пользователь просит вывести "все" документы, игнорируй параметр top_k (верни None), чтобы сработал дефолтный лимит поиска.
* Если дата указана в формате, отличном от YYYY-MM-DD, не извлекай её как часть date_range.
"""

GIGACHAT_API_URL = "http://liveaccess/v1/gc/chat/completions"
HEADERS = {
    "Authorization": f"Bearer {os.environ.get('JPY_API_TOKEN')}",
    "Content-Type": "application/json"
}

def def_ask_gigachat(messages: list) -> str:
    data = {
        "model": "GigaChat-3-Ultra",
        "messages": messages,
        "n": 1,
        "temperature": 0.01
    }
    attempt = 0
    while True:
        attempt += 1
        response = requests.post(GIGACHAT_API_URL, headers=HEADERS, json=data)
        if response.ok:
            return response.json()['choices'][0]['message']['content']
        elif not response.ok and attempt <= 20:
            time.sleep(1)
        else:
            raise RuntimeError(f"GigaChat error {response.status_code}: {response.text}")


def def_normalize_date_range(date_range):
    if not date_range or not isinstance(date_range, list) or len(date_range) != 2:
        return date_range

    def normalize_date(date_str):
        return date_str

    return [normalize_date(date_range[0]), normalize_date(date_range[1])]


# ------
def def_summarize_complaints(topic: str, descriptions: list[str],
                             scores: list[float] = None,
                             transcriptions: list[str] = None) -> str:
    if not descriptions:
        return ""

    if scores:
        scored = sorted(zip(scores, descriptions), reverse=True)
        top3_text = "\n".join(f"- {d}" for _, d in scored[:3] if d and str(d).strip())
    else:
        top3_text = "\n".join(f"- {d}" for d in descriptions[:3] if d and str(d).strip())

# ----------
    descriptions_text = "\n".join(
        f"{i+1}. {d}" for i, d in enumerate(descriptions) if d and str(d).strip()
    )

    valid_trans = []
    if transcriptions:
        valid_trans = [
            t[:600] for t in transcriptions
            if t and str(t).strip() and str(t).strip() != 'None'
        ]

    user_message = (
        f"Тема запроса: {topic}\n\n"
        f"ТОП-3 самых релевантных обращения (ориентируйся на них при определении главных проблем):\n{top3_text}\n\n"
        f"Краткие описания обращений ({len(descriptions)} штук):\n{descriptions_text}"
    )

    if valid_trans:
        trans_joined = "\n\n=== ===\n".join(valid_trans)
        user_message += (
            f"\n\nТранскрипции диалогов из базы ({len(valid_trans)} штук) - "
            f"ищи в них явные решения проблем:\n{trans_joined}"
        )

    try:
        response_text = def_ask_gigachat([
            {"role": "system", "content": SUMMARIZE_PROMPT_TEMPLATE},
            {"role": "user", "content": user_message}
        ])
# ------------
        result = response_text.strip()
        result = result.replace('**', '')
        result = result.strip('\"\'')
        print(f"Ответ LLM: {repr(result)}")
        return result
    except Exception as e:
        print(f"Ошибка саммаризации: {e}")
        return ""

summarize_complaints = def_summarize_complaints


def extract_search_params(message: str) -> dict:
    try:
        response_text = def_ask_gigachat([
            {"role": "system", "content": SYSTEM_PROMPT_TEMPLATE},
            {"role": "user", "content": message}
        ])
        
        print(f"Ответ LLM: {repr(response_text)}")
        
        cleaned_text = response_text.replace('\ufeff', '')
        cleaned_text = re.sub(r'[\u200b\u200c\u200d\u200e\u200f\u202a-\u202e\u2066-\u2069]', '', cleaned_text)
        cleaned_text = cleaned_text.replace(': None', ': null')
        cleaned_text = cleaned_text.replace(':None', ':null')
        cleaned_text = cleaned_text.replace(', None', ', null')
        
        json_match = re.search(r'\{.*?\}', cleaned_text, re.DOTALL)
        if not json_match:
            raise ValueError("JSON не найден в ответе LLM")
            
        params = json.loads(json_match.group(0))
        query = params.get("query") or message
        
        top_k = params.get("top_k")
        if isinstance(top_k, str):
            if top_k.isdigit():
                top_k = int(top_k)
            elif top_k.lower() in ["none", "null"]:
                top_k = None
                
        date_range = params.get("date_range")
        return {
            "query": query,
            "top_k": top_k,
            "date_range": date_range
        }
    except Exception as e:
        print(f"Ошибка при извлечении параметров через GigaChat: {e}")
        return {"query": message, "top_k": None, "date_range": None}
