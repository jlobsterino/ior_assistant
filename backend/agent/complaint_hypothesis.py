import pandas as pd
import logging
import asyncio
import re
from backend.gigachat_extractor import def_ask_gigachat

logger = logging.getLogger(__name__)

# Global in-memory cache for complaints in the session
_COMPLAINTS_SESSION_CACHE = {}

def _determine_complaint_route(session_id: str) -> str:
    session_data = _COMPLAINTS_SESSION_CACHE.get(session_id, {})
    id_to_text_map = session_data.get("id_to_text_map", {})
    if id_to_text_map and len(id_to_text_map) > 0:
        return "follow_up"
    return "analytical"


def profile_complaints_dataframe(df: pd.DataFrame) -> str:
    """
    Builds a structured text profile of the retrieved complaints.
    Includes temporal breakdown and lists the top complaints with their short descriptions 
    and snippets of the dialogue transcriptions to serve as LLM context.
    """
    if df.empty:
        return "Таблица обращений пуста."

    total_rows = len(df)
    lines = [f"Профиль данных обращений (Всего найдено: {total_rows} обращений):"]

    # 1. Temporal breakdown
    if "date" in df.columns:
        try:
            temp_df = df.copy()
            temp_df["date"] = pd.to_datetime(temp_df["date"], errors='coerce')
            temp_df = temp_df.dropna(subset=["date"])
            if not temp_df.empty:
                temp_df['month'] = temp_df['date'].dt.to_period('M')
                grp = temp_df.groupby('month').size().reset_index(name='count')
                lines.append("\nВременное распределение обращений:")
                for _, r in grp.iterrows():
                    m_pct = (r['count'] / total_rows) * 100
                    lines.append(f"- Месяц: {r['month']} | Количество обращений: {r['count']} ({m_pct:.1f}%)")
        except Exception as e:
            logger.warning(f"Ошибка при анализе дат обращений: {e}")

    # 2. Top 10 complaints details
    lines.append("\nТоп наиболее релевантных обращений для анализа:")
    top_n = df.head(10)
    for idx, row in top_n.iterrows():
        cid = row.get("id", "N/A")
        desc = row.get("Короткое описание", "—")
        score = row.get("score", 0.0)
        date_str = row.get("date", "—")
        dialogue = row.get("Транскрибация диалога", "—")

        # Truncate dialogue to prevent context length overflow
        if isinstance(dialogue, str) and len(dialogue) > 1000:
            dialogue_snippet = dialogue[:1000] + "..."
        else:
            dialogue_snippet = str(dialogue)

        lines.append(f"\n---")
        lines.append(f"Обращение #{idx+1} (ID: {cid}) | Дата: {date_str} | Скор релевантности: {score:.4f}")
        lines.append(f"Короткое описание: {desc}")
        lines.append(f"Транскрибация диалога: {dialogue_snippet}")

    return "\n".join(lines)


async def generate_complaint_hypothesis_narrative(user_msg: str, df: pd.DataFrame, file_info: dict) -> str:
    """
    Generates a natural language narrative (analytical report with hypotheses)
    about customer complaints using the GigaChat API.
    """
    if df.empty:
        return "По вашему запросу не найдено подходящих обращений в базе данных."

    profile_text = profile_complaints_dataframe(df)

    system_prompt = """Ты — ведущий эксперт-аналитик Службы контроля качества и клиентского опыта Сбербанка.
Твоя задача — провести глубокий анализ представленных обращений клиентов и сформулировать обоснованные аналитические гипотезы о корневых причинах (root causes) возникновения проблем.

Аналитическая гипотеза должна не просто пересказывать жалобы клиентов ("клиенты жалуются на кэшбэк"), а выявлять возможные системные сбои, технические ошибки, недостатки в процессах или обучении персонала.

Придерживайся следующей структуры отчета:

### 1. Выявленные проблемы и паттерны
- Классифицируй жалобы на 2-3 основные группы проблем.
- Укажи, какие именно аспекты вызывают наибольший негатив (на основе диалогов и описаний).

### 2. Анализ корневых причин (Root Cause Analysis)
- Для каждой группы проблем предложи теорию, почему она возникает (например, технический сбой на стороне шлюза, некорректная консультация оператора, баг в обновлении приложения, задержка обработки транзакции).
- Опирайся на детали из транскрибаций диалогов (например, если оператор долго не мог помочь или система выдавала конкретную ошибку).

### 3. Рекомендации и гипотезы для проверки
- Сформулируй 2-3 гипотезы с конкретными действиями для проверки. Например:
  * "Гипотеза о сбое интеграции с партнером X: клиенты не получают баллы при покупках с даты Y. Рекомендация: Проверить логи API-интеграции с партнером."
  * "Гипотеза о недостаточной информированности поддержки: операторы не знают о новых условиях акции X и дают неверные ответы. Рекомендация: Обновить базу знаний для операторов."

Пиши на русском языке, в профессиональном, аналитическом стиле. Оперируй только фактами и цифрами из предоставленных данных. Не используй общие фразы.
"""

    user_prompt = f"""Запрос пользователя: "{user_msg}"
Файл выгрузки: "{file_info.get('name', 'отчет.xlsx')}" (строк: {len(df)})

{profile_text}

Сформулируй гипотезу на основе этих данных. Пиши на русском языке, в профессиональном стиле."""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ]

    try:
        # Since def_ask_gigachat is synchronous and does network requests, run it in a separate thread
        response = await asyncio.to_thread(def_ask_gigachat, messages)
        return str(response)
    except Exception as e:
        logger.exception(f"Ошибка генерации гипотезы через GigaChat API: {e}")
        return f"Не удалось сгенерировать гипотезу из-за ошибки: {str(e)}"


def classify_complaint_intent(user_query: str) -> str:
    """
    Classifies the user query intent for complaints follow-up dialog.
    """
    system_prompt = """Ты - ИИ-классификатор интентов для чата по обращениям клиентов.
Пользователь уже получил выгрузку обращений и теперь задает следующий вопрос.
Твоя задача - определить, требует ли его запрос поиска/фильтрации конкретных обращений в локальной выгрузке по ключевым словам/теме, или же это просто продолжение диалога (вопрос по предыдущему ответу, просьба объяснить термин, уточнение, приветствие/спасибо).

Категории:
- "search": запрос требует поиска/фильтрации конкретных обращений по теме (например: "покажи жалобы на мобильное приложение", "выдели обращения про задержки", "какие клиенты жаловались на обслуживание?").
- "chat": запрос является продолжением диалога, вопросом по твоему предыдущему ответу, просьбой пояснить термины/слова, мета-вопросом или общим общением (например: "поясни второй пункт", "почему ты так решил?", "откуда эти данные?", "привет", "спасибо").

Ответь строго одним словом: "search" или "chat". Не пиши ничего, кроме этого слова.
"""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Запрос: \"{user_query}\"\nКатегория:"}
    ]
    try:
        response = def_ask_gigachat(messages).strip().lower()
        logger.info(f"[COMPLAINT INTENT] Classified intent as: {response}")
        if "search" in response:
            return "search"
        return "chat"
    except Exception as e:
        logger.error(f"[COMPLAINT INTENT] Classification failed: {e}")
        return "chat"


def search_complaints_cache(query: str, id_to_text_map: dict) -> list:
    """
    Ranks the cached complaints by simple keyword match overlap with the query.
    """
    words = [w.lower() for w in re.findall(r'[а-яёa-z0-9]+', query.lower()) if len(w) > 2]
    if not words:
        return list(id_to_text_map.values())

    scored = []
    for cid, item in id_to_text_map.items():
        desc = item.get("desc", "").lower()
        dialogue = item.get("dialogue", "").lower()
        
        score = 0
        for word in words:
            if word in desc:
                score += 5  # higher weight for short description match
            if word in dialogue:
                score += 1  # lower weight for dialogue match
                
        scored.append((score, item))
        
    scored.sort(key=lambda x: x[0], reverse=True)
    
    matches = [item for score, item in scored if score > 0]
    if matches:
        return matches[:15]
    return [item for score, item in scored[:15]]


def answer_complaint_details(user_query: str, complaints: list, history: list = None) -> str:
    """
    Answers questions about specific complaints (by ID).
    """
    formatted_texts = ""
    for item in complaints:
        cid = item.get("id")
        desc = item.get("desc", "")
        dialogue = item.get("dialogue", "")
        date_str = item.get("date", "")
        formatted_texts += f"--- Обращение ID: {cid} | Дата: {date_str} ---\nКороткое описание: {desc}\nТранскрибация диалога: {dialogue}\n\n"

    system_prompt = """Ты — ведущий эксперт-аналитик Службы контроля качества и клиентского опыта Сбербанка.
Твоя задача — проанализировать детальную информацию по конкретным обращениям клиентов (включая краткое описание и транскрибацию диалога) и ответить на вопросы пользователя.

Правила работы:
1. Отвечай только на основе предоставленного текста обращений. Не выдумывай факты.
2. Ссылайся на конкретные ID обращений при ответе.
3. Опиши суть проблемы клиента, действия оператора и результат обращения, если они есть.
4. Пиши в профессиональном стиле, лаконично и четко.
"""

    user_prompt = f"""Вопрос пользователя: "{user_query}"

Предоставленные обращения:
{formatted_texts}

Ответь на вопрос пользователя на основе предоставленных данных."""

    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_prompt})

    return def_ask_gigachat(messages)


def answer_complaint_follow_up(user_query: str, complaints: list, history: list = None) -> str:
    """
    Answers general follow-up questions about the entire retrieved set of complaints.
    """
    formatted_texts = ""
    for idx, item in enumerate(complaints[:15]):  # limit to top 15 to prevent context limit issues
        cid = item.get("id")
        desc = item.get("desc", "")
        dialogue = item.get("dialogue", "")
        # Truncate dialogue to keep prompt size reasonable
        if isinstance(dialogue, str) and len(dialogue) > 800:
            dialogue_snippet = dialogue[:800] + "..."
        else:
            dialogue_snippet = str(dialogue)
        formatted_texts += f"--- Обращение ID: {cid} ---\nОписание: {desc}\nДиалог (фрагмент): {dialogue_snippet}\n\n"

    system_prompt = """Ты — ведущий эксперт-аналитик Службы контроля качества и клиентского опыта Сбербанка.
Ты ведешь диалог на основе выгрузки клиентских обращений. Твоя задача — ответить на вопрос пользователя, используя предоставленный список обращений.

Правила:
1. Анализируй весь предоставленный список обращений для ответа на общие вопросы.
2. Если пользователь спрашивает, на основе чего сделаны выводы, покажи связь между гипотезами и конкретными обращениями (упоминай ID обращений).
3. Будь предельно объективен, опирайся только на факты из обращений.
"""

    user_prompt = f"""Вопрос пользователя: "{user_query}"

Список обращений для анализа:
{formatted_texts}

Ответь на вопрос пользователя."""

    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_prompt})

    return def_ask_gigachat(messages)


def answer_complaint_dialog(user_query: str, history: list) -> str:
    """
    Generates a dialog response using GigaChat, preserving context of the chat session.
    """
    system_prompt = """Ты — ведущий эксперт-аналитик Службы контроля качества и клиентского опыта Сбербанка.
Ты ведешь диалог с пользователем. Твои ответы должны основываться исключительно на истории сообщений.
Если тебя просят пояснить понятие, термин или предыдущие выводы, дай развернутое пояснение в профессиональном стиле.
"""

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_query})

    return def_ask_gigachat(messages)

