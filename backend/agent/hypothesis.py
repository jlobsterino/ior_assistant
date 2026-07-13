import os
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
        "сумма последствий, ₽"
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


def get_total_and_direct_loss(df: pd.DataFrame) -> tuple[float, float]:
    total_loss = 0.0
    direct_loss = 0.0
    
    col_map = {str(c).lower().strip().replace("–", "-"): c for c in df.columns}
    
    # 1. Total loss column candidates
    total_cols = [
        "incdnt_sum", 
        "общая сумма всех последствий (руб.)", 
        "общая сумма последствий (руб.)", 
        "сумма последствий, ₽"
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
            
        money_cols = [c for c in df.columns if any(x in str(c).lower() for x in ("sum", "loss", "dmg", "rub", "amt", "потер", "убыт", "возмещ", "сумм"))]
        loss_cols = [c for c in money_cols if any(x in str(c).lower() for x in ("loss", "dmg", "потер", "убыт", "sum")) and "rec" not in str(c).lower()]
        if not loss_cols and money_cols:
            loss_cols = [c for c in money_cols if not any(x in str(c).lower() for x in ("rec", "возмещ", "возврат"))]
        recovery_cols = [c for c in money_cols if any(x in str(c).lower() for x in ("rec", "возмещ"))]
        
        primary_loss = loss_cols[0] if loss_cols else None
        primary_recovery = recovery_cols[0] if recovery_cols else None
        
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
        
        # Set max ticks limit for horizontal x-axis readability
        ax1.xaxis.set_major_locator(plt.MaxNLocator(12))
        
        # Bar 1: incident counts (primary y-axis, clean sky blue color)
        bar_width = 0.35
        x_positions = range(len(periods))
        ax1.bar(x_positions, counts, width=bar_width, color='#3b82f6', edgecolor='#2563eb', alpha=0.85, label='Число инцидентов')
        ax1.set_ylabel('Число инцидентов', color='#1e3a8a', fontsize=10)
        ax1.tick_params(axis='y', labelcolor='#1e3a8a', colors='#0f172a')
        ax1.set_xticks(x_positions)
        ax1.set_xticklabels(periods, rotation=15, ha='right', fontsize=8, color='#0f172a')
        
        # Hide borders
        for spine in ax1.spines.values():
            spine.set_edgecolor('#cbd5e1')
            
        # Optional lines on secondary axis for losses/recoveries
        if primary_loss and grouped['loss_sum'].sum() > 0:
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
            ax2.plot(periods, losses_scaled, color='#dc2626', marker='s', markersize=4, linewidth=2, label=f'Сумма потерь ({denom_label})')
            
            # Line 3: recoveries (green color)
            if primary_recovery and grouped['recovery_sum'].sum() > 0:
                ax2.plot(periods, recoveries_scaled, color='#15803d', marker='^', markersize=4, linewidth=1.8, linestyle='--', label=f'Сумма возмещений ({denom_label})')
                
            ax2.set_ylabel(f'Объем средств ({denom_label})', color='#dc2626', fontsize=10)
            ax2.tick_params(axis='y', labelcolor='#dc2626', colors='#0f172a')
            
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


def profile_dataframe(df: pd.DataFrame) -> str:
    """
    Generates a Markdown profile of the dataframe.
    """
    if df.empty:
        return "Таблица пуста."

    total_rows = len(df)
    lines = [f"### Профиль данных выгрузки (Всего строк для анализа: {total_rows}):\n"]

    df_copy = df.copy()
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
    money_cols = [c for c in df_copy.columns if any(x in str(c).lower() for x in ("sum", "loss", "dmg", "rub", "amt", "потер", "убыт", "возмещ", "сумм"))]
    loss_cols = [c for c in money_cols if any(x in str(c).lower() for x in ("loss", "dmg", "потер", "убыт", "sum", "сумм")) and not any(r in str(c).lower() for r in ("rec", "возмещ", "возврат"))]
    if not loss_cols and money_cols:
        loss_cols = [c for c in money_cols if not any(x in str(c).lower() for x in ("rec", "возмещ", "возврат"))]
    rec_cols = [c for c in money_cols if "rec" in str(c).lower() or "возмещ" in str(c).lower() or "возврат" in str(c).lower()]

    # Locate specific key columns for side-by-side comparison
    incdnt_sum_col = None
    recovery_col = None
    for col in df_copy.columns:
        col_lower = str(col).lower()
        if any(x in col_lower for x in ("incdnt_sum", "общая сумма", "сумма последствий")) and not any(x in col_lower for x in ("rec", "возмещ", "возврат")):
            incdnt_sum_col = col
        elif any(x in col_lower for x in ("recovery", "возмещ", "возврат")):
            recovery_col = col

    if incdnt_sum_col is None and loss_cols:
        for c in loss_cols:
            if "incdnt_sum" in str(c).lower() or "общая сумма" in str(c).lower():
                incdnt_sum_col = c
                break
        if incdnt_sum_col is None:
            incdnt_sum_col = loss_cols[0]

    if recovery_col is None and rec_cols:
        recovery_col = rec_cols[0]

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
    Uses async local Qwen analysis for summaries.
    """
    import re
    # Locate column names
    loss_cols = ["incdnt_sum", "Общая сумма всех последствий (руб.)", "Общая сумма последствий (руб.)"]
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
    
    if not primary_loss:
        return ""
        
    df_with_loss = df.copy()
    df_with_loss[primary_loss] = _to_numeric_clean(df_with_loss[primary_loss])
    df_with_loss = df_with_loss.dropna(subset=[primary_loss])
    
    descriptions = []
    
    # Extract top 30 largest incidents by loss
    top_30_df = df_with_loss.sort_values(by=primary_loss, ascending=False).head(30)
    for _, row in top_30_df.iterrows():
        sid = row[primary_id] if primary_id else "—"
        val = row[primary_loss]
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
            descriptions.append(f"Инцидент {sid} (Сумма потерь: {format_loss(val)}): {desc}")
            
    if not descriptions:
        return ""
        
    # Split into exactly 2 batches
    batch_size = (len(descriptions) + 1) // 2
    batch_1 = descriptions[:batch_size]
    batch_2 = descriptions[batch_size:]
    
    from local_qwen import ask_local_qwen
    
    async def analyze_batch(batch_items, batch_num):
        if not batch_items:
            return "Нет данных по этому пакету."
        prompt = f"Ниже представлены описания крупных инцидентов операционного риска (Пакет {batch_num}). Выдели ключевые технические и системные проблемы:\n" + "\n".join(batch_items)
        try:
            res = await asyncio.to_thread(
                ask_local_qwen, [
                    {"role": "system", "content": "Ты — аналитик Службы внутреннего аудита. Подготовь краткое структурированное резюме технических и системных причин инцидентов в 1-2 абзацах, используя строго нейтральный деловой язык."},
                    {"role": "user", "content": prompt}
                ],
                max_tokens=1024
            )
            return str(res)
        except Exception as e:
            logger.error(f"Error analyzing descriptions batch {batch_num}: {e}")
            return f"Ошибка при анализе пакета {batch_num}: {e}"
            
    summary_1, summary_2 = await asyncio.gather(
        analyze_batch(batch_1, 1),
        analyze_batch(batch_2, 2)
    )
    
    combined = (
        f"### Результаты анализа описаний инцидентов (Пакет 1):\n{summary_1}\n\n"
        f"### Результаты анализа описаний инцидентов (Пакет 2):\n{summary_2}\n"
    )
    return combined


async def generate_hypothesis_narrative(user_msg: str, df: pd.DataFrame, file_info: dict, session_id: str) -> str:
    """
    Generates a natural language narrative (with hypothesis / insights) based on the dataframe profile and optional plot.
    """
    if len(df) < 20:
        status_col = next((c for c in df.columns if str(c).lower() in ("incdnt_status_name", "статус события", "статус")), None)
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

        total_loss, direct_loss = get_total_and_direct_loss(df)

        recovery_loss = 0.0
        rec_cols_list = ["recovery", "Сумма возмещений", "возмещ", "recovery_rub_amt", "recovery_rub_amt_aggr", "Сумма возмещения (агрегат)", "Возмещение – итого по инциденту (руб.)"]
        primary_rec = next((c for c in rec_cols_list if c in df.columns), None)
        if not primary_rec:
            money_cols = [c for c in df.columns if any(x in str(c).lower() for x in ("sum", "loss", "dmg", "rub", "amt", "потер", "убыт", "возмещ", "сумм"))]
            rec_cols_fallback = [c for c in money_cols if any(x in str(c).lower() for x in ("rec", "возмещ", "возврат"))]
            if rec_cols_fallback:
                primary_rec = rec_cols_fallback[0]
        if primary_rec:
            recovery_loss = _to_numeric_clean(df[primary_rec]).sum()

        net_loss = max(0.0, total_loss - recovery_loss)

        report_lines = []
        report_lines.append("### Общая информация о выгрузке:")
        report_lines.append(f"Выгрузка успешно сформирована. Файл: **{file_info.get('name', 'отчет.xlsx')}** ({file_info.get('size', 'размер неизвестен')}).")
        report_lines.append(f"- **Всего инцидентов в файле**: {len(df)}")
        report_lines.append(f"- **Распределение по статусам**:")
        report_lines.append(f"  • **Группа 1: Утверждение**: {group_counts['Группа 1: Утверждение']}")
        report_lines.append(f"  • **Группа 2: Черновик/Исследование**: {group_counts['Группа 2: Черновик/Исследование']}")
        report_lines.append(f"  • **Группа 3: Удален**: {group_counts['Группа 3: Удален']}")
        report_lines.append("")
        report_lines.append("### Финансовые показатели:")
        report_lines.append(f"- **Общие потери**: {format_loss(total_loss)}")
        report_lines.append(f"- **Прямые потери**: {format_loss(direct_loss)}")
        report_lines.append(f"- **Сумма возмещений**: {format_loss(recovery_loss)}")
        report_lines.append(f"- **Чистые потери (Net Loss)**: {format_loss(net_loss)}")
        report_lines.append("")

        id_col = next((c for c in df.columns if any(x in str(c).lower() for x in ("sid", "идентификатор", "key", "id"))), None)
        loss_cols = ["incdnt_sum", "Общая сумма всех последствий (руб.)", "Общая сумма последствий (руб.)", "Сумма последствий, ₽"]
        primary_loss_col = next((c for c in loss_cols if c in df.columns), None)
        if not primary_loss_col:
            loss_cols_fallback = [c for c in df.columns if any(x in str(c).lower() for x in ("sum", "loss", "dmg", "rub", "amt", "потер", "убыт", "сумм")) and not any(r in str(c).lower() for r in ("rec", "возмещ", "возврат"))]
            if loss_cols_fallback:
                primary_loss_col = loss_cols_fallback[0]

        report_lines.append("### Инциденты, требующие внимания:")
        if primary_loss_col and id_col and not df.empty:
            df_sorted = df.copy()
            df_sorted["numeric_loss"] = _to_numeric_clean(df_sorted[primary_loss_col])
            df_sorted = df_sorted.sort_values(by="numeric_loss", ascending=False)

            has_outliers = False
            top_incidents = df_sorted.head(3)
            for _, row in top_incidents.iterrows():
                inc_id = row[id_col]
                inc_loss = row["numeric_loss"]
                if inc_loss > 0:
                    has_outliers = True
                    status_val = row[status_col] if (status_col and status_col in df.columns) else "Неизвестно"
                    desc_col = next((c for c in df.columns if any(x in str(c).lower() for x in ("descr", "описание", "summary"))), None)
                    desc_val = f": *{row[desc_col]}*" if desc_col and pd.notna(row[desc_col]) else ""
                    if len(desc_val) > 100:
                        desc_val = desc_val[:97] + "..."
                    report_lines.append(f"- **Инцидент {inc_id}** (Статус: *{status_val}*): потери составляют **{format_loss(inc_loss)}**{desc_val}")

            if not has_outliers:
                report_lines.append("- В данной выгрузке нет инцидентов с ненулевыми финансовыми потерями.")
        else:
            report_lines.append("- Детальная информация по конкретным инцидентам не может быть извлечена (отсутствуют колонки идентификатора или сумм).")

        report_lines.append("")
        report_lines.append("*(Аналитические гипотезы не формировались, так как размер выборки составляет менее 20 инцидентов, что является статистически недостаточным для глубокого анализа трендов и закономерностей).*")

        return "\n".join(report_lines)

    # 5. Агрегация по статусам на старте
    status_col = next((c for c in df.columns if str(c).lower() in ("incdnt_status_name", "статус события", "статус")), None)
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
                
    # 6. Фильтрация для анализа: только группа Утверждение
    if status_col:
        df_analysis = df[df[status_col].astype(str).str.strip().str.lower().isin(["утверждение", "утверждён", "утвержден"])].copy()
    else:
        df_analysis = df.copy()

    # 2. Расчет потерь
    total_loss, direct_loss = get_total_and_direct_loss(df_analysis)

    # Расчет возмещений для группы Утвержден
    recovery_loss = 0.0
    col_map = {str(c).lower().strip().replace("–", "-"): c for c in df_analysis.columns}
    rec_cols_list = [
        "recovery", 
        "сумма возмещений", 
        "возмещ", 
        "recovery_rub_amt", 
        "recovery_rub_amt_aggr", 
        "сумма возмещения (агрегатор)", 
        "возмещение - итого по инциденту (руб.)"
    ]
    primary_rec_analysis = None
    for c_cand in rec_cols_list:
        norm_cand = c_cand.lower().strip().replace("–", "-")
        if norm_cand in col_map:
            primary_rec_analysis = col_map[norm_cand]
            break
            
    if not primary_rec_analysis:
        money_cols = [c for c in df_analysis.columns if any(x in str(c).lower() for x in ("sum", "loss", "dmg", "rub", "amt", "потер", "убыт", "возмещ", "сумм"))]
        rec_cols_fallback = [c for c in money_cols if any(x in str(c).lower() for x in ("rec", "возмещ", "возврат"))]
        if rec_cols_fallback:
            primary_rec_analysis = rec_cols_fallback[0]
            
    if primary_rec_analysis:
        recovery_loss = _to_numeric_clean(df_analysis[primary_rec_analysis]).sum()

    net_loss = max(0.0, total_loss - recovery_loss)

    # 3. Формирование префикса
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

    # 8. Ограничение по удаленным: по ним собираются только базовые вещи
    deleted_count = 0
    deleted_loss = 0.0
    deleted_rec = 0.0
    
    if status_col:
        df_deleted = df[df[status_col].astype(str).str.strip().str.lower().isin(["удалён", "удален"])].copy()
        deleted_count = len(df_deleted)
        
        # Calculate sum of losses for deleted
        loss_cols = ["incdnt_sum", "Общая сумма всех последствий (руб.)", "Общая сумма последствий (руб.)"]
        primary_loss = next((c for c in loss_cols if c in df.columns), None)
        if not primary_loss:
            money_cols = [c for c in df.columns if any(x in str(c).lower() for x in ("sum", "loss", "dmg", "rub", "amt", "потер", "убыт", "возмещ", "сумм"))]
            loss_cols_fallback = [c for c in money_cols if any(x in str(c).lower() for x in ("loss", "dmg", "потер", "убыт", "sum", "сумм")) and not any(r in str(c).lower() for r in ("rec", "возмещ", "возврат"))]
            if loss_cols_fallback:
                primary_loss = loss_cols_fallback[0]
                
        rec_cols_list = ["recovery", "Сумма возмещений", "возмещ"]
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

    # 9. Ретроспективный анализ профилей риска (отключен по требованию пользователя)
    retro_text = ""

    profile = profile_dataframe(df_analysis)
    
    # Check if the query asks for a hypothesis / dynamics / trends
    low_msg = user_msg.lower()
    is_hypothesis_query = any(x in low_msg for x in ("гипотез", "аномал", "динамик", "анализ", "тренд"))
    is_dynamics_query = any(x in low_msg for x in ("динамик", "график", "тренд", "изменен", "рост", "спад"))

    chart_file_id = None
    if is_dynamics_query or len(df_analysis) > 30:
        # Generate charts asynchronously in worker threads to prevent blocking the event loop
        chart_file_id = await asyncio.to_thread(generate_dynamics_chart, df_analysis, session_id)

    llm = get_llm()
    
    # Deep, expert-auditor grade prompt formulation (Requirement 12: simpler and clearer narrative style)
    system_prompt = """Ты — эксперт-аналитик Службы внутреннего аудита.
Твоя задача — провести анализ представленного профиля данных инцидентов операционного риска и сформулировать аналитические гипотезы о возможных причинах этих инцидентов.

Пиши максимально простым, понятным и человеческим языком без эмоций, преувеличений и сложного IT или узкоспециализированного корпоративного жаргона. Текст должен быть легким для чтения и понятным любому линейному аналитику или сотруднику.
- Полностью избегай оценочных и экспрессивных выражений (например, "экстремальная концентрация", "катастрофический сбой", "немедленный аудит", "это означает, что").
- Избегай перегруженных сложных терминов. Вместо тяжелого IT-жаргона используй простые аналоги (например, вместо "недостаточная отказоустойчивость" пиши "частые сбои в работе систем", вместо "каскадные последствия" — "цепная реакция сбоев" или "последующие ошибки", вместо "синергетический эффект" — "взаимное влияние проблем"). Обычные технические термины вроде "мониторинг систем" или "автоматизация контроля" использовать можно.
- Излагай факты и предположения сухо, четко, структурированно, с использованием списков и ключевых метрик.

Придерживайся следующей структуры отчета:

### 1. Общая сводка данных
- Кратко перечисли ключевые показатели: общее число инцидентов, общая сумма всех последствий, сумма возмещений и чистые потери по группе Утверждение.
- Укажи, какая самая частая причина инцидентов (основная причина / тип события).
- Опиши общую динамику регистрации во времени (в какие периоды/месяцы зафиксировано больше всего инцидентов, когда наблюдался спад).
- Важно: пиши этот раздел как чистое, сухое описание фактов БЕЗ каких-либо выводов, анализа, интерпретаций или гипотез.

### 2. Выявленные аномалии и динамика трендов
- Опиши динамику во времени (сезонность, тренды спада/роста, временные всплески). Сформулируй предположение о возможных причинах временного всплеска.
- Выдели распределение по территориальным банкам (ТБ) или процессам, перечислив лидеров по сумме потерь и количеству инцидентов.

### 3. Концентрация рисков и системные факторы
- Опиши концентрацию потерь: укажи суммарный вклад Топ-10 крупнейших инцидентов (их точную сумму и процент от общего объема потерь).
- Укажи конкретные идентификаторы событий (например, EVE-XXXXXXX) из топа крупнейших инцидентов и проанализируй их вклад.
- Поле "Тип события - уровень 1" (incdnt_type_lvl_1_name) транслируй в отчет как "Основная причина".
- Оцени долю авторегистрации (процент авторегистрированных инцидентов).
- Важно: НЕ перегружай отчет бесконечным перечислением процентов концентрации и долей (например, вклад Топ-10/Топ-5 в процентах, доля авторегистрации). Упомяни эти доли кратко один раз, но не строй весь отчет и гипотезы вокруг них.

### 4. Аналитические гипотезы для аудиторской проверки
- Сформулируй 2-3 гипотезы (теории) по РАЗЛИЧНЫМ направлениям на основе предоставленных данных (например, одна гипотеза о конкретном техническом сбое или уязвимости в системах на основе топ-30 инцидентов, другая — об операционных процессах или ошибках ввода, третья — о специфике контроля).
- Важно: гипотезы должны быть разнообразными. Не зацикливайся на одной и той же теме (например, только ручном вводе или процентах концентрации) во всех гипотезах.
- Каждая гипотеза должна сопровождаться конкретными шагами проверки и ожидаемым результатом.

Оперируй исключительно реальными цифрами, процентами и названиями из предоставленного профиля данных. Если какие-то метрики (например, возмещения) отсутствуют или равны нулю, не упоминай их.
ОБЯЗАТЕЛЬНО в самом начале или при первом упоминании финансовых показателей укажи точные цифры общей суммы всех последствий (incdnt_sum), суммы возмещений (recovery) и чистые потери (Net Loss) по группе Утверждение.
Если все финансовые показатели равны нулю или отсутствуют, сфокусируйся на анализе количества инцидентов, динамике регистраций, оргструктуре/ТБ и авторегистрации.
Пиши строго на русском языке. Все заголовки разделов пиши ровно так, как они указаны выше.
"""

    if not is_hypothesis_query:
        system_prompt += "\nСформулируй краткую гипотезу или наблюдение на основе данных выгрузки в конце отчета. Начни с общей информации о выгрузке (файл готов, количество строк). ОБЯЗАТЕЛЬНО в самом начале укажи общую сумму всех последствий (incdnt_sum), сумму возмещений (recovery) и чистые потери (Net Loss) по группе Утверждение."
    
    # Batch extraction and Qwen analysis of incident descriptions (Requirement 10)
    desc_summary = await analyze_incident_descriptions(df_analysis)
    
    user_prompt = f"""Запрос пользователя: "{user_msg}"
Файл выгрузки: "{file_info.get('name', 'отчет.xlsx')}" ({file_info.get('size', '')}, строк: {len(df_analysis)})

{profile}

{desc_summary}

Сформулируй гипотезу на основе этих данных. Пиши на русском языке, в профессиональном стиле, доступно и понятно для аналитиков любого уровня."""

    try:
        from local_qwen import ask_local_qwen
        raw_response = await asyncio.to_thread(
            ask_local_qwen, [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            max_tokens=4096
        )
        
        narrative = str(raw_response)
        
        # Inject information about deleted incidents and retrospective analysis before the hypotheses section (Requirements 8 & 13)
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
            
        return narrative
    except Exception as e:
        logger.exception(f"Error generating hypothesis: {e}")
        # fallback narrative
        parts = [prefix, deleted_text, retro_text, "✅ Готово."]
        if file_info:
            parts.append(f"\n\n📁 Сформирован файл: **{file_info.get('name')}** (строк: {len(df_analysis)})")
        return "".join(parts)
