import os
import re
import uuid
import logging
import asyncio
from pathlib import Path
from typing import Optional
import pandas as pd
import numpy as np

# Set matplotlib backend to non-interactive
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from backend.core.llm import get_llm
from backend.config import get_settings
from backend.storage.database import FileRepo, get_db

logger = logging.getLogger(__name__)


def _to_numeric_clean(series: pd.Series) -> pd.Series:
    if isinstance(series, pd.DataFrame):
        series = series.iloc[:, 0]
    if series.empty:
        return series
    if pd.api.types.is_numeric_dtype(series):
        return series.fillna(0)
    # clean formatting from string columns (e.g. spaces, commas, currency symbols)
    s = series.astype(str).str.replace(r'\s+', '', regex=True)
    s = s.str.replace(',', '.', regex=False)
    s = s.str.replace(r'[^\d\.\-]', '', regex=True)
    return pd.to_numeric(s, errors='coerce').fillna(0)


def _df_to_markdown_clean(df: pd.DataFrame) -> str:
    """Renders a pandas DataFrame as a clean Markdown table without tabulate dependency."""
    if df.empty:
        return ""
    headers = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(headers) + " |"]
    lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
    for _, row in df.iterrows():
        row_str = []
        for v in row:
            if v is None or pd.isna(v):
                row_str.append("—")
            elif isinstance(v, float):
                row_str.append(f"{v:.2f}")
            else:
                row_str.append(str(v).replace("\n", " ").replace("|", "\\|"))
        lines.append("| " + " | ".join(row_str) + " |")
    return "\n".join(lines)


def format_loss(val: float) -> str:
    return f"{val:,.2f} ₽".replace(",", " ")


def _to_datetime_safe(s: pd.Series) -> pd.Series:
    from datetime import datetime
    import pandas as pd
    
    def parse_val(val):
        if pd.isna(val) or val is None:
            return pd.NaT
        if isinstance(val, (datetime, pd.Timestamp)):
            return pd.Timestamp(val)
        val_str = str(val).strip()
        if not val_str or val_str.lower() in ("nan", "nat", "none", "—", "-"):
            return pd.NaT
            
        for fmt in (None, "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%d", "%d.%m.%Y"):
            try:
                if fmt is None:
                    return pd.Timestamp(val_str)
                else:
                    return pd.Timestamp(datetime.strptime(val_str, fmt))
            except Exception:
                continue
        try:
            from dateutil import parser
            return pd.Timestamp(parser.parse(val_str))
        except Exception:
            return pd.NaT

    if hasattr(s, 'apply'):
        return s.apply(parse_val)
    return pd.Series([parse_val(x) for x in s])


def get_total_and_direct_loss(df: pd.DataFrame) -> tuple[float, float]:
    total_loss = 0.0
    direct_loss = 0.0
    
    col_map = {str(c).lower().strip().replace("–", "-"): c for c in df.columns}
    
    # 1. Total loss column candidates
    total_cols = [
        "incdnt_sum", 
        "общая сумма всех последствий (руб.)", 
        "общая сумма последствий (руб.)", 
        "сумма последствий, ₽",
        "fin_impact_rub_amt",
        "сумма последствия (руб.)",
        "сумма последствия",
        "сумма в рублях"
    ]
    for c_cand in total_cols:
        norm_cand = c_cand.lower().strip().replace("–", "-")
        if norm_cand in col_map:
            total_loss = _to_numeric_clean(df[col_map[norm_cand]]).sum()
            break
    else:
        money_cols = [c for c in df.columns if any(x in str(c).lower() for x in ("sum", "loss", "dmg", "rub", "amt", "потер", "убыт", "возмещ", "сумм"))]
        loss_cols = [c for c in money_cols if any(x in str(c).lower() for x in ("loss", "dmg", "потер", "убыт", "sum", "сумм")) and not any(r in str(c).lower() for r in ("rec", "возмещ", "возврат"))]
        if loss_cols:
            total_loss = _to_numeric_clean(df[loss_cols[0]]).sum()

    # 2. Direct loss column candidates
    direct_cols = [
        "incdnt_drct_dmg_sum", 
        "прямая потеря - итого (руб.)", 
        "direct_loss", 
        "прямая потеря"
    ]
    for c_cand in direct_cols:
        norm_cand = c_cand.lower().strip().replace("–", "-")
        if norm_cand in col_map:
            direct_loss = _to_numeric_clean(df[col_map[norm_cand]]).sum()
            break
    else:
        type_col = next((c for c in df.columns if str(c).lower() == "fin_impact_type_name"), None)
        amt_col = next((c for c in df.columns if str(c).lower() == "fin_impact_rub_amt"), None)
        if type_col and amt_col:
            direct_loss = _to_numeric_clean(df[df[type_col] == "Прямая потеря"][amt_col]).sum()
            
    return float(total_loss), float(direct_loss)


def format_loss(val: float) -> str:
    return f"{val:,.2f} ₽".replace(",", " ")


def get_recovery_column(df: pd.DataFrame, running_skill: str = None) -> Optional[str]:
    if running_skill and "financial_consequences_ior" in running_skill:
        return None

    col_map = {str(c).lower().strip().replace("–", "-"): c for c in df.columns}
    rec_cols_list = [
        "recovery", 
        "сумма возмещений", 
        "сумма возмещения", 
        "сумма возмещения (руб.)", 
        "сумма возмещений (руб.)", 
        "возмещ", 
        "recovery_rub_amt", 
        "recovery_rub_amt_aggr", 
        "сумма возмещения (агрегатор)", 
        "возмещение - итого по инциденту (руб.)"
    ]
    for c_cand in rec_cols_list:
        norm_cand = c_cand.lower().strip().replace("–", "-")
        if norm_cand in col_map:
            return col_map[norm_cand]
            
    # Fallback to general recovery/возмещ/возврат keywords
    money_cols = [c for c in df.columns if any(x in str(c).lower() for x in ("sum", "loss", "dmg", "rub", "amt", "потер", "убыт", "возмещ", "сумм"))]
    rec_cols_fallback = [c for c in money_cols if any(x in str(c).lower() for x in ("rec", "возмещ", "возврат"))]
    if rec_cols_fallback:
        return rec_cols_fallback[0]
        
    return None


def collapse_cyclical_repetitions(text: str) -> str:
    import re
    lines = text.split('\n')
    n = len(lines)
    if n < 2:
        return text

    collapsed = []
    i = 0
    while i < n:
        found_cycle = False
        for k in range(1, 13):
            if i + 2 * k > n:
                continue
            
            block = [re.sub(r'\s+', ' ', lines[i + j]).strip().lower() for j in range(k)]
            if not any(block):
                continue
                
            reps = 1
            while i + (reps + 1) * k <= n:
                next_block = [re.sub(r'\s+', ' ', lines[i + reps * k + j]).strip().lower() for j in range(k)]
                if next_block == block:
                    reps += 1
                else:
                    break
            
            if reps > 1:
                for j in range(k):
                    collapsed.append(lines[i + j])
                logger.warning(f"Collapsed cyclical repetition: block of size {k} repeated {reps} times.")
                i += reps * k
                found_cycle = True
                break
        
        if not found_cycle:
            collapsed.append(lines[i])
            i += 1
            
    return '\n'.join(collapsed)


def normalize_markdown_for_frontend(text: str) -> str:
    import re
    lines = text.split('\n')
    normalized_lines = []
    for line in lines:
        stripped = line.strip()
        # Match ### or #### headers
        match = re.match(r'^(#{1,5})\s*(.+)$', stripped)
        if match:
            content = match.group(2).strip()
            # If it already ends/starts with **, keep it, else wrap in **
            if content.startswith("**") and content.endswith("**"):
                normalized_lines.append(content)
            else:
                # Remove trailing colons/periods from bold headers for cleaner look
                normalized_lines.append(f"**{content}**")
        elif stripped == "---":
            # Remove single markdown divider lines completely
            continue
        else:
            normalized_lines.append(line)
            
    res = '\n'.join(normalized_lines)
    return res


def collapse_repeated_sentences(text: str) -> tuple[str, int]:
    import re
    # First split by line to preserve layout, but also check for identical consecutive non-empty lines
    lines = text.split('\n')
    collapsed_lines = []
    total_reps = 0
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        norm_line = re.sub(r'\s+', ' ', line).strip().lower()
        if not norm_line:
            collapsed_lines.append(line)
            i += 1
            continue
        
        j = i + 1
        while j < n:
            next_line = lines[j]
            norm_next = re.sub(r'\s+', ' ', next_line).strip().lower()
            if norm_next == norm_line:
                j += 1
            else:
                break
        
        dup_count = j - i
        if dup_count > 1:
            total_reps += (dup_count - 1)
            logger.warning(f"Detected repetition loop of length {dup_count} for line: '{line}'")
        collapsed_lines.append(line)
        i = j

    # Now inside each collapsed line, check for consecutive sentence repetitions
    final_lines = []
    for line in collapsed_lines:
        if not line.strip():
            final_lines.append(line)
            continue
        sentences = re.split(r'(?<=[.!?])\s+', line)
        collapsed_sentences = []
        i = 0
        n = len(sentences)
        while i < n:
            s = sentences[i]
            norm_s = re.sub(r'\s+', ' ', s).strip().lower()
            if not norm_s:
                collapsed_sentences.append(s)
                i += 1
                continue
            
            j = i + 1
            while j < n:
                next_s = sentences[j]
                norm_next = re.sub(r'\s+', ' ', next_s).strip().lower()
                if norm_next == norm_s:
                    j += 1
                else:
                    break
            
            dup_count = j - i
            if dup_count > 1:
                total_reps += (dup_count - 1)
                logger.warning(f"Detected repetition loop of length {dup_count} for sentence: '{s}'")
            collapsed_sentences.append(s)
            i = j
        final_lines.append(" ".join(collapsed_sentences))
        
    return "\n".join(final_lines), total_reps


def collapse_repeated_sections(text: str) -> str:
    import re
    lines = text.split('\n')
    sections = []
    current_section = {'header': '', 'norm_header': '', 'lines': []}
    sections.append(current_section)
    
    header_pattern = re.compile(r'^(?:#+\s*|\d+\.\s*)(.+)$')
    
    for line in lines:
        match = header_pattern.match(line.strip())
        if match:
            header_text = match.group(1).strip()
            # Normalize header text: strip non-alphanumeric/spaces, lowercase
            norm_header = re.sub(r'[^\w\s]', '', header_text).strip().lower()
            norm_header = re.sub(r'^\d+\s*', '', norm_header).strip()
            
            current_section = {'header': line, 'norm_header': norm_header, 'lines': []}
            sections.append(current_section)
        else:
            current_section['lines'].append(line)
            
    seen_headers = set()
    unique_sections = []
    
    for sec in sections:
        norm = sec['norm_header']
        if not norm:
            unique_sections.append(sec)
            continue
            
        if norm in seen_headers:
            logger.warning(f"Detected duplicate section: '{sec['header']}'")
            continue
            
        seen_headers.add(norm)
        unique_sections.append(sec)
        
    result_lines = []
    for sec in unique_sections:
        if sec['header']:
            result_lines.append(sec['header'])
        result_lines.extend(sec['lines'])
        
    return "\n".join(result_lines)


def trim_extra_sections(narrative: str, is_summarization: bool) -> str:
    import re
    # Find where the last section starts.
    last_sec_markers = ["### 3.", "3. Концентрация", "3. Выявленные особенности"] if is_summarization else ["### 4.", "4. Аналитические гипотезы", "Аналитические гипотезы"]
    
    last_sec_pos = -1
    for marker in last_sec_markers:
        pos = narrative.find(marker)
        if pos != -1:
            last_sec_pos = pos
            break
            
    if last_sec_pos == -1:
        return narrative
        
    marker_line_end = narrative.find('\n', last_sec_pos)
    if marker_line_end == -1:
        return narrative
        
    scan_start = marker_line_end
    text_after = narrative[scan_start:]
    
    lines = text_after.split('\n')
    cut_idx = -1
    
    plain_header_pattern = re.compile(r'^[А-ЯA-Z\d][^\n]{1,100}$')
    list_item_pattern = re.compile(r'^\s*[•\-\*\d+\.]\s')
    
    blacklist_headers = ["следующие шаги", "финальный вывод", "выводы", "рекомендации", "дополнительно", "заключение", "итоги", "резюме", "вывод"]
    
    for idx, line in enumerate(lines):
        striped_line = line.strip()
        if not striped_line:
            continue
            
        # Check if it is a markdown heading (but not #### if it's subheadings of hypothesis)
        if striped_line.startswith('#') and not striped_line.startswith('####'):
            cut_idx = idx
            break
            
        # Check blacklist headings (case-insensitive, strip punctuation)
        norm_line = re.sub(r'[^\w\s]', '', striped_line).strip().lower()
        if norm_line in blacklist_headers:
            cut_idx = idx
            break
            
        # Check plain text heading followed by bullet points
        if plain_header_pattern.match(striped_line):
            next_idx = idx + 1
            while next_idx < len(lines) and not lines[next_idx].strip():
                next_idx += 1
            if next_idx < len(lines):
                next_line = lines[next_idx].strip()
                if list_item_pattern.match(next_line):
                    cut_idx = idx
                    break
                    
    if cut_idx != -1:
        trimmed_after = "\n".join(lines[:cut_idx])
        return narrative[:scan_start] + trimmed_after
        
    return narrative


async def validate_narrative(narrative: str, forbidden_fields: list[str] = None) -> dict:
    import json
    import re
    
    # Base system prompt with common rules
    system_prompt = (
        "Ты — контролёр качества аналитических отчётов. Проверь предоставленный текст на следующие нарушения правил и верни ТОЛЬКО JSON без пояснений:\n"
        "{\n"
        "  \"autoreg_criticized\": bool, \n"
        "  \"hypotheses_duplicate\": bool, \n"
        "  \"extra_sections\": bool, \n"
        "  \"missing_eve_ids_in_major_incidents\": bool, \n"
        "  \"fabricated_thresholds\": bool, \n"
        "  \"numbers_inconsistent\": bool, \n"
        "  \"unfounded_inference_from_null_data\": bool, \n"
        "  \"fields_not_in_dataset\": bool, \n"
        "  \"details\": \"краткое описание найденных проблем на русском\"\n"
        "}\n\n"
        "КРИТЕРИИ НАРУШЕНИЙ:\n"
        "1. autoreg_criticized: критикуется ли авторегистрация (авторег) как негативный фактор, или утверждается, что высокая доля авторегистрации — это проблема/уязвимость.\n"
        "2. hypotheses_duplicate: дублируют ли гипотезы друг друга по смыслу или сводятся ли они к одной причине (например, все гипотезы утверждают, что 'виноват персонал').\n"
        "3. extra_sections: содержит ли отчет разделы, выходящие за рамки разрешенной структуры (например, разделы 'Вывод', 'Заключение', 'Финальный вывод', 'Следующие шаги', 'Рекомендации').\n"
        "4. missing_eve_ids_in_major_incidents: отсутствуют ли конкретные ID инцидентов (EVE-XXXXXXX) при описании крупных инцидентов или концентрации потерь.\n"
        "5. fabricated_thresholds: присутствуют ли в шагах проверки гипотез надуманные/вымышленные числовые пороги подтверждения (например, '>30%', '>50%'), не подтвержденные данными.\n"
        "6. numbers_inconsistent: противоречат ли друг другу числовые показатели в разных частях отчета (например, разное количество инцидентов или разные суммы для одного среза данных).\n"
        "7. unfounded_inference_from_null_data: делаются ли необоснованные причинно-следственные выводы из нулевых или вырожденных значений метрик (например, 'нулевые потери означают урегулированность всех ошибок')."
    )

    if forbidden_fields:
        fields_str = ", ".join(f"'{f}'" for f in forbidden_fields)
        system_prompt += (
            f"\n8. fields_not_in_dataset: присутствуют ли в отчете числовые значения или явные упоминания сумм/процентов/метрик "
            f"по полям {fields_str}, которых заведомо НЕТ в данном типе выгрузки (например, возмещения в выгрузке финансовых последствий)."
        )
    else:
        system_prompt += "\n8. fields_not_in_dataset: false (всегда false, так как список запрещенных полей пуст)."
    
    user_message = f"Проверь следующий отчет:\n\n{narrative}"
    
    from local_qwen import ask_local_qwen
    try:
        response_text = await asyncio.to_thread(
            ask_local_qwen, [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message}
            ],
            max_tokens=768
        )
        
        text = str(response_text).strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()
            
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass
    except Exception as e:
        logger.error(f"Error in validate_narrative: {e}")
        
    return {
        "autoreg_criticized": False,
        "hypotheses_duplicate": False,
        "extra_sections": False,
        "missing_eve_ids_in_major_incidents": False,
        "fabricated_thresholds": False,
        "numbers_inconsistent": False,
        "unfounded_inference_from_null_data": False,
        "fields_not_in_dataset": False,
        "details": "Ошибка парсинга ответа судьи"
    }


def calculate_advanced_stats(df: pd.DataFrame) -> dict:
    """
    Computes advanced operational risk metrics: Top-10 concentration (exact sum and percentage).
    """
    stats = {}
    total_rows = len(df)
    
    money_cols = [c for c in df.columns if any(x in str(c).lower() for x in ("sum", "loss", "dmg", "rub", "amt", "потер", "убыт", "возмещ", "сумм"))]
    loss_cols = [c for c in money_cols if any(x in str(c).lower() for x in ("loss", "dmg", "потер", "убыт", "sum", "сумм")) and not any(r in str(c).lower() for r in ("rec", "возмещ", "возврат"))]
    if not loss_cols and money_cols:
        loss_cols = [c for c in money_cols if not any(x in str(c).lower() for x in ("rec", "возмещ", "возврат"))]
    
    if loss_cols:
        primary_loss = loss_cols[0]
        try:
            losses = _to_numeric_clean(df[primary_loss])
            total_loss = losses.sum()
            
            if total_loss > 0:
                sorted_losses = losses.sort_values(ascending=False)
                top_10_losses = sorted_losses.head(10)
                top_10_sum = top_10_losses.sum()
                top_10_pct = (top_10_sum / total_loss) * 100
                
                stats["top_10_sum"] = top_10_sum
                stats["top_10_pct"] = top_10_pct
                stats["total_loss"] = total_loss
        except Exception as e:
            logger.warning(f"Error calculating advanced stats: {e}")
            
    return stats


def generate_dynamics_chart(df: pd.DataFrame, session_id: str) -> Optional[str]:
    """
    Plots a professional chart representing temporal dynamics (multiple synergistic lines) and saves it.
    Returns the file_id of the registered image.
    """
    try:
        # Prioritize entry date (Дата ввода) to match user requested period and prevent plotting historical start dates (from 2016)
        entry_candidates = [c for c in df.columns if any(x in str(c).lower() for x in ("entry", "ввод", "регистр"))
                            and not any(y in str(c).lower() for y in ("признак", "тип", "флаг", "flag", "type", "номер", "id", "status", "статус"))]
        date_cols = [c for c in df.columns if any(x in str(c).lower() for x in ("dt", "date", "dttm", "ts", "дата", "время"))
                     and not any(y in str(c).lower() for y in ("признак", "тип", "флаг", "flag", "type", "номер", "id", "status", "статус"))]
        
        primary_date = None
        if entry_candidates:
            primary_date = entry_candidates[0]
        elif date_cols:
            primary_date = date_cols[0]
            
        if not primary_date:
            logger.info("No date column found, skipping chart generation.")
            return None
            
        # Try exact matching first for translated columns
        loss_candidates = ["общая сумма всех последствий (руб.)", "общая сумма последствий (руб.)", "сумма последствий, ₽", "сумма последствий", "incdnt_sum", "incdnt_drct_dmg_sum", "сумма последствия (руб.)", "сумма последствия", "fin_impact_rub_amt"]
        rec_candidates = ["возмещение – итого по инциденту (руб.)", "сумма возмещений (руб.)", "сумма возмещений", "возмещ", "recovery", "recovery_rub_amt_aggr", "recovery_rub_amt", "сумма возмещения (руб.)"]
        
        primary_loss = next((c for c in df.columns if str(c).lower().strip() in loss_candidates), None)
        primary_recovery = next((c for c in df.columns if str(c).lower().strip() in rec_candidates), None)
        
        if not primary_loss:
            money_cols = [c for c in df.columns if any(x in str(c).lower() for x in ("sum", "loss", "dmg", "rub", "amt", "потер", "убыт", "возмещ", "сумм", "потери"))]
            loss_cols = [c for c in money_cols if any(x in str(c).lower() for x in ("loss", "dmg", "потер", "убыт", "sum", "потери")) and not any(r in str(c).lower() for r in ("rec", "возмещ", "возврат", "возмещений"))]
            if loss_cols:
                primary_loss = loss_cols[0]
            elif money_cols:
                primary_loss = [c for c in money_cols if not any(r in str(c).lower() for r in ("rec", "возмещ", "возврат", "возмещений"))][0]
                
        if not primary_recovery:
            money_cols = [c for c in df.columns if any(x in str(c).lower() for x in ("sum", "loss", "dmg", "rub", "amt", "потер", "убыт", "возмещ", "сумм", "потери"))]
            recovery_cols = [c for c in money_cols if any(x in str(c).lower() for x in ("rec", "возмещ", "возврат", "возмещений"))]
            if recovery_cols:
                primary_recovery = recovery_cols[0]
        
        # Prepare data
        temp_df = df.copy()
        temp_df[primary_date] = _to_datetime_safe(temp_df[primary_date])
        temp_df = temp_df.dropna(subset=[primary_date])
        
        if temp_df.empty:
            logger.info("Date column has only null values, skipping chart generation.")
            return None
            
        # Determine temporal grouping level based on dataset span
        min_date = temp_df[primary_date].min()
        max_date = temp_df[primary_date].max()
        days_diff = (max_date - min_date).days if pd.notna(min_date) and pd.notna(max_date) else 0
        
        if days_diff <= 31:
            temp_df['period_key'] = temp_df[primary_date].dt.strftime('%d.%m')
            period_label = 'День'
        elif days_diff <= 180:
            temp_df['period_key'] = temp_df[primary_date].dt.to_period('W').astype(str).apply(lambda x: x.split('/')[0])
            period_label = 'Неделя'
        else:
            temp_df['period_key'] = temp_df[primary_date].dt.to_period('M').astype(str)
            period_label = 'Месяц'

        # Group data
        grouped = temp_df.groupby('period_key').agg(
            count=(primary_date, 'count')
        ).reset_index()
        
        if primary_loss:
            temp_df[primary_loss] = pd.to_numeric(temp_df[primary_loss], errors='coerce').fillna(0)
            loss_g = temp_df.groupby('period_key')[primary_loss].sum().reset_index(name='loss_sum')
            grouped = grouped.merge(loss_g, on='period_key', how='left')
        else:
            grouped['loss_sum'] = 0.0
            
        if primary_recovery:
            temp_df[primary_recovery] = pd.to_numeric(temp_df[primary_recovery], errors='coerce').fillna(0)
            rec_g = temp_df.groupby('period_key')[primary_recovery].sum().reset_index(name='recovery_sum')
            grouped = grouped.merge(rec_g, on='period_key', how='left')
        else:
            grouped['recovery_sum'] = 0.0
            
        grouped['loss_sum'] = grouped['loss_sum'].fillna(0.0)
        grouped['recovery_sum'] = grouped['recovery_sum'].fillna(0.0)
        
        # Sort chronologically based on original sequence
        chronological_keys = temp_df.sort_values(primary_date)['period_key'].unique()
        grouped['period_key'] = pd.Categorical(grouped['period_key'], categories=chronological_keys, ordered=True)
        grouped = grouped.sort_values('period_key')
        
        # Setup modern clean light style matching light background requirements
        plt.style.use('default')
        fig, ax1 = plt.subplots(figsize=(9, 4.5), dpi=150)
        fig.patch.set_facecolor('#ffffff')  # Clean white background
        ax1.set_facecolor('#ffffff')
        
        # Grid lines (light slate)
        ax1.grid(True, axis='both', color='#e2e8f0', linestyle=':', alpha=0.8)
        
        periods = grouped['period_key'].astype(str).tolist()
        counts = grouped['count'].tolist()
        
        # Bar 1: incident counts (primary y-axis, clean sky blue color)
        bar_width = 0.35
        n_periods = len(periods)
        x_positions = list(range(n_periods))
        ax1.bar(x_positions, counts, width=bar_width, color='#3b82f6', edgecolor='#2563eb', alpha=0.85, label='Число инцидентов')
        ax1.set_ylabel('Число инцидентов', color='#1e3a8a', fontsize=10)
        ax1.tick_params(axis='y', labelcolor='#1e3a8a', colors='#0f172a')
        
        # Handle custom tick label density to avoid date overlap
        if n_periods > 12:
            step = (n_periods // 12) + 1
            tick_positions = list(range(0, n_periods, step))
            tick_labels = [periods[i] for i in tick_positions]
        else:
            tick_positions = x_positions
            tick_labels = periods
            
        ax1.set_xticks(tick_positions)
        ax1.set_xticklabels(tick_labels, rotation=15, ha='right', fontsize=8, color='#0f172a')
        
        # Hide borders
        for spine in ax1.spines.values():
            spine.set_edgecolor('#cbd5e1')
            
        # Optional lines on secondary axis for losses/recoveries
        has_losses = primary_loss and grouped['loss_sum'].sum() > 0
        has_recoveries = primary_recovery and grouped['recovery_sum'].sum() > 0
        
        if has_losses or has_recoveries:
            ax2 = ax1.twinx()
            ax2.set_facecolor('none')  # Make background transparent
            
            # scaling denom
            max_val = max(grouped['loss_sum'].max(), grouped['recovery_sum'].max())
            if max_val >= 1_000_000_000:
                denom = 1_000_000_000
                denom_label = 'млрд ₽'
            elif max_val >= 1_000_000:
                denom = 1_000_000
                denom_label = 'млн ₽'
            else:
                denom = 1000
                denom_label = 'тыс. ₽'
                
            losses_scaled = (grouped['loss_sum'] / denom).tolist()
            recoveries_scaled = (grouped['recovery_sum'] / denom).tolist()
            
            ax2.spines['right'].set_color('#94a3b8')
            
            # Line 2: losses (rose color)
            if has_losses:
                ax2.plot(x_positions, losses_scaled, color='#dc2626', marker='s', markersize=4, linewidth=2, label=f'Сумма потерь ({denom_label})')
            
            # Line 3: recoveries (green color)
            if has_recoveries:
                ax2.plot(x_positions, recoveries_scaled, color='#15803d', marker='^', markersize=4, linewidth=1.8, linestyle='--', label=f'Сумма возмещений ({denom_label})')
                
            y2_label = []
            if has_losses:
                y2_label.append("потерь")
            if has_recoveries:
                y2_label.append("возмещений")
            label_text = f"Объем {' и '.join(y2_label)} ({denom_label})"
            
            ax2.set_ylabel(label_text, color='#dc2626' if has_losses else '#15803d', fontsize=10)
            ax2.tick_params(axis='y', labelcolor='#dc2626' if has_losses else '#15803d', colors='#0f172a')
            
            # Combine legends
            lines1, labels1 = ax1.get_legend_handles_labels()
            lines2, labels2 = ax2.get_legend_handles_labels()
            ax1.legend(lines1 + lines2, labels1 + labels2, loc='upper left', frameon=True, facecolor='#ffffff', edgecolor='#cbd5e1', labelcolor='#0f172a')
        else:
            ax1.legend(loc='upper left', frameon=True, facecolor='#ffffff', edgecolor='#cbd5e1', labelcolor='#0f172a')
            
        plt.title('Временная динамика инцидентов операционного риска и финансовых объемов', color='#0f172a', fontsize=11, pad=15, fontweight='bold')
        fig.tight_layout()
        
        # Save to disk
        settings = get_settings()
        files_dir = settings.files_path
        files_dir.mkdir(parents=True, exist_ok=True)
        
        file_uuid = str(uuid.uuid4())
        chart_name = f"chart_{file_uuid}.png"
        chart_path = files_dir / chart_name
        
        plt.savefig(str(chart_path), bbox_inches='tight', facecolor='#ffffff', edgecolor='none')
        plt.close(fig)
        
        # Register in database
        try:
            with get_db() as db:
                f = FileRepo.add(
                    db,
                    session_id=session_id,
                    file_path=str(chart_path),
                    file_name=chart_name,
                    size_bytes=chart_path.stat().st_size,
                    total_rows=len(grouped),
                    status="ready"
                )
                return f.id
        except Exception as e:
            logger.error(f"Error registering chart file: {e}")
            return None
    except Exception as e:
        logger.error(f"Error generating dynamics chart: {e}")
        return None


def generate_distribution_chart(df: pd.DataFrame, session_id: str) -> Optional[str]:
    """
    Generates a beautiful distribution chart (pie chart for small category counts, 
    horizontal bar chart for larger ones) and registers it.
    """
    try:
        # Find candidate categorical columns
        cat_candidates = []
        for col in df.columns:
            col_lower = str(col).lower()
            if any(x in col_lower for x in ("id", "sid", "key", "date", "dt", "dttm", "ts", "sum", "loss", "dmg", "rub", "amt", "потер", "убыт", "возмещ", "сумм")):
                continue
            if pd.api.types.is_numeric_dtype(df[col]):
                continue
            nunique = df[col].dropna().nunique()
            if 2 <= nunique <= 12:
                # Rank by relevance (prefer event type or TB)
                rank = 0
                if "type" in col_lower or "тип" in col_lower:
                    rank = 3
                elif "tb" in col_lower or "тб" in col_lower or "struct" in col_lower or "орг" in col_lower:
                    rank = 2
                elif "status" in col_lower or "статус" in col_lower:
                    rank = 1
                cat_candidates.append((col, nunique, rank))
                
        if not cat_candidates:
            return None
            
        # Select best column: sort by rank descending, then nunique ascending
        cat_candidates.sort(key=lambda x: (-x[2], x[1]))
        target_col, nunique, _ = cat_candidates[0]
        
        # Group data
        grouped = df.groupby(target_col).size().reset_index(name='count')
        grouped = grouped.sort_values(by='count', ascending=True) # Ascending for horizontal bar
        
        # Setup modern dark style matching the premium UI theme
        plt.style.use('dark_background')
        fig, ax = plt.subplots(figsize=(8, 4), dpi=150)
        fig.patch.set_facecolor('#0b0f19')
        ax.set_facecolor('#0b0f19')
        
        labels = grouped[target_col].astype(str).tolist()
        sizes = grouped['count'].tolist()
        
        # Human readable column label for title
        col_label_map = {
            "incdnt_type_lvl_1_name": "Типы событий ИОР",
            "incdnt_type_lvl_2_name": "Подтипы событий ИОР",
            "org_struct_lvl_3_name": "Территориальные банки (ТБ)",
            "incdnt_status_name": "Статусы инцидентов",
            "src_type_lvl_1_name": "Источники обнаружения",
            "incdnt_autoreg_flag": "Авторегистрация"
        }
        title_subject = col_label_map.get(target_col, f"Категория '{target_col}'")
        
        if nunique <= 5:
            # Generate a gorgeous Pie Chart
            # Premium dark palette colors
            colors = ['#10b981', '#3b82f6', '#f59e0b', '#ef4444', '#8b5cf6']
            wedges, texts, autotexts = ax.pie(
                sizes, 
                labels=labels, 
                autopct='%1.1f%%', 
                startangle=140, 
                colors=colors[:nunique],
                wedgeprops=dict(width=0.4, edgecolor='#1e293b', linewidth=1.5) # Donut chart style
            )
            # Style text labels
            for text in texts:
                text.set_color('#cbd5e1')
                text.set_fontsize(9)
            for autotext in autotexts:
                autotext.set_color('#ffffff')
                autotext.set_fontsize(8)
                autotext.set_weight('bold')
            ax.set_title(f"Распределение: {title_subject}", color='#f8fafc', fontsize=11, pad=15, fontweight='bold')
        else:
            # Generate a gorgeous Horizontal Bar Chart
            y_pos = range(len(labels))
            ax.barh(y_pos, sizes, color='#3b82f6', edgecolor='#60a5fa', alpha=0.85, height=0.5)
            ax.set_yticks(y_pos)
            ax.set_yticklabels(labels, color='#cbd5e1', fontsize=9)
            ax.grid(True, axis='x', color='#1e293b', linestyle='--', alpha=0.6)
            ax.spines['top'].set_visible(False)
            ax.spines['right'].set_visible(False)
            ax.spines['left'].set_color('#334155')
            ax.spines['bottom'].set_color('#334155')
            ax.tick_params(axis='x', colors='#94a3b8')
            ax.set_xlabel('Количество инцидентов', color='#94a3b8', fontsize=9, labelpad=8)
            ax.set_title(f"Топ-категории: {title_subject}", color='#f8fafc', fontsize=11, pad=15, fontweight='bold')
            
        fig.tight_layout()
        
        # Save to disk
        settings = get_settings()
        files_dir = settings.files_path
        files_dir.mkdir(parents=True, exist_ok=True)
        
        file_uuid = str(uuid.uuid4())
        chart_name = f"chart_dist_{file_uuid}.png"
        chart_path = files_dir / chart_name
        
        plt.savefig(str(chart_path), bbox_inches='tight', facecolor='#0b0f19')
        plt.close(fig)
        
        # Register in database
        try:
            with get_db() as db:
                f = FileRepo.add(
                    db,
                    session_id=session_id,
                    file_path=str(chart_path),
                    file_name=chart_name,
                    size_bytes=chart_path.stat().st_size,
                    total_rows=len(grouped),
                    status="ready"
                )
                return f.id
        except Exception as db_err:
            logger.error(f"Error registering distribution chart: {db_err}")
            return None
    except Exception as e:
        logger.error(f"Error generating distribution chart: {e}")
        return None


def profile_dataframe(df: pd.DataFrame, running_skill: str = None) -> str:
    """
    Generates a Markdown profile of the dataframe.
    """
    if df.empty:
        return "Таблица пуста."

    total_rows = len(df)
    lines = [f"### Профиль данных выгрузки (Всего строк для анализа: {total_rows}):\n"]

    df_copy = df.copy()
    is_nonfinancial = (running_skill == "ior_nonfinancial_consequences")
    reason_col = next((c for c in df_copy.columns if str(c).lower() in ("incdnt_type_lvl_1_name", "тип события – уровень 1", "тип события - уровень 1")), None)
    if reason_col:
        df_copy = df_copy.rename(columns={reason_col: "Основная причина"})

    # Clean technical block keys starting with SBR_ or being numeric from the functional block column if present
    fb_lvl2_col = next((c for c in df_copy.columns if str(c).lower() in ("funct_block_lvl_2_name", "функциональный блок – уровень 2", "функциональный блок - уровень 2")), None)
    if fb_lvl2_col:
        try:
            mask = df_copy[fb_lvl2_col].astype(str).str.startswith("SBR_") | df_copy[fb_lvl2_col].astype(str).str.isdigit()
            df_copy.loc[mask, fb_lvl2_col] = None
        except Exception:
            pass

    # 1. Money/Loss summaries
    money_cols = []
    incdnt_sum_col = None
    recovery_col = None
    total_loss = 0.0
    total_rec = 0.0

    if not is_nonfinancial:
        money_cols = [c for c in df_copy.columns if any(x in str(c).lower() for x in ("sum", "loss", "dmg", "rub", "amt", "потер", "убыт", "возмещ", "сумм"))]
        loss_cols = [c for c in money_cols if any(x in str(c).lower() for x in ("loss", "dmg", "потер", "убыт", "sum", "сумм")) and not any(r in str(c).lower() for r in ("rec", "возмещ", "возврат"))]
        if not loss_cols and money_cols:
            loss_cols = [c for c in money_cols if not any(x in str(c).lower() for x in ("rec", "возмещ", "возврат"))]
        rec_cols = [c for c in money_cols if "rec" in str(c).lower() or "возмещ" in str(c).lower() or "возврат" in str(c).lower()]

        # Locate specific key columns for side-by-side comparison
        incdnt_sum_col = None
        for col in df_copy.columns:
            col_lower = str(col).lower()
            if any(x in col_lower for x in ("incdnt_sum", "общая сумма", "сумма последствий")) and not any(x in col_lower for x in ("rec", "возмещ", "возврат")):
                incdnt_sum_col = col

        if incdnt_sum_col is None and loss_cols:
            for c in loss_cols:
                if "incdnt_sum" in str(c).lower() or "общая сумма" in str(c).lower():
                    incdnt_sum_col = c
                    break
            if incdnt_sum_col is None:
                incdnt_sum_col = loss_cols[0]

        recovery_col = get_recovery_column(df_copy, running_skill)

        total_loss = 0.0
        total_rec = 0.0

        if incdnt_sum_col is not None:
            try:
                total_loss = _to_numeric_clean(df_copy[incdnt_sum_col]).sum()
                lines.append(f"- **Общая сумма всех последствий (incdnt_sum)**: {format_loss(total_loss)} (по колонке '{incdnt_sum_col}')")
            except Exception as e:
                logger.warning(f"Error summing loss: {e}")

        if recovery_col is not None:
            try:
                total_rec = _to_numeric_clean(df_copy[recovery_col]).sum()
                lines.append(f"- **Сумма возмещений (recovery)**: {format_loss(total_rec)} (по колонке '{recovery_col}')")
            except Exception as e:
                logger.warning(f"Error summing recovery: {e}")

        if incdnt_sum_col is not None and recovery_col is not None:
            lines.append(f"- **Чистые потери (Net Loss)**: {format_loss(total_loss - total_rec)}")

        if total_loss == 0:
            lines.append("\n> [!NOTE]\n> Данные по потерям отсутствуют (равны нулю). В анализе и гипотезах сфокусируйся на других метриках (количестве инцидентов, динамике регистраций, распределении по категориям/ТБ, доле авторегистрации). Не зацикливайся на нулевых потерях.")

    # 2. Date/Temporal analysis
    entry_candidates = [c for c in df_copy.columns if any(x in str(c).lower() for x in ("entry", "ввод", "регистр"))
                        and not any(y in str(c).lower() for y in ("признак", "тип", "флаг", "flag", "type", "номер", "id", "status", "статус"))]
    date_cols = [c for c in df_copy.columns if any(x in str(c).lower() for x in ("dt", "date", "dttm", "ts", "дата", "время"))
                 and not any(y in str(c).lower() for y in ("признак", "тип", "флаг", "flag", "type", "номер", "id", "status", "статус"))]
    
    primary_date = None
    if entry_candidates:
        primary_date = entry_candidates[0]
    elif date_cols:
        primary_date = date_cols[0]

    if primary_date:
        try:
            temp_df = df_copy.dropna(subset=[primary_date]).copy()
            # Optimize parsing with cache=True for large datasets
            temp_df[primary_date] = _to_datetime_safe(temp_df[primary_date])
            temp_df = temp_df.dropna(subset=[primary_date])
            if not temp_df.empty:
                temp_df['month'] = temp_df[primary_date].dt.to_period('M')
                grp = temp_df.groupby('month')
                
                lines.append("\n#### Временное распределение:")
                lines.append("| Месяц | Число инцидентов | % от общего | Сумма потерь | % потерь |")
                lines.append("|---|---|---|---|---|")
                
                for month, group in sorted(grp, key=lambda x: x[0]):
                    m_count = len(group)
                    m_pct = (m_count / total_rows) * 100
                    m_loss_sum = 0
                    m_loss_pct_str = "—"
                    
                    if incdnt_sum_col is not None:
                        m_loss_sum = _to_numeric_clean(group[incdnt_sum_col]).sum()
                        if total_loss > 0:
                            m_loss_pct_str = f"{(m_loss_sum / total_loss) * 100:.1f}%"
                            
                    lines.append(f"| {month} | {m_count} | {m_pct:.1f}% | {format_loss(m_loss_sum)} | {m_loss_pct_str} |")
        except Exception as e:
            logger.warning(f"Error in temporal profiling: {e}")

    # 3. Categorical analyses
    cat_cols = [c for c in df_copy.columns if any(x in str(c).lower() for x in ("name", "type", "kind", "class", "lvl", "status", "tb", "block", "org", "proc", "блок", "процесс", "статус"))]
    cat_cols = [c for c in cat_cols if c not in date_cols and c not in money_cols and "id" not in str(c).lower() and "sid" not in str(c).lower()]
    
    # Exclude functional block col from cat_cols so it isn't duplicate processed
    if fb_lvl2_col and fb_lvl2_col in cat_cols:
        cat_cols.remove(fb_lvl2_col)

    # Clean functional block level 2 prioritization
    if fb_lvl2_col:
        try:
            grp = df_copy.groupby(fb_lvl2_col)
            sorted_grp = sorted(grp, key=lambda x: len(x[1]), reverse=True)
            lines.append(f"\n**Показатели по функциональным блокам (уровень 2):**")
            lines.append("| Функциональный блок | Число инцидентов | % от общего | Сумма потерь | % потерь |")
            lines.append("|---|---|---|---|---|")
            for val, group in sorted_grp[:5]:
                # Exclude technical codes or numeric keys
                if str(val).startswith("SBR_") or str(val).isdigit():
                    continue
                v_count = len(group)
                v_pct = (v_count / total_rows) * 100
                v_loss_sum = 0
                v_loss_pct_str = "—"
                if incdnt_sum_col is not None:
                    v_loss_sum = _to_numeric_clean(group[incdnt_sum_col]).sum()
                    if total_loss > 0:
                        v_loss_pct_str = f"{(v_loss_sum / total_loss) * 100:.1f}%"
                lines.append(f"| {val} | {v_count} | {v_pct:.1f}% | {format_loss(v_loss_sum)} | {v_loss_pct_str} |")
        except Exception as e:
            logger.warning(f"Error in functional block level 2 profiling: {e}")

    # Process Level 4
    proc_lvl4_col = next((c for c in df_copy.columns if str(c).lower() in ("process_lvl_4_name", "процесс – уровень 4", "процесс - уровень 4")), None)
    if proc_lvl4_col:
        try:
            grp = df_copy.groupby(proc_lvl4_col)
            sorted_grp = sorted(grp, key=lambda x: len(x[1]), reverse=True)
            lines.append(f"\n**Показатели по процессам (уровень 4):**")
            lines.append("| Бизнес-процесс (Уровень 4) | Число инцидентов | % от общего | Сумма потерь | % потерь |")
            lines.append("|---|---|---|---|---|")
            for val, group in sorted_grp[:5]:
                v_count = len(group)
                v_pct = (v_count / total_rows) * 100
                v_loss_sum = 0
                v_loss_pct_str = "—"
                if incdnt_sum_col is not None:
                    v_loss_sum = _to_numeric_clean(group[incdnt_sum_col]).sum()
                    if total_loss > 0:
                        v_loss_pct_str = f"{(v_loss_sum / total_loss) * 100:.1f}%"
                lines.append(f"| {val} | {v_count} | {v_pct:.1f}% | {format_loss(v_loss_sum)} | {v_loss_pct_str} |")
        except Exception as e:
            logger.warning(f"Error in process level 4 profiling: {e}")

    if cat_cols:
        lines.append("\n#### Распределение по категориям:")
        for col in cat_cols[:4]: # Top 4 categorical columns
            try:
                grp = df_copy.groupby(col)
                # Sort by count descending
                sorted_grp = sorted(grp, key=lambda x: len(x[1]), reverse=True)
                
                is_status_history_col = False
                if running_skill == "deleted_ior":
                    col_lower = str(col).lower()
                    if any(x in col_lower for x in ("status", "статус", "stts_chng", "stts")):
                        # check if there are values other than "удален", "удалён"
                        unique_vals = df_copy[col].dropna().unique()
                        unique_vals_lower = [str(v).lower().strip() for v in unique_vals]
                        if any(v not in ("удален", "удалён") for v in unique_vals_lower):
                            is_status_history_col = True

                if is_status_history_col:
                    lines.append(f"\n**Показатели по колонке '{col}' (Топ-5) [статусы, которые инцидент проходил ДО удаления]:**")
                else:
                    lines.append(f"\n**Показатели по колонке '{col}' (Топ-5):**")
                lines.append("| Значение | Число инцидентов | % от общего | Сумма потерь | % потерь |")
                lines.append("|---|---|---|---|---|")
                
                for val, group in sorted_grp[:5]:
                    v_count = len(group)
                    v_pct = (v_count / total_rows) * 100
                    v_loss_sum = 0
                    v_loss_pct_str = "—"
                    
                    if incdnt_sum_col is not None:
                        v_loss_sum = _to_numeric_clean(group[incdnt_sum_col]).sum()
                        if total_loss:
                            v_loss_pct_str = f"{(v_loss_sum / total_loss) * 100:.1f}%"
                            
                    lines.append(f"| {val} | {v_count} | {v_pct:.1f}% | {format_loss(v_loss_sum)} | {v_loss_pct_str} |")
            except Exception as e:
                logger.warning(f"Error in categorical profiling for {col}: {e}")

    # Detection channels statistics
    det_col = next((c for c in df_copy.columns if str(c).lower() in ("incdnt_detection_person_name", "кем выявлено событие")), None)
    if det_col:
        try:
            lines.append("\n#### Анализ каналов выявления событий:")
            # 1. Detected by clients
            client_mask = df_copy[det_col].astype(str).str.lower().str.contains("клиент", na=False)
            client_df = df_copy[client_mask]
            client_count = len(client_df)
            client_loss = _to_numeric_clean(client_df[incdnt_sum_col]).sum() if incdnt_sum_col and not client_df.empty else 0.0
            lines.append(f"- **Выявлено клиентами**: {client_count} инцидентов (сумма потерь: {format_loss(client_loss)})")
            
            # 2. Detected by external regulators / supervisors
            reg_mask = df_copy[det_col].astype(str).str.lower().str.contains("внешн|регул|контрол|орган", na=False)
            reg_df = df_copy[reg_mask]
            reg_count = len(reg_df)
            reg_loss = _to_numeric_clean(reg_df[incdnt_sum_col]).sum() if incdnt_sum_col and not reg_df.empty else 0.0
            lines.append(f"- **Выявлено внешними контролирующими органами/регуляторами**: {reg_count} инцидентов (сумма потерь: {format_loss(reg_loss)})")
        except Exception as e:
            logger.warning(f"Error calculating detection channels stats: {e}")

    # 4. Outliers (Top 3 largest losses)
    id_cols = [c for c in df_copy.columns if any(x in str(c).lower() for x in ("id", "sid", "key", "номер", "идентификатор"))]
    if id_cols and incdnt_sum_col is not None:
        primary_id = id_cols[0]
        try:
            # Sort by loss sum descending
            temp_df = df_copy.copy()
            temp_df[incdnt_sum_col] = _to_numeric_clean(temp_df[incdnt_sum_col])
            top_3 = temp_df.sort_values(by=incdnt_sum_col, ascending=False).head(3)
            lines.append("\n#### Топ-3 крупнейших инцидентов по сумме потерь:")
            for idx, row in top_3.iterrows():
                sid_val = row[primary_id]
                loss_val = row[incdnt_sum_col]
                pct_val = (loss_val / total_loss * 100) if total_loss else 0
                
                # Check status if available
                status_str = ""
                status_cols = [c for c in df_copy.columns if "status" in str(c).lower() or "статус" in str(c).lower()]
                if status_cols:
                    status_str = f" (Статус: {row[status_cols[0]]})"
                    
                lines.append(f"- **{sid_val}**: {format_loss(loss_val)} ({pct_val:.1f}% от всех потерь){status_str}")
        except Exception as e:
            logger.warning(f"Error calculating outliers: {e}")

    # 5. Advanced Concentration (Pareto & Outliers)
    advanced_stats = calculate_advanced_stats(df_copy)
    if advanced_stats:
        lines.append("\n#### Концентрация потерь (Топ-10 инцидентов):")
        if "top_10_sum" in advanced_stats:
            lines.append(f"- **Суммарные потери Топ-10 инцидентов**: {format_loss(advanced_stats['top_10_sum'])} ({advanced_stats['top_10_pct']:.1f}% от всей суммы потерь)")

    # 6. Autoregistration
    autoreg_cols = [c for c in df_copy.columns if "autoreg" in str(c).lower() or "авторег" in str(c).lower()]
    if autoreg_cols:
        col = autoreg_cols[0]
        try:
            auto_cnt = df_copy[df_copy[col].astype(str).str.upper().str.startswith('Y') | (df_copy[col] == True)].shape[0]
            auto_pct = (auto_cnt / total_rows) * 100
            lines.append(f"\n- **Авторегистрация**: {auto_cnt} инцидентов ({auto_pct:.1f}% от всей выгрузки)")
        except Exception as e:
            logger.warning(f"Error calculating autoregistration: {e}")

    # Check for risk profile column
    rp_id_col = next((c for c in df_copy.columns if str(c).lower() in ("risk_profile_id", "идентификатор профиля риска")), None)
    rp_name_col = next((c for c in df_copy.columns if str(c).lower() in ("risk_profile_name", "наименование профиля риска")), None)
    if rp_id_col and rp_name_col:
        try:
            grp = df_copy.groupby([rp_id_col, rp_name_col])
            sorted_grp = sorted(grp, key=lambda x: len(x[1]), reverse=True)
            if sorted_grp:
                top_rp = sorted_grp[0][0]
                lines.append(f"\n- **Основной вид рискового события**: {top_rp[0]} - {top_rp[1]}")
        except Exception as e:
            logger.warning(f"Error calculating risk profile stats: {e}")

    if total_rows <= 30:
        lines.append("\n### Сводная таблица данных:\n")
        lines.append(_df_to_markdown_clean(df_copy))

    return "\n".join(lines)


async def analyze_incident_descriptions(df: pd.DataFrame) -> str:
    """
    Batches incident descriptions to prevent LLM context window overflows.
    Uses async local Qwen analysis for summaries (top 60, 3 batches of 20).
    """
    import re
    # Locate column names
    loss_cols = ["incdnt_sum", "Общая сумма всех последствий (руб.)", "Общая сумма последствий (руб.)", "Сумма последствий, ₽"]
    primary_loss = next((c for c in loss_cols if c in df.columns), None)
    if not primary_loss:
        money_cols = [c for c in df.columns if any(x in str(c).lower() for x in ("sum", "loss", "dmg", "rub", "amt", "потер", "убыт", "возмещ", "сумм"))]
        loss_cols_fallback = [c for c in money_cols if any(x in str(c).lower() for x in ("loss", "dmg", "потер", "убыт", "sum", "сумм")) and not any(r in str(c).lower() for r in ("rec", "возмещ", "возврат"))]
        if loss_cols_fallback:
            primary_loss = loss_cols_fallback[0]
            
    id_cols = [c for c in df.columns if any(x in str(c).lower() for x in ("id", "sid", "key", "номер", "идентификатор"))]
    primary_id = id_cols[0] if id_cols else None
    
    full_desc_col = next((c for c in df.columns if str(c).lower() in ("incdnt_full_descr_txt", "подробное описание", "полное описание", "описание")), None)
    sum_desc_col = next((c for c in df.columns if str(c).lower() in ("incdnt_summary_descr_txt", "краткое описание", "аннотация")), None)
    
    df_sorted = df.copy()
    if primary_loss:
        df_sorted[primary_loss] = _to_numeric_clean(df_sorted[primary_loss])
        df_sorted = df_sorted.sort_values(by=primary_loss, ascending=False)
        
    descriptions = []
    
    # Extract top 30 largest incidents
    top_30_df = df_sorted.head(30)
    for _, row in top_30_df.iterrows():
        sid = row[primary_id] if primary_id and primary_id in row else "—"
        val = row[primary_loss] if primary_loss and primary_loss in row else 0.0
        desc = ""
        # Prefer full description, fallback to summary description
        if full_desc_col and full_desc_col in row and pd.notna(row[full_desc_col]) and str(row[full_desc_col]).strip():
            desc = str(row[full_desc_col]).strip()
        elif sum_desc_col and sum_desc_col in row and pd.notna(row[sum_desc_col]) and str(row[sum_desc_col]).strip():
            desc = str(row[sum_desc_col]).strip()
        else:
            # Fallback check for any description-like column
            desc_fallback_col = next((c for c in df.columns if any(x in str(c).lower() for x in ("descr", "описание", "аннотация"))), None)
            if desc_fallback_col and desc_fallback_col in row and pd.notna(row[desc_fallback_col]):
                desc = str(row[desc_fallback_col]).strip()
                
        if desc:
            loss_str = f" (Сумма потерь: {format_loss(val)})" if val > 0 else ""
            descriptions.append(f"Идентификатор: {sid}{loss_str} | Описание: {desc}")
            
    if not descriptions:
        return ""
        
    # Split into exactly 3 batches of at most 10 items
    batch_1 = descriptions[:10]
    batch_2 = descriptions[10:20]
    batch_3 = descriptions[20:30]
    
    from local_qwen import ask_local_qwen
    
    async def analyze_batch(batch_items, batch_num):
        if not batch_items:
            return ""
        prompt = (
            f"Ниже представлены описания крупных инцидентов операционного риска (Пакет {batch_num}). "
            f"Для каждого инцидента подготовь краткую выжимку (1-2 предложения), объясняющую суть произошедшего. "
            f"Обязательно сохрани связь с Идентификатором инцидента.\n\n"
            + "\n".join(batch_items)
        )
        try:
            res = await asyncio.to_thread(
                ask_local_qwen, [
                    {"role": "system", "content": "Ты — аналитик Службы внутреннего аудита. Опиши суть каждого инцидента строго индивидуально, в формате 'Идентификатор: [краткая суть]'. Пиши нейтральным деловым языком. Не делай общих выводов."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=1024
            )
            return str(res)
        except Exception as e:
            logger.error(f"Error analyzing descriptions batch {batch_num}: {e}")
            return ""
            
    results = await asyncio.gather(
        analyze_batch(batch_1, 1),
        analyze_batch(batch_2, 2),
        analyze_batch(batch_3, 3)
    )
    
    combined = []
    if results[0]:
        combined.append(f"### Результаты анализа описаний инцидентов (Пакет 1):\n{results[0]}")
    if results[1]:
        combined.append(f"### Результаты анализа описаний инцидентов (Пакет 2):\n{results[1]}")
    if results[2]:
        combined.append(f"### Результаты анализа описаний инцидентов (Пакет 3):\n{results[2]}")
        
    return "\n\n".join(combined)


def get_running_skill_id(df: pd.DataFrame, session_id: str) -> Optional[str]:
    try:
        from backend.agent.state import get_session_state
        state = get_session_state(session_id)
        # 1. Identity match
        for df_id, meta in state.dataframe_meta.items():
            stored_df = state.dataframes.get(df_id)
            if stored_df is df:
                created_by = meta.created_by
                if created_by.startswith("run_preset:"):
                    return created_by.split(":", 1)[1]
        # 2. Fallback: columns and length match
        for df_id, meta in state.dataframe_meta.items():
            stored_df = state.dataframes.get(df_id)
            if stored_df is not None and len(stored_df) == len(df) and list(stored_df.columns) == list(df.columns):
                created_by = meta.created_by
                if created_by.startswith("run_preset:"):
                    return created_by.split(":", 1)[1]
        # 3. Fallback: check last dataframe meta
        if state.dataframe_meta:
            last_meta = list(state.dataframe_meta.values())[-1]
            if last_meta.created_by.startswith("run_preset:"):
                return last_meta.created_by.split(":", 1)[1]
    except Exception as e:
        logger.warning(f"Error determining running skill_id: {e}")
    return None


async def summarize_deletion_comments(comments: list[str]) -> str:
    if not comments:
        return ""
    unique_comments = list(set(comments))[:300]
    prompt = (
        "Ниже представлены комментарии сотрудников об основаниях и причинах удаления инцидентов операционного риска.\n"
        "Проанализируй их и подготовь краткое структурированное резюме наиболее популярных причин удаления (например, дубликаты, сбои АС, ошибки ввода), "
        "используя строго нейтральный деловой язык. Не упоминай точное количество проанализированных комментариев:\n\n"
        + "\n".join(f"- {c}" for c in unique_comments)
    )
    from local_qwen import ask_local_qwen
    try:
        res = await asyncio.to_thread(
            ask_local_qwen, [
                {"role": "system", "content": "Ты — аналитик Службы внутреннего аудита. Проведи анализ комментариев о причинах удаления инцидентов и выдели ключевые системные или операционные причины удаления."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=1024
        )
        return str(res)
    except Exception as e:
        logger.error(f"Error summarizing deletion comments: {e}")
        return f"Ошибка при анализе комментариев удаления: {e}"


async def generate_hypothesis_narrative(user_msg: str, df: pd.DataFrame, file_info: dict, session_id: str) -> str:
    """
    Generates a natural language narrative (with hypothesis / insights) based on the dataframe profile and optional plot.
    """
    if len(df) == 0:
        return "### Общая информация о выгрузке:\nВыгрузка пуста. Нет данных для формирования гипотезы."

    # 1. Determine running skill
    skill_id = get_running_skill_id(df, session_id)
    if skill_id is None:
        skill_id = "ior_hypothesis_v2"
        
    running_skill = skill_id or ""
    if running_skill.endswith("_v2"):
        normalized_skill = running_skill[:-3]
    else:
        normalized_skill = running_skill

    # Protective check for TB filter leak (Task 15)
    try:
        tb_keywords = ["московск", "сибирск", "уральск", "байкальск", "дальневосточ", "поволжск", "северо-запад", "центральн", "юго-запад"]
        requested_tb = None
        for kw in tb_keywords:
            if kw in user_msg.lower():
                requested_tb = kw
                break
        if requested_tb:
            tb_col = next((c for c in df.columns if any(x in str(c).lower() for x in ("тб", "орг. структура", "org_struct_lvl_3_name"))), None)
            if tb_col:
                unique_tbs = df[tb_col].dropna().unique()
                if len(unique_tbs) > 1:
                    logger.warning(
                        f"[LEAK DETECTED] User requested TB containing '{requested_tb}', "
                        f"but DataFrame has multiple unique TB values: {unique_tbs}"
                    )
    except Exception as leak_err:
        logger.error(f"Error checking filter leak: {leak_err}")

    # 2. Агрегация по статусам на старте
    status_col = next((c for c in df.columns if any(x == str(c).lower().strip() for x in ("incdnt_status_name", "статус события", "статус", "статус инцидента", "status"))), None)
    group_counts = {"Группа 1: Утверждение": 0, "Группа 2: Черновик/Исследование": 0, "Группа 3: Удален": 0}
    if status_col:
        statuses = df[status_col].astype(str).str.strip()
        for s in statuses:
            s_lower = s.lower()
            if s_lower in ("утверждение", "утверждён", "утвержден"):
                group_counts["Группа 1: Утверждение"] += 1
            elif s_lower in ("черновик", "исследование"):
                group_counts["Группа 2: Черновик/Исследование"] += 1
            elif s_lower in ("удалён", "удален"):
                group_counts["Группа 3: Удален"] += 1

    # 3. Фильтрация для анализа: группа Удален или Утверждение
    is_deleted = (normalized_skill == "deleted_ior")
    is_nonfinancial = (normalized_skill == "ior_nonfinancial_consequences")

    if is_deleted:
        if status_col:
            df_analysis = df[df[status_col].astype(str).str.strip().str.lower().isin(["удалён", "удален"])].copy()
        else:
            df_analysis = df.copy()
    else:
        if status_col:
            df_analysis = df[df[status_col].astype(str).str.strip().str.lower().isin(["утверждение", "утверждён", "утвержден"])].copy()
        else:
            df_analysis = df.copy()

    if df_analysis.empty and not df.empty:
        df_analysis = df.copy()

    # 3.5. Расчет общих сумм по всей выгрузке (все статусы)
    total_loss_all = 0.0
    direct_loss_all = 0.0
    recovery_loss_all = 0.0
    
    if not df.empty:
        total_loss_all, direct_loss_all = get_total_and_direct_loss(df)
        primary_rec_all = get_recovery_column(df, running_skill)
        if primary_rec_all:
            recovery_loss_all = _to_numeric_clean(df[primary_rec_all]).sum()
            
    net_loss_all = max(0.0, total_loss_all - recovery_loss_all)

    # 4. Расчет потерь и возмещений для группы анализа (Утвержденные / Удаленные)
    total_loss, direct_loss = get_total_and_direct_loss(df_analysis)

    recovery_loss = 0.0
    primary_rec_analysis = get_recovery_column(df_analysis, running_skill)
    if primary_rec_analysis:
        recovery_loss = _to_numeric_clean(df_analysis[primary_rec_analysis]).sum()

    net_loss = max(0.0, total_loss - recovery_loss)

    # 5. Формирование префикса в зависимости от типа отчета
    overall_stats = ""
    if normalized_skill not in ("ior_nonfinancial_consequences", "credit_no_way_collect_debt", "report_period_specific_ior"):
        overall_stats = (
            f"### Общая информация по выгрузке:\n"
            f"- **Всего инцидентов**: {len(df)}\n"
            f"- **Общая сумма потерь**: {format_loss(total_loss_all)}\n"
            f"- **Общая сумма возмещений**: {format_loss(recovery_loss_all)}\n"
            f"- **Чистые потери (Net Loss)**: {format_loss(net_loss_all)}\n\n"
        )
    elif normalized_skill == "ior_nonfinancial_consequences":
        overall_stats = (
            f"### Общая информация по выгрузке:\n"
            f"- **Всего качественных последствий**: {len(df)}\n\n"
        )

    if normalized_skill == "ior_nonfinancial_consequences":
        qualitative_col = next((c for c in df.columns if str(c).lower().strip() in ("nonfin_impact_kind_name", "вид качественной потери")), None)
        qualitative_summary = ""
        if qualitative_col:
            counts = df[qualitative_col].value_counts()
            qualitative_summary = "#### Распределение по видам качественных потерь:\n"
            for k, v in counts.items():
                qualitative_summary += f"- **{k}**: {v}\n"
            qualitative_summary += "\n"

        prefix = (
            f"### Распределение инцидентов по статусам:\n"
            f"- **Группа 1: Утверждение**: {group_counts['Группа 1: Утверждение']}\n"
            f"- **Группа 2: Черновик/Исследование**: {group_counts['Группа 2: Черновик/Исследование']}\n"
            f"- **Группа 3: Удален**: {group_counts['Группа 3: Удален']}\n\n"
            f"{qualitative_summary}"
        )
    elif normalized_skill == "deleted_ior":
        prefix = (
            f"### Распределение инцидентов по статусам:\n"
            f"- **Группа 1: Утверждение**: {group_counts['Группа 1: Утверждение']}\n"
            f"- **Группа 2: Черновик/Исследование**: {group_counts['Группа 2: Черновик/Исследование']}\n"
            f"- **Группа 3: Удален**: {group_counts['Группа 3: Удален']}\n\n"
            f"По инцидентам в статусе **Удален/Удалён** (Группа 3):\n"
            f"- **Сумма потерь по удаленным инцидентам**: {format_loss(total_loss)}\n"
            f"- **Сумма возмещений по удаленным инцидентам**: {format_loss(recovery_loss)}\n\n"
        )
    elif normalized_skill == "financial_consequences_ior":
        type_summary = ""
        type_col = next((c for c in df.columns if str(c).lower().strip() in ("fin_impact_type_name", "тип последствия")), None)
        if type_col:
            counts = df[type_col].value_counts()
            type_summary = "#### Распределение по типам финансовых последствий:\n"
            for k, v in counts.items():
                type_summary += f"- **{k}**: {v}\n"
            type_summary += "\n"
        prefix = (
            f"### Распределение инцидентов по статусам:\n"
            f"- **Группа 1: Утверждение**: {group_counts['Группа 1: Утверждение']}\n"
            f"- **Группа 2: Черновик/Исследование**: {group_counts['Группа 2: Черновик/Исследование']}\n"
            f"- **Группа 3: Удален**: {group_counts['Группа 3: Удален']}\n\n"
            f"{type_summary}"
            f"По последствиям инцидентов:\n"
            f"- **Суммарные потери по последствиям**: {format_loss(total_loss)}\n\n"
        )
    elif normalized_skill == "vozmeshenie_ior":
        type_summary = ""
        type_col = next((c for c in df.columns if str(c).lower().strip() in ("recovery_type_name", "тип возмещения")), None)
        if type_col:
            counts = df[type_col].value_counts()
            type_summary = "#### Распределение по видам/источникам возмещений:\n"
            for k, v in counts.items():
                type_summary += f"- **{k}**: {v}\n"
            type_summary += "\n"
        prefix = (
            f"### Распределение инцидентов по статусам:\n"
            f"- **Группа 1: Утверждение**: {group_counts['Группа 1: Утверждение']}\n"
            f"- **Группа 2: Черновик/Исследование**: {group_counts['Группа 2: Черновик/Исследование']}\n"
            f"- **Группа 3: Удален**: {group_counts['Группа 3: Удален']}\n\n"
            f"{type_summary}"
            f"По возмещениям инцидентов:\n"
            f"- **Сумма полученных возмещений**: {format_loss(recovery_loss)}\n\n"
        )
    elif normalized_skill == "credit_no_way_collect_debt":
        total_debt = 0.0
        total_rvps = 0.0
        total_pledge = 0.0
        debt_cols = [c for c in df.columns if any(x in str(c).lower() for x in ("debt", "credit", "loan", "договор", "задолженность", "сумма"))]
        rvps_cols = [c for c in df.columns if any(x in str(c).lower() for x in ("rvps", "резерв", "рвпс"))]
        pledge_cols = [c for c in df.columns if any(x in str(c).lower() for x in ("pledge", "залог", "обеспечение"))]
        if debt_cols:
            total_debt = _to_numeric_clean(df[debt_cols[0]]).sum()
        if rvps_cols:
            total_rvps = _to_numeric_clean(df[rvps_cols[0]]).sum()
        if pledge_cols:
            total_pledge = _to_numeric_clean(df[pledge_cols[0]]).sum()
        prefix = (
            f"### Сводная информация по проблемной задолженности:\n"
            f"- **Количество кредитных договоров**: {len(df)}\n"
            f"- **Общая сумма задолженности**: {format_loss(total_debt)}\n"
            f"- **Сформированный резерв (РВПС)**: {format_loss(total_rvps)}\n"
            f"- **Оценочная стоимость залогов**: {format_loss(total_pledge)}\n\n"
        )
    elif normalized_skill == "report_period_specific_ior":
        if df.empty:
            prefix = "### Детальное досье по инциденту:\n\nДанные по инциденту отсутствуют.\n\n"
        else:
            first_row = df.iloc[0]
            sid_col_name = next((c for c in df.columns if str(c).lower().strip() in ("incdnt_sid", "идентификатор события")), "Идентификатор события")
            status_col_name = next((c for c in df.columns if str(c).lower().strip() in ("incdnt_status_name", "статус события", "статус")), "Статус события")
            
            total_loss_col = next((c for c in df.columns if str(c).lower().strip() in ("incdnt_sum", "общая сумма всех последствий (руб.)", "общая сумма последствий (руб.)", "сумма последствий, ₽")), None)
            direct_loss_col = next((c for c in df.columns if str(c).lower().strip() in ("incdnt_drct_dmg_sum", "прямая потеря – итого (руб.)", "прямая потеря - итого (руб.)")), None)
            recovery_col_name = next((c for c in df.columns if str(c).lower().strip() in ("recovery_rub_amt_aggr", "возмещение – итого по инциденту (руб.)", "возмещение - итого по инциденту (руб.)")), None)
            
            spec_sid = first_row[sid_col_name] if sid_col_name in df.columns else "EVE-XXXXXXX"
            spec_status = first_row[status_col_name] if status_col_name in df.columns else "Неизвестно"
            
            spec_total_loss = _to_numeric_clean(pd.Series([first_row[total_loss_col]])).iloc[0] if total_loss_col and total_loss_col in df.columns else 0.0
            spec_direct_loss = _to_numeric_clean(pd.Series([first_row[direct_loss_col]])).iloc[0] if direct_loss_col and direct_loss_col in df.columns else 0.0
            spec_recovery = _to_numeric_clean(pd.Series([first_row[recovery_col_name]])).iloc[0] if recovery_col_name and recovery_col_name in df.columns else 0.0
            spec_net_loss = max(0.0, spec_total_loss - spec_recovery)
            
            prefix = (
                f"### Детальное досье по инциденту {spec_sid}:\n"
                f"- **Идентификатор события**: {spec_sid}\n"
                f"- **Статус события**: {spec_status}\n"
                f"- **Общие потери**: {format_loss(spec_total_loss)}\n"
                f"- **Прямые потери**: {format_loss(spec_direct_loss)}\n"
                f"- **Сумма возмещений**: {format_loss(spec_recovery)}\n"
                f"- **Чистые потери (Net Loss)**: {format_loss(spec_net_loss)}\n\n"
            )
    else:
        prefix = (
            f"### Распределение инцидентов по статусам:\n"
            f"- **Группа 1: Утверждение**: {group_counts['Группа 1: Утверждение']}\n"
            f"- **Группа 2: Черновик/Исследование**: {group_counts['Группа 2: Черновик/Исследование']}\n"
            f"- **Группа 3: Удален**: {group_counts['Группа 3: Удален']}\n\n"
            f"По инцидентам в статусе **Утвержден/Утверждение** (Группа 1):\n"
            f"- **Общие потери**: {format_loss(total_loss)}\n"
            f"- **Прямые потери**: {format_loss(direct_loss)}\n"
            f"- **Сумма возмещений**: {format_loss(recovery_loss)}\n"
            f"- **Чистые потери (Net Loss)**: {format_loss(net_loss)}\n\n"
        )

    prefix = overall_stats + prefix

    # 6. Сбор информации об удаленных (если мы не в summarization_only и не в deleted_ior)
    is_summarization_only = (len(df_analysis) < 20) or (normalized_skill == "report_period_specific_ior")
    
    deleted_text = ""
    if not is_deleted and not is_summarization_only:
        deleted_count = 0
        deleted_loss = 0.0
        deleted_rec = 0.0
        
        if status_col:
            df_deleted = df[df[status_col].astype(str).str.strip().str.lower().isin(["удалён", "удален"])].copy()
            deleted_count = len(df_deleted)
            
            loss_cols = ["incdnt_sum", "Общая сумма всех последствий (руб.)", "Общая сумма последствий (руб.)", "Сумма последствий, ₽"]
            primary_loss = next((c for c in loss_cols if c in df.columns), None)
            if not primary_loss:
                money_cols = [c for c in df.columns if any(x in str(c).lower() for x in ("sum", "loss", "dmg", "rub", "amt", "потер", "убыт", "возмещ", "сумм"))]
                loss_cols_fallback = [c for c in money_cols if any(x in str(c).lower() for x in ("loss", "dmg", "потер", "убыт", "sum", "сумм")) and not any(r in str(c).lower() for r in ("rec", "возмещ", "возврат"))]
                if loss_cols_fallback:
                    primary_loss = loss_cols_fallback[0]
                    
            rec_cols_list = [
                "recovery", 
                "сумма возмещений", 
                "сумма возмещения", 
                "сумма возмещения (руб.)", 
                "сумма возмещений (руб.)", 
                "возмещ", 
                "recovery_rub_amt", 
                "recovery_rub_amt_aggr", 
                "сумма возмещения (агрегатор)", 
                "возмещение - итого по инциденту (руб.)"
            ]
            primary_rec = next((c for c in rec_cols_list if c in df.columns), None)
            if not primary_rec:
                money_cols = [c for c in df.columns if any(x in str(c).lower() for x in ("sum", "loss", "dmg", "rub", "amt", "потер", "убыт", "возмещ", "сумм"))]
                rec_cols_fallback = [c for c in money_cols if any(x in str(c).lower() for x in ("rec", "возмещ", "возврат"))]
                if rec_cols_fallback:
                    primary_rec = rec_cols_fallback[0]
                    
            if primary_loss and not df_deleted.empty:
                deleted_loss = _to_numeric_clean(df_deleted[primary_loss]).sum()
            if primary_rec and not df_deleted.empty:
                deleted_rec = _to_numeric_clean(df_deleted[primary_rec]).sum()

        deleted_text = (
            f"\n### Информация об удаленных инцидентах:\n"
            f"- **Количество удаленных инцидентов**: {deleted_count}\n"
            f"- **Сумма потерь по удаленным инцидентам**: {format_loss(deleted_loss)}\n"
            f"- **Сумма возмещений по удаленным инцидентам**: {format_loss(deleted_rec)}\n\n"
            f"Если вы хотите больше узнать о причинах удаления инцидентов, создайте новую сессию и запросите выгрузку по удаленным инцидентам.\n\n"
        )

    retro_text = ""
    profile = profile_dataframe(df_analysis, running_skill=normalized_skill)

    # 7. Check if chart is needed
    chart_file_id = None
    if normalized_skill not in ("ior_nonfinancial_consequences", "deleted_ior"):
        low_msg = user_msg.lower()
        is_dynamics_query = any(x in low_msg for x in ("динамик", "график", "тренд", "изменен", "рост", "спад"))
        if is_dynamics_query or len(df_analysis) > 30:
            chart_file_id = await asyncio.to_thread(generate_dynamics_chart, df_analysis, session_id)

    # 8. Extract summaries of descriptions / comments (always analyze through Qwen)
    if normalized_skill == "deleted_ior":
        comment_col = next((c for c in df_analysis.columns if str(c).lower().strip() in ("stts_chng_comment_txt", "комментарий / причина действия", "комментарий / причина", "комментарий", "причина действия", "причина удаления")), None)
        comments_summary = ""
        if comment_col:
            comments_series = df_analysis[comment_col].dropna().astype(str).str.strip()
            unique_comments = [c for c in comments_series.unique() if c and c.lower() not in ("nan", "none", "—", "-")]
            comments_summary = await summarize_deletion_comments(unique_comments)
            
        # Combine comments summary and incident descriptions to enrich the deleted hypothesis
        inc_desc_summary = await analyze_incident_descriptions(df_analysis)
        if comments_summary:
            desc_summary = f"{comments_summary}\n\n#### Детальный анализ содержания удаленных инцидентов:\n{inc_desc_summary}"
        else:
            desc_summary = inc_desc_summary
    else:
        desc_summary = await analyze_incident_descriptions(df_analysis)

    # 9. Prompts selection
    prompts = {}
    
    prompts["ior_hypothesis"] = """Ты — эксперт-аналитик Службы внутреннего аудита.
Твоя задача — провести анализ представленного профиля данных инцидентов операционного риска и сформулировать аналитические гипотезы о возможных причинах этих инцидентов.

Пиши максимально простым, понятным и человеческим языком без эмоций, преувеличений и сложного IT или узкоспециализированного корпоративного жаргона. Текст должен быть легким для чтения и понятным любому линейному аналитику или сотруднику.
- Полностью избегай оценочных и экспрессивных выражений (например, "экстремальная концентрация", "катастрофический сбой", "немедленный аудит").
- Избегай перегруженных сложных терминов. Вместо тяжелого IT-жаргона используй простые аналоги. Обычные технические термины использовать можно.
- Излагай факты и предположения сухо, четко, структурированно, с использованием списков и ключевых метрик.

Придерживайся следующей структуры отчета:

### 1. Общая сводка данных
- Кратко перечисли ключевые показатели: общее число инцидентов, общая сумма всех последствий, сумма возмещений и чистые потери по группе Утверждение.
- Укажи, какая самая частая причина инцидентов (основная причина / тип события).
- Опиши общую динамику регистрации во времени.
- Важно: пиши этот раздел как чистое, сухое описание фактов БЕЗ каких-либо выводов, анализа, интерпретаций или гипотез.

### 2. Выявленные аномалии и динамика трендов
- Опиши динамику во времени (сезонность, тренды спада/роста, временные всплески). Сформулируй предположение о возможных причинах временного всплеска.
- Выдели распределение по территориальным банкам (ТБ) или процессам, перечислив лидеров по сумме потерь и количеству инцидентов.

### 3. Концентрация рисков и системные факторы
- Опиши концентрацию потерь: укажи суммарный вклад Топ-10 крупнейших инцидентов (их точную сумму и процент от общего объема потерь).
- Укажи конкретные идентификаторы событий (например, EVE-XXXXXXX) из топа крупнейших инцидентов и проанализируй их вклад.
- Поле "Тип события - уровень 1" (incdnt_type_lvl_1_name) транслируй в отчет как "Основная причина".
- Оцени долю авторегистрации (процент авторегистрированных инцидентов).
- Важно: НЕ перегружай отчет бесконечным перечислением процентов концентрации и долей.

### 4. Аналитические гипотезы для аудиторской проверки
Сформулируй ровно 3 аналитические гипотезы, закрепив за каждой обязательный ракурс:
- Гипотеза 1: Обязательно строится на концентрации потерь (крупнейшие инциденты, конкретные EVE-id и точные суммы потерь).
- Гипотеза 2: Обязательно строится на географии/оргструктуре (распределение по ТБ/блокам).
- Гипотеза 3: Обязательно строится на содержании описаний инцидентов (реальные факты из анализа описаний инцидентов desc_summary), а не на агрегированных долях/процентах.
Гипотезы не могут опираться на одну и ту же метрику или вести к одному и тому же выводу о причинах — если два вывода совпадают по сути, один из них нужно заменить. Каждая гипотеза должна сопровождаться конкретными шагами проверки (не более 3 шагов на гипотезу, без указания вымышленных числовых порогов) и ожидаемым результатом (описанным качественно, без придуманных чисел)."""

    prompts["deleted_ior"] = """Ты — эксперт-аналитик Службы внутреннего аудита.
Твоя задача — провести анализ удаленных инцидентов операционного риска на основе предоставленных данных и результатов суммаризации комментариев сотрудников о причинах удаления.

Пиши максимально простым, понятным и человеческим языком без эмоций, преувеличений и сложного IT или узкоспециализированного корпоративного жаргона. Текст должен быть легким для чтения и понятным любому линейному аналитику или сотруднику.
- Полностью избегай оценочных и экспрессивных выражений (например, "катастрофический сбой", "немедленный аудит").
- Избегай перегруженных сложных терминов. Вместо тяжелого IT-жаргона используй простые аналоги.
- Излагай факты и предположения сухо, четко, структурированно, с использованием списков и ключевых метрик.
- Важно: НЕ упоминай точное количество проанализированных комментариев или строк (например, 'проанализировано 300 комментариев'). Вместо этого пиши о популярных причинах качественно (например, "наиболее распространенной причиной является...", "часто встречаются случаи...").

Придерживайся следующей структуры отчета:

### 1. Общая сводка данных
- Кратко перечисли ключевые показатели: общее число удаленных инцидентов, общая сумма последствий по удаленным инцидентам, сумма возмещений по удаленным инцидентам.
- Укажи основные действия пользователей (например, удалено вручную, отменено).
- Опиши временную динамику удаления инцидентов.
- Важно: пиши этот раздел как чистое, сухое описание фактов БЕЗ каких-либо выводов, анализа, интерпретаций или гипотез.

### 2. Выявленные аномалии и динамика трендов
- Опиши динамику удалений во времени (сезонность, всплески). Сформулируй предположение о возможных причинах всплеска.
- Выдели распределение по территориальным банкам (ТБ) или процессам, перечислив лидеров по количеству удалений.

### 3. Концентрация рисков и системные факторы
- Опиши концентрацию удалений: укажи вклад Топ-10 крупнейших удаленных инцидентов.
- Выдели наиболее популярные причины удаления на основе предоставленного анализа комментариев сотрудников (например, дубликаты, технические сбои АС при авторегистрации, ошибки ручного ввода реквизитов).

### 4. Аналитические гипотезы для аудиторской проверки
Сформулируй ровно 3 аналитические гипотезы о причинах удаления инцидентов, закрепив обязательные ракурсы:
- Гипотеза 1: Обязательно строится на анализе комментариев сотрудников о причинах удаления (дубликаты, ошибки ручного ввода и т.д.).
- Гипотеза 2: Обязательно строится на географии/оргструктуре удалений (ТБ, лидеры по количеству удалений).
- Гипотеза 3: Обязательно строится на концентрации потерь по удаленным инцидентам (крупнейшие удаленные инциденты, конкретные EVE-id и суммы потерь).
Гипотезы не могут опираться на одну и ту же метрику или вести к одному и тому же выводу о причинах — если два вывода совпадают по сути, один из них нужно заменить. Каждая гипотеза должна сопровождаться конкретными шагами проверки (не более 3 шагов на гипотезу, без указания вымышленных числовых порогов) и ожидаемым результатом (описанным качественно, без придуманных чисел)."""

    prompts["ior_nonfinancial_consequences"] = """Ты — эксперт-аналитик Службы внутреннего аудита.
Твоя задача — провести анализ качественных (нефинансовых) последствий инцидентов операционного риска и сформулировать аналитические гипотезы.

Придерживайся следующей структуры отчета:

### 1. Общая сводка данных
- Кратко перечисли ключевые показатели: общее число инцидентов с качественными последствиями.
- Укажи распределение по видам качественных потерь (например, репутационный риск, прерывание деятельности, регуляторные санкции).
- Опиши общую динамику регистрации во времени.
- Важно: пиши этот раздел как чистое, сухое описание фактов БЕЗ каких-либо выводов, анализа, интерпретаций или гипотез.

### 2. Выявленные аномалии и динамика трендов
- Опиши динамику во времени (сезонность, тренды спада/роста, временные всплески). Сформулируй предположение о возможных причинах временного всплеска.
- Выдели распределение по территориальным банкам (ТБ) или процессам, перечислив лидеров по количеству инцидентов.

### 3. Концентрация рисков и системные факторы
- Опиши концентрацию рисков: укажи наиболее подверженные качественным рискам процессы и подразделения.
- Оцени долю авторегистрации (процент авторегистрированных инцидентов).

### 4. Аналитические гипотезы для аудиторской проверки
Сформулируй ровно 3 аналитические гипотезы по качественным рискам, закрепив обязательные ракурсы:
- Гипотеза 1: Обязательно строится на структуре качественных последствий (виды качественных потерь, например, репутационный риск или прерывание деятельности).
- Гипотеза 2: Обязательно строится на географии/оргструктуре (распределение качественных инцидентов по ТБ/процессам).
- Гипотеза 3: Обязательно строится на содержании описаний инцидентов (реальные факты из анализа описаний инцидентов desc_summary), связывая их с рисками информационных систем или поведением персонала.
Гипотезы не могут опираться на одну и ту же метрику или вести к одному и тому же выводу о причинах — если два вывода совпадают по сути, один из них нужно заменить. Каждая гипотеза должна сопровождаться конкретными шагами проверки (не более 3 шагов на гипотезу, без указания вымышленных числовых порогов) и ожидаемым результатом (описанным качественно, без придуманных чисел)."""

    prompts["financial_consequences_ior"] = """Ты — эксперт-аналитик Службы внутреннего аудита.
Твоя задача — провести анализ финансовых последствий инцидентов операционного риска на основе предоставленных детальных данных о последствиях и сформулировать аналитические гипотезы.

- Излагай факты и предположения сухо, четко, структурированно, с использованием списков и ключевых метрик.
- Важно: В данной выгрузке (детализация финансовых последствий) СТРУКТУРНО отсутствуют данные по возмещениям (возвратам денег). Ни при каких условиях не упоминай суммы или проценты возмещений (recovery) в тексте отчёта — если тебе кажется, что ты видишь такие данные в переданном профиле данных, это ошибка, полностью игнорируй их.

Придерживайся следующей структуры отчета:

### 1. Общая сводка финансовых последствий
- Кратко перечисли ключевые показатели: общее число записей о последствиях, общая сумма зафиксированных потерь.
- Укажи структуру и распределение по типам финансовых последствий (например, прямые, косвенные, нереализовавшиеся потери, потери третьих лиц, прибыль).
- Опиши временную динамику возникновения финансовых последствий.
- Важно: пиши этот раздел как чистое, сухое описание фактов БЕЗ каких-либо выводов, анализа, интерпретаций или гипотез.

### 2. Структура и виды потерь
- Выдели ключевые виды потерь на основе поля вида последствий (например, хищение, судебные расходы, списание).
- Перечисли территориальные банки (ТБ) или подразделения, лидирующие по объему финансовых потерь.

### 3. Концентрация финансовых последствий и крупные потери
- Опиши концентрацию: вклад топ-10 крупнейших последствий в общий объем потерь.
- Укажи конкретные инциденты (EVE-XXXXXXX) с наибольшими суммами последствий.

### 4. Аналитические гипотезы для аудиторской проверки
Сформулируй ровно 3 аналитические гипотезы о причинах возникновения финансовых последствий, закрепив обязательные ракурсы:
- Гипотеза 1: Обязательно строится на структуре и видах потерь (списание, судебные расходы, хищения).
- Гипотеза 2: Обязательно строится на концентрации финансовых последствий (крупные потери, конкретные инциденты EVE-id с наибольшими суммами).
- Гипотеза 3: Обязательно строится на географии/оргструктуре финансовых потерь (распределение по ТБ/подразделениям).
Гипотезы не могут опираться на одну и ту же метрику или вести к одному и тому же выводу о причинах — если два вывода совпадают по сути, один из них нужно заменить. Каждая гипотеза должна сопровождаться конкретными шагами проверки (не более 3 шагов на гипотезу, без указания вымышленных числовых порогов) и ожидаемым результатом (описанным качественно, без придуманных чисел)."""

    prompts["vozmeshenie_ior"] = """Ты — эксперт-аналитик Службы внутреннего аудита.
Твоя задача — провести анализ полученных возмещений (возвратов, страховых выплат, компенсаций) по инцидентам операционного риска и сформулировать аналитические гипотезы.

Пиши максимально простым, понятным и человеческим языком без эмоций, преувеличений и сложного IT или узкоспециализированного корпоративного жаргона. Текст должен быть легким для чтения и понятным любому линейному аналитику или сотруднику.
- Полностью избегай оценочных и экспрессивных выражений.
- Излагай факты и предположения сухо, четко, структурированно, с использованием списков и ключевых метрик.

Придерживайся следующей структуры отчета:

### 1. Общая сводка по возмещениям
- Кратко перечисли ключевые показатели: общее число записей о возмещениях, общая сумма полученных возмещений.
- Укажи распределение по типам/источникам возмещений (например, страховые выплаты, внесудебные возвраты от клиентов, взыскания с сотрудников).
- Опиши временную динамику поступления возмещений.
- Важно: пиши этот раздел как чистое, сухое описание фактов БЕЗ каких-либо выводов, анализа, интерпретаций или гипотез.

### 2. Географическая и процессная структура возмещений
- Перечисли территориальные банки (ТБ) или функциональные блоки, лидирующие по суммам возвращенных средств.
- Опиши, по каким типам инцидентов возмещения проходят наиболее успешно.

### 3. Концентрация и крупные возмещения
- Опиши концентрацию: вклад топ-10 крупнейших возмещений в общую сумму возврата.
- Выдели инциденты (EVE-XXXXXXX) с наибольшей долей возмещенных потерь.

### 4. Аналитические гипотезы для аудиторской проверки
Сформулируй ровно 3 аналитические гипотезы об эффективности процессов возмещения, закрепив обязательные ракурсы:
- Гипотеза 1: Обязательно строится на источниках возмещений (страховые выплаты, взыскания с сотрудников, возвраты от клиентов).
- Гипотеза 2: Обязательно строится на географии/оргструктуре возмещений (ТБ/блоки, лидирующие по суммам возвратов).
- Гипотеза 3: Обязательно строится на концентрации возмещений (крупнейшие возмещения по инцидентам EVE-id с высокой долей возмещенных потерь).
Гипотезы не могут опираться на одну и ту же метрику или вести к одному и тому же выводу о причинах — если два вывода совпадают по сути, один из них нужно заменить. Каждая гипотеза должна сопровождаться конкретными шагами проверки (не более 3 шагов на гипотезу, без указания вымышленных числовых порогов) и ожидаемым результатом (описанным качественно, без придуманных чисел)."""

    prompts["credit_no_way_collect_debt"] = """Ты — эксперт-аналитик Службы внутреннего аудита.
Твоя задача — провести анализ случаев невозможности взыскания задолженности по кредитным продуктам и сформулировать аналитические гипотезы.

Пиши максимально простым, понятным и человеческим языком без эмоций, преувеличений и сложного IT или узкоспециализированного корпоративного жаргона. Текст должен быть легким для чтения и понятным любому линейному аналитику или сотруднику.
- Полностью избегай оценочных и экспрессивных выражений.
- Излагай факты и предположения сухо, четко, структурированно, с использованием списков и ключевых метрик.

Придерживайся следующей структуры отчета:

### 1. Сводные показатели по кредитной задолженности
- Кратко перечисли ключевые показатели: общее количество проанализированных кредитных договоров, общая сумма задолженности, размер сформированного резерва (РВПС).
- Укажи распределение по типам заемщиков (физические лица, юридические лица) и продуктам.
- Важно: пиши этот раздел как чистое, сухое описание фактов БЕЗ каких-либо выводов, анализа, интерпретаций или гипотез.

### 2. Причины невозможности взыскания и залоговое обеспечение
- Проанализируй основные причины невозможности взыскания (ликвидация заемщика, истечение срока исковой давности, невключение в реестр требований кредиторов).
- Опиши структуру и достаточность залогового обеспечения по проблемным договорам.

### 3. Крупнейшие кейсы задолженности
- Опиши крупнейшие случаи невозврата кредитов, указав их долю в общем объеме нереализованного взыскания.

### 4. Аналитические гипотезы для аудиторской проверки
Сформулируй ровно 2-3 аналитические гипотезы о системных недостатках в кредитном процессе, закрепив обязательные ракурсы:
- Гипотеза 1: Обязательно строится на причинах невозможности взыскания (ликвидация заемщика, истечение срока исковой давности, невключение в реестр требований) и роли залогового обеспечения.
- Гипотеза 2: Обязательно строится на концентрации проблемной задолженности (крупнейшие кейсы невозврата и их доля в общем объеме).
- Гипотеза 3: Обязательно строится на распределении по типам заемщиков и кредитным продуктам.
Гипотезы не могут опираться на одну и ту же метрику или вести к одному и тому же выводу о причинах — если два вывода совпадают по сути, один из них нужно заменить. Каждая гипотеза должна сопровождаться конкретными шагами проверки (не более 3 шагов на гипотезу, без указания вымышленных числовых порогов) и ожидаемым результатом (описанным качественно, без придуманных чисел)."""

    prompts["report_period_specific_ior"] = """Ты — эксперт-аналитик Службы внутреннего аудита.
Твоя задача — провести детальный анализ конкретного инцидента (или группы конкретных инцидентов) операционного риска (досье ИОР) и сформулировать аналитические гипотезы о его причинах.

Пиши максимально простым, понятным и человеческим языком без эмоций, преувеличений и сложного IT или узкоспециализированного корпоративного жаргона. Текст должен быть легким для чтения и понятным любому линейному аналитику или сотруднику.
- Полностью избегай оценочных и экспрессивных выражений.
- Излагай факты и предположения сухо, четко, структурированно, с использованием списков и ключевых метрик.

Придерживайся следующей структуры отчета:

### 1. Сведения об инциденте
- Кратко перечисли ключевые реквизиты инцидента: бизнес-идентификатор (incdnt_sid), статус, даты регистрации и совершения события.
- Опиши финансовые параметры: общая сумма последствий, прямые/косвенные потери, сумма возмещения.
- Укажи организационное подразделение (ТБ) и процесс, в котором зафиксировано событие.
- Важно: пиши этот раздел как чистое, сухое описание фактов БЕЗ каких-либо выводов, анализа, интерпретаций или гипотез.

### 2. Описание события и каналы обнаружения
- Приведи резюме сути инцидента на основе описания.
- Укажи источник и канал выявления события.

### 3. Выявленные особенности инцидента
- Проанализируй специфические факторы события (например, участие информационных систем, признаки авторегистрации, человеческий фактор).

### 4. Аналитические гипотезы для аудиторской проверки
Сформулируй ровно 2 аналитические гипотезы о причинах возникновения данного конкретного инцидента, закрепив обязательные ракурсы:
- Гипотеза 1: Обязательно строится на содержании описания этого конкретного события (фактические действия персонала, участие информационных систем, сбои АС).
- Гипотеза 2: Обязательно строится на организационном контексте (роль подразделения, ТБ, процесса, канала выявления).
Гипотезы не могут опираться на одну и ту же метрику или вести к одному и тому же выводу о причинах — если два вывода совпадают по сути, один из них нужно заменить. Каждая гипотеза должна сопровождаться конкретными шагами проверки (не более 3 шагов на гипотезу, без указания вымышленных числовых порогов) и ожидаемым результатом (описанным качественно, без придуманных чисел)."""

    prompts["ior_period_pao_sberbank"] = """Ты — эксперт-аналитик Службы внутреннего аудита.
Твоя задача — провести комплексный анализ инцидентов операционного риска по ПАО Сбербанк за указанный период и сформулировать аналитические гипотезы.

Пиши максимально простым, понятным и человеческим языком без эмоций, преувеличений и сложного IT или узкоспециализированного корпоративного жаргона. Текст должен быть легким для чтения и понятным любому линейному аналитику или сотруднику.
- Полностью избегай оценочных и экспрессивных выражений.
- Излагай факты и предположения сухо, четко, структурированно, с использованием списков и ключевых метрик.

Придерживайся следующей структуры отчета:

### 1. Общая сводка данных
- Кратко перечисли ключевые показатели: общее число зарегистрированных инцидентов, общая сумма всех последствий, сумма возмещений и чистые потери (Net Loss) по ПАО Сбербанк.
- Укажи распределение инцидентов по статусам.
- Важно: пиши этот раздел как чистое, сухое описание фактов БЕЗ каких-либо выводов, анализа, интерпретаций или гипотез.

### 2. Выявленные аномалии и динамика трендов
- Опиши динамику во времени (сезонность, тренды спада/роста, временные всплески). Сформулируй предположение о возможных причинах всплесков.
- Выдели распределение по территориальным банкам (ТБ) или процессам, перечислив лидеров по сумме потерь и количеству инцидентов.

### 3. Концентрация рисков и системные факторы
- Опиши концентрацию рисков: вклад топ-10 крупнейших инцидентов в общую сумму потерь, укажите их идентификаторы (EVE-XXXXXXX).
- Оцени долю авторегистрации инцидентов.

### 4. Аналитические гипотезы для аудиторской проверки
Сформулируй ровно 3 аналитические гипотезы, закрепив за каждой обязательный ракурс:
- Гипотеза 1: Обязательно строится на концентрации потерь (Топ-10 крупнейших инцидентов по ПАО Сбербанк с конкретными EVE-id).
- Гипотеза 2: Обязательно строится на географии/оргструктуре (распределение инцидентов по ТБ и процессам в рамках ПАО Сбербанк).
- Гипотеза 3: Обязательно строится на содержании описаний инцидентов (факты из анализа описаний инцидентов desc_summary), а не на общих долях.
Гипотезы не могут опираться на одну и ту же метрику или вести к одному и тому же выводу о причинах — если два вывода совпадают по сути, один из них нужно заменить. Каждая гипотеза должна сопровождаться конкретными шагами проверки (не более 3 шагов на гипотезу, без указания вымышленных числовых порогов) и ожидаемым результатом (описанным качественно, без придуманных чисел)."""

    critical_rules = """КРИТИЧЕСКИЕ ПРАВИЛА И ОГРАНИЧЕНИЯ (ПРОЧТИ В ПЕРВУЮ ОЧЕРЕДЬ):
1. ВАЖНО: Авторегистрация (авторег) — это нормальный штатный процесс регистрации событий, а не негативный фактор, проблема или уязвимость. СТРОГО ЗАПРЕЩЕНО критиковать авторегистрацию или интерпретировать долю авторегистрации как негативный фактор, проблему или недостаток.
2. ВАЖНО: Аналитические гипотезы не должны дублировать друг друга по смыслу и не должны сводиться к одному и тому же выводу. Каждая гипотеза обязана иметь свой собственный уникальный ракурс и вести к принципиально разным выводам о причинах произошедшего.

"""

    system_prompt = critical_rules + prompts.get(normalized_skill, prompts["ior_hypothesis"])
    
    # Append global rules for style, jargon-prevention, distinct hypotheses and specific figures/IDs (EVE-XXXXXXX)
    global_rules = """

КРИТИЧЕСКИЕ ПРАВИЛА ЯЗЫКА И ФОРМАТИРОВАНИЯ:
1. Пиши понятным, человеческим языком для аудитора. Избегай сложных IT-терминов, тяжеловесного жаргона и преувеличений.
2. СТРОГО ЗАПРЕЩЕНО использовать следующие слова и словосочетания:
   - "каскадные потери"
   - "каскадные последствия"
   - "каскадный сбой"
   - "экстремальная концентрация"
   - "синергетический эффект"
   - "недостаточная отказоустойчивость"
   - "технологический стек"
3. Вместо заумных фраз пиши проще: например, вместо "недостаточная отказоустойчивость" пиши "частые технические сбои", вместо "каскадные последствия" — "цепная реакция сбоев" или "последующие ошибки".
4. СТРОГО ЗАПРЕЩЕНО создавать разделы, которые не предусмотрены структурой шаблона выше. В отчёте должны быть только разрешенные разделы (обычно 1-4). Запрещено добавлять разделы до, между или после них (включая "Следующие шаги", "Финальный вывод", "Выводы", "Рекомендации", "Дополнительно", "Заключение", "Итоги", "Резюме", "Вывод"). Если требуется дать рекомендации по проверке гипотезы, они должны располагаться исключительно внутри описания шагов проверки в разделе 4.
5. Больше конкретики в деталях: не пиши общие фразы вроде "топ-10 составляет большую часть потерь". Указывай конкретные цифры и суммы (например, "на топ-10 инцидентов приходится 45.2 млн руб. потерь").
6. Обязательно ссылайся на конкретные идентификаторы событий (например, EVE-XXXXXXX) и пиши их точные суммы потерь при описании крупных инцидентов.
7. Не делай огромных пустых строк между абзацами.
8. Все цифры и проценты в разных частях одного отчёта должны быть взаимно непротиворечивы — если в сводке указано, что 100% записей имеют статус X, в последующих разделах нельзя утверждать, что тем же статусом X обладает другой процент записей, без явного пояснения, что речь идёт о другом временном срезе или другом поле.
9. ВАЖНО: Не пиши надуманных выводов про «отсутствие автоматизации», «отсутствие теневых режимов», «отсутствие автоматических проверок» и т.п., если этого прямо нет в текстах описаний инцидентов. Гипотезы должны основываться исключительно на реальных фактах из выгрузки и реальном содержании инцидентов, а не на общих шаблонных предположениях об ИТ-системах.
10. ВАЖНО: Если какая-либо метрика равна нулю, равна 100% по одному значению или иным образом вырождена (например, суммы потерь равны нулю, все записи имеют один и тот же статус), констатируй это как факт БЕЗ домысливания причинно-следственного объяснения этому факту (например, НЕЛЬЗЯ писать, что нулевые потери означают, что 'все ошибки урегулированы' — это не следует логически из данных). Просто укажи значение метрики и, если нужно, предложи это как область для отдельной проверки, а не как готовый вывод.
"""

    if normalized_skill == "ior_nonfinancial_consequences":
        global_rules += """11. ВАЖНО: Так как это выгрузка качественных (нефинансовых) последствий, в ней полностью отсутствуют финансовые убытки и возмещения. ТЕБЕ СТРОГО ЗАПРЕЩЕНО писать о финансовых потерях, возмещениях или убытках, а также СТРОГО ЗАПРЕЩЕНО упоминать о том, что финансовых потерь/убытков нет, или писать фразы вроде "финансовых потерь не зафиксировано", "потери равны 0", "нет данных о возмещениях". Вообще никак не касайся финансовой темы и цифр в рублях! Весь анализ должен строиться исключительно на качественных показателях: виды качественных потерь, класс влияния, организационная структура, процессы, связь с рисками информационных систем (ИБ/ИС) и поведенческими рисками.
"""

    system_prompt += global_rules

    if is_summarization_only:
        # Strip Section 4 outline from prompt if present to prevent any hallucination
        for sec4_marker in ("### 4. Аналитические гипотезы для аудиторской проверки", "### 4. Аналитические гипотезы"):
            if sec4_marker in system_prompt:
                system_prompt = system_prompt.split(sec4_marker)[0]
                break
        system_prompt += f"\n\nВАЖНО: Так как размер выборки мал (выборка содержит менее 20 записей, всего {len(df_analysis)} строк), НЕ ВКЛЮЧАЙ раздел '4. Аналитические гипотезы для аудиторской проверки' и СТРОГО ЗАПРЕЩЕНО формулировать гипотезы. Ограничься только описанием и детальной сводкой текущих {len(df_analysis)} инцидентов. Не придумывай никаких обобщений или гипотез."

    user_prompt = f"""Запрос пользователя: "{user_msg}"
Файл выгрузки: "{file_info.get('name', 'отчет.xlsx')}" ({file_info.get('size', '')}, строк: {len(df_analysis)})

{profile}

{desc_summary}

Сформулируй {"суммаризацию" if is_summarization_only else "гипотезу"} на основе этих данных. Пиши на русском языке, в professional стиле, доступно и понятно для аналитиков любого уровня."""

    try:
        from local_qwen import ask_local_qwen
        raw_response = await asyncio.to_thread(
            ask_local_qwen, [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=4096
        )
        
        narrative = str(raw_response).strip()
        if narrative.startswith("```"):
            lines = narrative.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            narrative = "\n".join(lines).strip()

        # Check if model generated duplicate deleted section to replace with code-injected
        header_pattern = re.compile(r'^(?:#+\s*|\d+\.\s*)(.+)$')
        lines = narrative.split('\n')
        cleaned_lines = []
        skip_section = False
        for line in lines:
            match = header_pattern.match(line.strip())
            if match:
                header_text = match.group(1).strip().lower()
                if any(x in header_text for x in ["информация об удаленных", "удаленные инциденты", "анализ удаленных"]):
                    skip_section = True
                    logger.warning("Found duplicate deleted section generated by model, stripping it.")
                else:
                    skip_section = False
            if not skip_section:
                cleaned_lines.append(line)
        narrative = "\n".join(cleaned_lines)

        # 1. Deduplicate sentences (Task 6)
        collapsed_narrative = collapse_cyclical_repetitions(narrative)
        collapsed_narrative, reps = collapse_repeated_sentences(collapsed_narrative)
        shrank_significantly = len(collapsed_narrative) < 0.8 * len(narrative) or reps > 0
        
        # 2. Deduplicate sections (Task 10)
        collapsed_narrative = collapse_repeated_sections(collapsed_narrative)
        
        # 3. Trim extra/blacklisted sections (Task 3 & 12)
        collapsed_narrative = trim_extra_sections(collapsed_narrative, is_summarization_only)
        
        # 4. LLM-as-judge validation (Task 5 & 16)
        forbidden_fields = []
        if "financial_consequences_ior" in running_skill:
            forbidden_fields = ["возмещения", "возмещение", "recovery", "возвраты"]

        validation = await validate_narrative(collapsed_narrative, forbidden_fields)
        has_violations = (
            validation.get("autoreg_criticized") or 
            validation.get("hypotheses_duplicate") or 
            validation.get("extra_sections") or 
            validation.get("missing_eve_ids_in_major_incidents") or
            validation.get("fabricated_thresholds") or
            validation.get("numbers_inconsistent") or
            validation.get("unfounded_inference_from_null_data") or
            validation.get("fields_not_in_dataset")
        )
        
        if has_violations or shrank_significantly:
            reasons = []
            if has_violations:
                reasons.append(f"найдены нарушения: {validation.get('details', '')}")
            if shrank_significantly:
                reasons.append("обнаружено зацикливание (repetition loop)")
                
            logger.warning(f"Narrative check failed ({'; '.join(reasons)}). Initiating retry...")
            
            retry_user_prompt = f"{user_prompt}\n\n"
            if shrank_significantly:
                retry_user_prompt += "ВНИМАНИЕ: предыдущая версия твоего ответа содержала критические повторения слов/предложений. Перепиши отчёт с нуля, избегая повторов и зацикливаний.\n"
            if has_violations:
                retry_user_prompt += f"ВНИМАНИЕ: предыдущая версия твоего ответа содержала следующие нарушения: {validation.get('details', '')}.\n"
            
            retry_user_prompt += f"Вот твой предыдущий ответ (частично очищенный):\n{collapsed_narrative}\n\nПерепиши отчёт с нуля, устранив эти проблемы, не потеряв остальные требования структуры и стиля."
            
            try:
                raw_response = await asyncio.to_thread(
                    ask_local_qwen, [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": retry_user_prompt}
                    ],
                    max_tokens=4096
                )
                retry_narrative = str(raw_response).strip()
                if retry_narrative.startswith("```"):
                    lines = retry_narrative.splitlines()
                    if lines[0].startswith("```"):
                        lines = lines[1:]
                    if lines and lines[-1].startswith("```"):
                        lines = lines[:-1]
                    retry_narrative = "\n".join(lines).strip()
                
                # Strip model deleted section again
                lines = retry_narrative.split('\n')
                cleaned_lines = []
                skip_section = False
                for line in lines:
                    match = header_pattern.match(line.strip())
                    if match:
                        header_text = match.group(1).strip().lower()
                        if any(x in header_text for x in ["информация об удаленных", "удаленные инциденты", "анализ удаленных"]):
                            skip_section = True
                        else:
                            skip_section = False
                    if not skip_section:
                        cleaned_lines.append(line)
                retry_narrative = "\n".join(cleaned_lines)
                
                retry_narrative = collapse_cyclical_repetitions(retry_narrative)
                collapsed_narrative, reps = collapse_repeated_sentences(retry_narrative)
                collapsed_narrative = collapse_repeated_sections(collapsed_narrative)
                collapsed_narrative = trim_extra_sections(collapsed_narrative, is_summarization_only)
                
                validation_retry = await validate_narrative(collapsed_narrative, forbidden_fields)
                has_violations_retry = (
                    validation_retry.get("autoreg_criticized") or 
                    validation_retry.get("hypotheses_duplicate") or 
                    validation_retry.get("extra_sections") or 
                    validation_retry.get("missing_eve_ids_in_major_incidents") or
                    validation_retry.get("fabricated_thresholds") or
                    validation_retry.get("numbers_inconsistent") or
                    validation_retry.get("unfounded_inference_from_null_data") or
                    validation_retry.get("fields_not_in_dataset")
                )
                if has_violations_retry:
                    logger.warning(f"Violations still found after retry: {validation_retry.get('details')}. Returning text as is.")
            except Exception as retry_err:
                logger.error(f"Error during retry generation: {retry_err}")
                
        narrative = collapsed_narrative
            
        # Post-process cleanup to ensure no Section 4 is generated if summarization only
        if is_summarization_only:
            for marker in ("### 4.", "4. Аналитические гипотезы", "Аналитические гипотезы"):
                if marker in narrative:
                    narrative = narrative.split(marker)[0].strip()
        
        # Inject information about deleted incidents and retrospective analysis before the hypotheses section
        combined_inject = deleted_text + retro_text
        if "### 4." in narrative:
            parts = narrative.split("### 4.", 1)
            narrative = parts[0] + combined_inject + "### 4." + parts[1]
        else:
            narrative = narrative + "\n" + combined_inject
            
        narrative = prefix + narrative
        
        # Append dynamic charts markdown if generated successfully
        if chart_file_id:
            narrative += "\n\n### Визуализация аналитики\n"
            narrative += f"\n![Динамика потерь и инцидентов](/api/files/{chart_file_id}/raw)\n"
            
        # Clean up huge empty lines (3 or more newlines) between paragraphs
        narrative = re.sub(r'\n{3,}', '\n\n', narrative)
        # Apply frontend markdown normalization
        narrative = normalize_markdown_for_frontend(narrative)
        return narrative.strip()
    except Exception as e:
        logger.exception(f"Error generating hypothesis: {e}")
        parts = [prefix, deleted_text, retro_text, "✅ Готово."]
        if file_info:
            parts.append(f"\n\n📁 Сформирован файл: **{file_info.get('name')}** (строк: {len(df_analysis)})")
        res = "".join(parts)
        res = re.sub(r'\n{3,}', '\n\n', res)
        res = normalize_markdown_for_frontend(res)
        return res.strip()
