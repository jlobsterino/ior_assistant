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


def extract_short_descriptions_summary(df: pd.DataFrame, max_unique: int = 200) -> str:
    """
    Extracts, normalizes, and tallies short descriptions ('Короткое описание') from the dataset.
    Returns a frequency table of up to max_unique top short descriptions.
    """
    if "Короткое описание" not in df.columns or df["Короткое описание"].dropna().empty:
        return "Короткие описания отсутствуют."
    
    descs = df["Короткое описание"].dropna().astype(str).str.strip()
    descs = descs[descs != ""]
    if descs.empty:
        return "Короткие описания отсутствуют."

    total_count = len(descs)
    freq_map = {}
    for d in descs:
        key = d.lower()
        if key not in freq_map:
            freq_map[key] = {"display": d, "count": 0}
        freq_map[key]["count"] += 1
    
    sorted_items = sorted(freq_map.values(), key=lambda x: x["count"], reverse=True)[:max_unique]
    
    lines = [f"Частотная статистика коротких описаний (всего проанализировано {total_count} обращений, выведено топ-{len(sorted_items)} уникальных тем):"]
    for item in sorted_items:
        cnt = item["count"]
        pct = (cnt / total_count) * 100
        lines.append(f"- \"{item['display']}\": {cnt} обращений ({pct:.1f}%)")
    
    return "\n".join(lines)


def profile_complaints_dataframe(df: pd.DataFrame, df_batch: pd.DataFrame = None, start_rank: int = 1) -> str:
    """
    Builds a structured text profile of retrieved complaints.
    Includes temporal breakdown, SVA metrics summary, short description frequency table (up to 200),
    and dialogue transcriptions for the current batch of complaints.
    """
    if df.empty:
        return "Таблица обращений пуста."

    total_rows = len(df)
    lines = [f"Профиль данных обращений (Всего в базе найдено: {total_rows} обращений):"]

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

    # 2. SVA metrics breakdown if available
    if "Метрика СВА" in df.columns:
        metrics_s = df["Метрика СВА"].dropna()
        if not metrics_s.empty:
            m_cnt = metrics_s.value_counts()
            lines.append("\nРаспределение по метрикам СВА:")
            for m_code, cnt in m_cnt.items():
                pct = (cnt / total_rows) * 100
                lines.append(f"- Метрика '{m_code}': {cnt} обращений ({pct:.1f}%)")

    # 3. Short descriptions frequency table (up to 200 unique)
    lines.append("\n" + extract_short_descriptions_summary(df, max_unique=200))

    # 4. Transcriptions for the specified batch
    target_batch = df_batch if df_batch is not None else df.head(5)
    if not target_batch.empty:
        lines.append(f"\nДетальная транскрибация обращений по релевантности (номера {start_rank}-{start_rank + len(target_batch) - 1}):")
        for idx, (_, row) in enumerate(target_batch.iterrows(), start=start_rank):
            cid = row.get("id", "N/A")
            desc = row.get("Короткое описание", "—")
            score = row.get("score", 0.0)
            date_str = row.get("date", "—")
            sva_m = row.get("Метрика СВА", "—")
            dialogue = row.get("Транскрибация диалога", "—")

            if isinstance(dialogue, str) and len(dialogue) > 1500:
                dialogue_snippet = dialogue[:1500] + "..."
            else:
                dialogue_snippet = str(dialogue)

            lines.append(f"\n---")
            lines.append(f"Обращение #{idx} (ID: {cid}) | Дата: {date_str} | Скор релевантности: {score:.4f} | Метрика СВА: {sva_m}")
            lines.append(f"Короткое описание: {desc}")
            lines.append(f"Транскрибация диалога: {dialogue_snippet}")

    return "\n".join(lines)


async def generate_complaint_hypothesis_narrative(user_msg: str, df: pd.DataFrame, file_info: dict) -> str:
    """
    Generates a natural language narrative (analytical report with hypotheses)
    about customer complaints using the GigaChat API.
    
    Processing Rules:
    - Short descriptions: Frequency table of up to 200 unique short descriptions.
    - Transcriptions: Exactly top 10 complaints by relevance, split into 2 batches of 5 (1-5 and 6-10).
    - Strict Grounding: System instructions strictly enforce no hallucinations, simple logical conclusions,
      and exact reliance on dates, short descriptions, and dialogue transcripts.
    """
    if df.empty:
        return "По вашему запросу не найдено подходящих обращений в базе данных."

    from backend.config import get_settings
    settings = get_settings()
    delay = settings.gigachat_delay_sec or 7.0

    system_grounding = """Ты — ведущий эксперт-аналитик Службы контроля качества и клиентского опыта Сбербанка.
Твоя задача — провести глубокий и объективный анализ обращений клиентов и сформулировать обоснованные аналитические гипотезы о корневых причинах (root causes) возникших проблем.

КРИТИЧЕСКИЕ ПРАВИЛА И ЗАЗЕМЛЕНИЕ (GROUNDING):
1. Опирайся СТРОГО на предоставленные данные: даты, частотную статистику коротких описаний (частоту повторения тем) и тексты транскрибаций диалогов.
2. НЕ придумывай вымышленных систем, несуществующих ошибок, внешних факторов или странных фактов, о которых нет сведений в предоставленном контексте.
3. Делай максимально простые, адекватные и логичные выводы напрямую из переданной информации.
4. Учитывай хронологию и даты: если проблемы концентрируются в конкретные месяцы/дни, обязательно отраззи это в отчете.
5. Связывай короткие описания (частые темы) с деталями из транскрибаций диалогов.

Структура отчета:
### 1. Выявленные проблемы и паттерны
- Укажи топ-группы проблем на основе частоты коротких описаний и текстов диалогов.
- Отметь временную динамику (в какие периоды всплеск обращений).

### 2. Анализ корневых причин (Root Cause Analysis)
- На основе деталей из транскрибаций предложи простые и реалистичные объяснения корневых причин (например, технический сбой в мобильном приложении, задержка проведения транзакции, неполная информация у оператора).

### 3. Рекомендации и гипотезы для проверки
- Сформулируй 2-3 гипотезы с конкретными действиями для проверки (например: "Гипотеза о сбое PUSH-уведомлений в январе: клиенты не получают смс-коды. Рекомендация: Проверить логи шлюза информирования за указанные даты.").

Пиши на русском языке, в профессиональном, аналитическом стиле.
"""

    # Top 10 complaints by relevance for transcription analysis
    df_top10 = df.head(10)

    # If df has <= 5 complaints total, process in a single batch
    if len(df_top10) <= 5:
        profile_text = profile_complaints_dataframe(df, df_batch=df_top10, start_rank=1)
        user_prompt = f"""Запрос пользователя: "{user_msg}"
Файл выгрузки: "{file_info.get('name', 'отчет.xlsx')}" (всего обращений в базе: {len(df)})

{profile_text}

Сформулируй гипотезу на основе этих данных. Пиши на русском языке, в профессиональном стиле."""
        
        messages = [
            {"role": "system", "content": system_grounding},
            {"role": "user", "content": user_prompt}
        ]
        try:
            response = await asyncio.to_thread(def_ask_gigachat, messages)
            return str(response)
        except Exception as e:
            logger.exception(f"Ошибка генерации гипотезы через GigaChat API: {e}")
            return f"Не удалось сгенерировать гипотезу из-за ошибки: {str(e)}"

    # If df has > 5 complaints, process top 10 transcriptions in 2 batches of 5
    df_batch1 = df_top10.iloc[0:5]
    df_batch2 = df_top10.iloc[5:10]

    # Batch 1 analysis (transcriptions 1-5)
    profile1 = profile_complaints_dataframe(df, df_batch=df_batch1, start_rank=1)
    user_prompt1 = f"""Запрос пользователя: "{user_msg}"
Группа обращений (топ 1-5 по релевантности) и общая статистика:
{profile1}

Составь промежуточный аналитический отчет по этой группе обращений."""

    logger.info("[COMPLAINT NARRATIVE] Starting Batch 1 analysis (top 1-5 transcriptions)...")
    try:
        res1 = await asyncio.to_thread(def_ask_gigachat, [
            {"role": "system", "content": system_grounding},
            {"role": "user", "content": user_prompt1}
        ])
    except Exception as e:
        logger.error(f"Error in batch 1 analysis: {e}")
        res1 = f"Ошибка анализа группы 1: {e}"

    # Pause to prevent rate limits
    logger.info(f"[COMPLAINT NARRATIVE] Pausing for {delay} seconds...")
    await asyncio.sleep(delay)

    # Batch 2 analysis (transcriptions 6-10 if available)
    if not df_batch2.empty:
        profile2 = profile_complaints_dataframe(df, df_batch=df_batch2, start_rank=6)
        user_prompt2 = f"""Запрос пользователя: "{user_msg}"
Группа обращений (топ 6-10 по релевантности) и общая статистика:
{profile2}

Составь промежуточный аналитический отчет по этой группе обращений."""

        logger.info(f"[COMPLAINT NARRATIVE] Starting Batch 2 analysis ({len(df_batch2)} transcriptions)...")
        try:
            res2 = await asyncio.to_thread(def_ask_gigachat, [
                {"role": "system", "content": system_grounding},
                {"role": "user", "content": user_prompt2}
            ])
        except Exception as e:
            logger.error(f"Error in batch 2 analysis: {e}")
            res2 = f"Ошибка анализа группы 2: {e}"

        logger.info(f"[COMPLAINT NARRATIVE] Pausing for {delay} seconds before merge...")
        await asyncio.sleep(delay)
    else:
        res2 = "Обращений для группы 2 нет."

    # Merge step
    user_prompt_combine = f"""Запрос пользователя: "{user_msg}"
Файл выгрузки: "{file_info.get('name', 'отчет.xlsx')}" (всего обращений в выгрузке: {len(df)})

Общая частотная статистика коротких описаний (топ 200 тем):
{extract_short_descriptions_summary(df, max_unique=200)}

Промежуточный аналитический отчет 1 (обращения по релевантности 1-5):
{res1}

Промежуточный аналитический отчет 2 (обращения по релевантности 6-10):
{res2}

Сформируй единый финальный объединенный аналитический отчет и гипотезы на основе этих данных.
Соблюдай критические правила: не придумывай фактов, опирайся строго на данные и выводи простые, логичные выводы."""

    logger.info("[COMPLAINT NARRATIVE] Merging batch reports into final hypothesis...")
    try:
        final_response = await asyncio.to_thread(def_ask_gigachat, [
            {"role": "system", "content": system_grounding},
            {"role": "user", "content": user_prompt_combine}
        ])
        return str(final_response)
    except Exception as e:
        logger.exception(f"Ошибка при объединении отчетов: {e}")
        return f"Не удалось объединить промежуточные отчеты из-за ошибки: {e}"


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
        desc = str(item.get("desc") or "").lower()
        dialogue = str(item.get("dialogue") or "").lower()
        
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


def answer_complaint_details(user_query: str, complaints: list, history: list = None, hypothesis: str = None) -> str:
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
    if hypothesis:
        system_prompt += f"\nДля контекста, ранее на основе всей выгрузки была сформирована следующая аналитическая гипотеза:\n{hypothesis}\n"

    user_prompt = f"""Вопрос пользователя: "{user_query}"

Предоставленные обращения:
{formatted_texts}

Ответь на вопрос пользователя на основе предоставленных данных."""

    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_prompt})

    return def_ask_gigachat(messages)


def answer_complaint_follow_up(user_query: str, complaints: list, history: list = None, hypothesis: str = None) -> str:
    """
    Answers general follow-up questions about the entire retrieved set of complaints.
    """
    formatted_texts = ""
    for idx, item in enumerate(complaints[:15]):  # limit to top 15 to prevent context limit issues
        cid = item.get("id")
        desc = item.get("desc", "")
        dialogue = item.get("dialogue", "")
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
    if hypothesis:
        system_prompt += f"\nДля контекста, ранее на основе всей выгрузки была сформирована следующая аналитическая гипотеза:\n{hypothesis}\n"

    user_prompt = f"""Вопрос пользователя: "{user_query}"

Список обращений для анализа:
{formatted_texts}

Ответь на вопрос пользователя."""

    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_prompt})

    return def_ask_gigachat(messages)


def answer_complaint_dialog(user_query: str, history: list, hypothesis: str = None) -> str:
    """
    Generates a dialog response using GigaChat, preserving context of the chat session.
    """
    system_prompt = """Ты — ведущий эксперт-аналитик Службы контроля качества и клиентского опыта Сбербанка.
Ты ведешь диалог с пользователем. Твои ответы должны основываться исключительно на истории сообщений.
Если тебя просят пояснить понятие, термин или предыдущие выводы, дай развернутое пояснение в профессиональном стиле.
"""
    if hypothesis:
        system_prompt += f"\nДля контекста, ранее на основе всей выгрузки была сформирована следующая аналитическая гипотеза:\n{hypothesis}\n"

    messages = [{"role": "system", "content": system_prompt}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_query})

    return def_ask_gigachat(messages)
