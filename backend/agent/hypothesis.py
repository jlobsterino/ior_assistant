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

def calculate_advanced_stats(df: pd.DataFrame) -> dict:
    """
    Computes advanced operational risk metrics (Pareto concentration and statistical outliers).
    """
    stats = {}
    total_rows = len(df)
    
    money_cols = [c for c in df.columns if any(x in str(c).lower() for x in ("sum", "loss", "dmg", "rub", "amt", "потер", "убыт", "возмещ", "сумм"))]
    loss_cols = [c for c in money_cols if any(x in str(c).lower() for x in ("loss", "dmg", "потер", "убыт", "sum")) and "rec" not in str(c).lower()]
    
    if loss_cols:
        primary_loss = loss_cols[0]
        try:
            losses = pd.to_numeric(df[primary_loss], errors='coerce').fillna(0)
            total_loss = losses.sum()
            
            if total_loss > 0:
                # Sort losses descending
                sorted_losses = losses.sort_values(ascending=False)
                
                # Pareto 80/20 & concentration
                top_1_pct_count = max(1, int(total_rows * 0.01))
                top_5_pct_count = max(1, int(total_rows * 0.05))
                
                top_1_pct_loss = sorted_losses.head(top_1_pct_count).sum()
                top_5_pct_loss = sorted_losses.head(top_5_pct_count).sum()
                
                stats["top_1_pct_share"] = (top_1_pct_loss / total_loss) * 100
                stats["top_5_pct_share"] = (top_5_pct_loss / total_loss) * 100
                
                # Outlier threshold (mean + 3*std)
                mean_loss = losses.mean()
                std_loss = losses.std()
                outlier_threshold = mean_loss + 3 * std_loss
                outliers = losses[losses > outlier_threshold]
                
                stats["outliers_count"] = len(outliers)
                stats["outliers_share_of_total_loss"] = (outliers.sum() / total_loss) * 100 if len(outliers) > 0 else 0
                stats["outlier_threshold"] = outlier_threshold
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
        entry_candidates = [c for c in df.columns if any(x in str(c).lower() for x in ("entry", "ввод", "регистр"))]
        date_cols = [c for c in df.columns if any(x in str(c).lower() for x in ("dt", "date", "dttm", "ts", "дата", "время"))]
        
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
        temp_df[primary_date] = pd.to_datetime(temp_df[primary_date], errors='coerce', cache=True)
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
        
        periods = grouped['period_key'].astype(str).tolist()
        counts = grouped['count'].tolist()
        
        # Grid and borders
        ax1.grid(True, axis='both', color='#cbd5e1', linestyle='--', alpha=0.6)
        ax1.spines['top'].set_visible(False)
        ax1.spines['right'].set_visible(False)
        ax1.spines['left'].set_color('#94a3b8')
        ax1.spines['bottom'].set_color('#94a3b8')
        
        # Draw counts as blue line area chart (ax1)
        ax1.plot(periods, counts, color='#1d4ed8', marker='o', markersize=4, linewidth=2, label='Количество инцидентов')
        ax1.fill_between(periods, counts, color='#1d4ed8', alpha=0.08)
        ax1.set_xlabel(period_label, color='#0f172a', fontsize=10, labelpad=8)
        ax1.set_ylabel('Количество инцидентов', color='#1d4ed8', fontsize=10)
        ax1.tick_params(axis='y', labelcolor='#1d4ed8', colors='#0f172a')
        ax1.tick_params(axis='x', colors='#0f172a')
        
        # Rotated labels and strict downsampling to prevent overlapping (No more date clutter!)
        n_periods = len(periods)
        step = max(1, n_periods // 10)
        visible_ticks = list(range(0, n_periods, step))
        visible_labels = [periods[i] for i in visible_ticks]
        
        ax1.set_xticks(visible_ticks)
        ax1.set_xticklabels(visible_labels, rotation=30, ha='right', color='#0f172a', fontsize=9)
        
        # Plot losses and recoveries on secondary y-axis if we have non-zero values
        max_money = max(grouped['loss_sum'].max(), grouped['recovery_sum'].max())
        has_money = max_money > 0
        
        if has_money:
            if max_money >= 1_000_000_000:
                denom = 1_000_000_000
                denom_label = 'млрд ₽'
            elif max_money >= 1_000_000:
                denom = 1_000_000
                denom_label = 'млн ₽'
            else:
                denom = 1_000
                denom_label = 'тыс ₽'
                
            losses_scaled = (grouped['loss_sum'] / denom).tolist()
            recoveries_scaled = (grouped['recovery_sum'] / denom).tolist()
            
            ax2 = ax1.twinx()
            ax2.spines['top'].set_visible(False)
            ax2.spines['left'].set_visible(False)
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
    Generates a compact Markdown profile of the dataframe.
    If the dataframe is already small (<= 30 rows), returns the table directly.
    Otherwise, computes key statistical metrics, temporal distributions, categorical breakdowns, and outliers.
    """
    if df.empty:
        return "Таблица пуста."

    total_rows = len(df)
    
    # If the dataframe is already small and summarized, just return it as markdown
    if total_rows <= 30:
        return f"### Сводная таблица (размер: {total_rows} строк):\n\n" + df.to_markdown(index=False)

    lines = [f"### Профиль данных выгрузки (Всего строк: {total_rows}):\n"]

    # 1. Money/Loss summaries
    money_cols = [c for c in df.columns if any(x in str(c).lower() for x in ("sum", "loss", "dmg", "rub", "amt", "потер", "убыт", "возмещ", "сумм"))]
    loss_cols = [c for c in money_cols if any(x in str(c).lower() for x in ("loss", "dmg", "потер", "убыт", "sum")) and "rec" not in str(c).lower()]
    if not loss_cols and money_cols:
        loss_cols = [c for c in money_cols if not any(x in str(c).lower() for x in ("rec", "возмещ", "возврат"))]
    rec_cols = [c for c in money_cols if "rec" in str(c).lower() or "возмещ" in str(c).lower() or "возврат" in str(c).lower()]

    total_loss = 0
    total_rec = 0
    
    if loss_cols:
        primary_loss = loss_cols[0]
        try:
            total_loss = pd.to_numeric(df[primary_loss], errors='coerce').fillna(0).sum()
            lines.append(f"- **Общая сумма потерь**: {total_loss:,.2f} ₽ (по колонке '{primary_loss}')")
        except Exception as e:
            logger.warning(f"Error summing loss column: {e}")

    if rec_cols:
        primary_rec = rec_cols[0]
        try:
            total_rec = pd.to_numeric(df[primary_rec], errors='coerce').fillna(0).sum()
            lines.append(f"- **Общая сумма возмещений**: {total_rec:,.2f} ₽ (по колонке '{primary_rec}')")
        except Exception as e:
            logger.warning(f"Error summing recovery column: {e}")

    if total_loss and total_rec:
        lines.append(f"- **Чистые потери (Net Loss)**: {total_loss - total_rec:,.2f} ₽")

    # 2. Date/Temporal analysis
    entry_candidates = [c for c in df.columns if any(x in str(c).lower() for x in ("entry", "ввод", "регистр"))]
    date_cols = [c for c in df.columns if any(x in str(c).lower() for x in ("dt", "date", "dttm", "ts", "дата", "время"))]
    
    primary_date = None
    if entry_candidates:
        primary_date = entry_candidates[0]
    elif date_cols:
        primary_date = date_cols[0]

    if primary_date:
        try:
            temp_df = df.copy()
            # Cache datetime conversions to optimize performance for large datasets (e.g. 600,000 rows)
            temp_df[primary_date] = pd.to_datetime(temp_df[primary_date], errors='coerce', cache=True)
            temp_df = temp_df.dropna(subset=[primary_date])
            if not temp_df.empty:
                # Group by Year-Month
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
                    
                    if loss_cols:
                        m_loss_sum = pd.to_numeric(group[loss_cols[0]], errors='coerce').fillna(0).sum()
                        if total_loss:
                            m_loss_pct_str = f"{(m_loss_sum / total_loss) * 100:.1f}%"
                            
                    lines.append(f"| {month} | {m_count} | {m_pct:.1f}% | {m_loss_sum:,.2f} ₽ | {m_loss_pct_str} |")
        except Exception as e:
            logger.warning(f"Error in temporal profiling: {e}")

    # 3. Categorical analyses
    cat_cols = [c for c in df.columns if any(x in str(c).lower() for x in ("name", "type", "kind", "class", "lvl", "status", "tb", "block", "org", "proc", "блок", "процесс", "статус"))]
    cat_cols = [c for c in cat_cols if c not in date_cols and c not in money_cols and "id" not in str(c).lower() and "sid" not in str(c).lower()]
    
    if cat_cols:
        lines.append("\n#### Распределение по категориям:")
        for col in cat_cols[:4]: # Top 4 categorical columns
            try:
                grp = df.groupby(col)
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
                    
                    if loss_cols:
                        v_loss_sum = pd.to_numeric(group[loss_cols[0]], errors='coerce').fillna(0).sum()
                        if total_loss:
                            v_loss_pct_str = f"{(v_loss_sum / total_loss) * 100:.1f}%"
                            
                    lines.append(f"| {val} | {v_count} | {v_pct:.1f}% | {v_loss_sum:,.2f} ₽ | {v_loss_pct_str} |")
            except Exception as e:
                logger.warning(f"Error in categorical profiling for {col}: {e}")

    # 4. Outliers (Top 3 largest losses)
    id_cols = [c for c in df.columns if any(x in str(c).lower() for x in ("id", "sid", "key", "номер", "идентификатор"))]
    if id_cols and loss_cols:
        primary_id = id_cols[0]
        primary_loss = loss_cols[0]
        try:
            # Sort by loss sum descending
            temp_df = df.copy()
            temp_df[primary_loss] = pd.to_numeric(temp_df[primary_loss], errors='coerce').fillna(0)
            top_3 = temp_df.sort_values(by=primary_loss, ascending=False).head(3)
            lines.append("\n#### Топ-3 крупнейших инцидентов по сумме потерь:")
            for idx, row in top_3.iterrows():
                sid_val = row[primary_id]
                loss_val = row[primary_loss]
                pct_val = (loss_val / total_loss * 100) if total_loss else 0
                
                # Check status if available
                status_str = ""
                status_cols = [c for c in df.columns if "status" in str(c).lower() or "статус" in str(c).lower()]
                if status_cols:
                    status_str = f" (Статус: {row[status_cols[0]]})"
                    
                lines.append(f"- **{sid_val}**: {loss_val:,.2f} ₽ ({pct_val:.1f}% от всех потерь){status_str}")
        except Exception as e:
            logger.warning(f"Error calculating outliers: {e}")

    # 5. Advanced Concentration (Pareto & Outliers)
    advanced_stats = calculate_advanced_stats(df)
    if advanced_stats:
        lines.append("\n#### Концентрация рисков и выбросы:")
        if "top_1_pct_share" in advanced_stats:
            lines.append(f"- **Правило Парето (Топ-1% инцидентов)**: {advanced_stats['top_1_pct_share']:.1f}% от всей суммы потерь")
        if "top_5_pct_share" in advanced_stats:
            lines.append(f"- **Концентрация (Топ-5% инцидентов)**: {advanced_stats['top_5_pct_share']:.1f}% от всей суммы потерь")
        if "outliers_count" in advanced_stats:
            lines.append(f"- **Статистические выбросы (outliers)**: {advanced_stats['outliers_count']} инцидентов (порог {advanced_stats['outlier_threshold']:,.2f} ₽) составляют {advanced_stats['outliers_share_of_total_loss']:.1f}% всех потерь")

    # 6. Autoregistration
    autoreg_cols = [c for c in df.columns if "autoreg" in str(c).lower() or "авторег" in str(c).lower()]
    if autoreg_cols:
        col = autoreg_cols[0]
        try:
            auto_cnt = df[df[col].astype(str).str.upper().str.startswith('Y') | (df[col] == True)].shape[0]
            auto_pct = (auto_cnt / total_rows) * 100
            lines.append(f"\n- **Авторегистрация**: {auto_cnt} инцидентов ({auto_pct:.1f}% от всей выгрузки)")
        except Exception as e:
            logger.warning(f"Error calculating autoregistration: {e}")

    return "\n".join(lines)


async def generate_hypothesis_narrative(user_msg: str, df: pd.DataFrame, file_info: dict, session_id: str) -> str:
    """
    Generates a natural language narrative (with hypothesis / insights) based on the dataframe profile and optional plot.
    """
    profile = profile_dataframe(df)
    
    # Check if the query asks for a hypothesis / dynamics / trends
    low_msg = user_msg.lower()
    is_hypothesis_query = any(x in low_msg for x in ("гипотез", "аномал", "динамик", "анализ", "тренд"))
    is_dynamics_query = any(x in low_msg for x in ("динамик", "график", "тренд", "изменен", "рост", "спад"))

    chart_file_id = None
    if is_dynamics_query or len(df) > 30:
        # Generate charts asynchronously in worker threads to prevent blocking the event loop
        chart_file_id = await asyncio.to_thread(generate_dynamics_chart, df, session_id)

    llm = get_llm()
    
    # Deep, expert-auditor grade prompt formulation
    system_prompt = """Ты — ведущий эксперт-аналитик Службы внутреннего аудита и управления операционными рисками Сбербанка.
Твоя задача — провести глубокий экспресс-анализ представленного профиля данных и сформулировать сильные, обоснованные аналитические гипотезы и теории о скрытых закономерностях в инцидентах операционного риска.

Аналитическая гипотеза должна быть не просто описанием фактов ("в марте выросли потери"), а содержать экспертное предположение о причинно-следственных связях (теорию возникновения рисков).

Придерживайся следующей структуры отчета:

### 1. Выявленные аномалии и динамика трендов
- Проанализируй динамику во времени (скачки, сезонность, тренды спада/роста). Сформулируй теорию, чем обусловлен временной всплеск.
- Если есть распределение по ТБ (территориальным банкам) или процессам, выяви лидеров по потерям и частоте.

### 2. Концентрация рисков и системные факторы
- Оцени концентрацию (правило Парето): какой процент инцидентов генерирует основную массу потерь.
- Укажи на наличие крупных статистических выбросов (outliers) и их влияние на общий профиль.
- Если доступно, проанализируй долю авторегистрации (высокий процент ручного ввода может сигнализировать о скрытых операционных инцидентах или задержках).

### 3. Аналитические гипотезы для аудиторской проверки
- Сформулируй 2-3 гипотезы (теории) для последующей проверки аудиторами. Например:
  * "Гипотеза о сбое бизнес-процесса X в период Y..."
  * "Гипотеза о системной уязвимости контроля в ТБ Z..."

Избегай общих фраз и «воды». Оперируй исключительно реальными цифрами, процентами и названиями из предоставленного профиля данных. Если какие-то метрики (например, возмещения) отсутствуют или равны нулю, не упоминай их.
"""

    if not is_hypothesis_query:
        system_prompt += "\nСформулируй краткую гипотезу или интересное наблюдение на основе данных выгрузки в конце твоего отчета, чтобы помочь аудитору обратить внимание на ключевые особенности. Начни с общей информации о выгрузке (например, файл готов, столько-то строк)."
    
    user_prompt = f"""Запрос пользователя: "{user_msg}"
Файл выгрузки: "{file_info.get('name', 'отчет.xlsx')}" ({file_info.get('size', '')}, строк: {len(df)})

{profile}

Сформулируй гипотезу на основе этих данных. Пиши на русском языке, в профессиональном стиле."""

    try:
        raw_response = await asyncio.to_thread(
            llm.invoke, [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]
        )
        
        narrative = str(raw_response)
        
        # Append dynamic charts markdown if generated successfully
        if chart_file_id:
            narrative += "\n\n### Визуализация аналитики\n"
            narrative += f"\n![Динамика потерь и инцидентов](/api/files/{chart_file_id}/raw)\n"
            
        return narrative
    except Exception as e:
        logger.exception(f"Error generating hypothesis: {e}")
        # fallback narrative
        parts = ["✅ Готово."]
        if file_info:
            parts.append(f"\n\n📁 Сформирован файл: **{file_info.get('name')}** (строк: {len(df)})")
        return "".join(parts)
