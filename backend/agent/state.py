"""
SessionState - runtime-состояние одной чат-сессии.

Хранит:
  • dataframes registry: df_1, df_2, ... - результат tool-вызовов
    (pandas DF live in-memory, сериализуем только metadata)
  • files registry: file_1, file_2, ... - сгенерированные xlsx/csv
  • messages history - последние N user+assistant turns
  • active_focus - короткое описание контекста ("СЭБ Q1 2025")

Используется Planner'ом (передаётся в LLM как snapshot) и Executor'ом
(сюда регистрируются результаты шагов).
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Per-process registry (1 backend = 1 процесс). Если будут multiple-workers,
# нужно вынести в Redis. Пока in-memory.
_STATES: dict[str, SessionState] = {}
_STATES_LOCK = threading.Lock()


@dataclass
class DataframeMeta:
    """Метадата зарегистрированного датафрейма (то что видит LLM)."""
    df_id: str
    description: str            # краткое описание "что в этом df'е"
    rows: int
    columns: list[str]
    created_by: str             # имя tool'а который создал
    created_at: datetime = field(default_factory=datetime.now)
    sample: Optional[list[list]] = None   # первые 3 строки для preview

    def to_llm_snapshot(self) -> dict:
        """Компактный вид для prompt'а LLM."""
        return {
            "df_id": self.df_id,
            "desc": self.description,
            "rows": self.rows,
            "columns": self.columns[:30],    # обрезаем если очень много колонок
            "n_columns_total": len(self.columns),
        }


@dataclass
class FileMeta:
    file_id: str
    name: str
    path: str
    size_bytes: int
    mime_type: str
    created_by_step: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)

    def to_llm_snapshot(self) -> dict:
        return {
            "file_id": self.file_id,
            "name": self.name,
            "size": _format_bytes(self.size_bytes),
        }


@dataclass
class SessionState:
    """Состояние одной чат-сессии (между tool-вызовами и turn'ами)."""
    session_id: str
    dataframes: dict[str, Any] = field(default_factory=dict)       # df_id -> pandas.DataFrame
    dataframe_meta: dict[str, DataframeMeta] = field(default_factory=dict)
    files: dict[str, FileMeta] = field(default_factory=dict)
    active_focus: str = ""                                         # «СЭБ Q1 2025»
    history: list[dict] = field(default_factory=list)              # [{role, content, ...}]
    _df_counter: int = 0
    _file_counter: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def next_df_id(self) -> str:
        with self._lock:
            self._df_counter += 1
            return f"df_{self._df_counter}"

    def next_file_id(self) -> str:
        with self._lock:
            self._file_counter += 1
            return f"file_{self._file_counter}"

    def register_dataframe(self, df, description: str, created_by: str,
                           df_id: Optional[str] = None) -> DataframeMeta:
        """Сохранить df в registry, вернуть metadata."""
        df_id = df_id or self.next_df_id()
        # Принудительно приводим колонки к нижнему регистру для избежания проблем с регистром на Hive/Spark
        try:
            df.columns = [str(c).lower() for c in df.columns]
            # Convert ID columns with large integers to string to avoid float64 precision loss in Pandas/Excel/JS
            for col in df.columns:
                col_lower = str(col).lower()
                if any(x in col_lower for x in ("id", "sid", "key", "номер", "идентификатор")):
                    if any(x in col_lower for x in ("cnt", "sum", "amt", "val", "кол", "кол-во", "сумма")):
                        continue
                    # Safely convert to string preserving full bigint precision (no float conversion)
                    df[col] = df[col].fillna("").astype(str).apply(lambda s: s.split('.')[0] if '.' in s else s)
        except Exception as e:
            logger.warning("[State] Failed to normalize columns or convert IDs: %s", e)
        # Сэмпл: первые 3 строки в list-of-lists
        try:
            sample = df.head(3).astype(str).values.tolist()
        except Exception:
            sample = None
        meta = DataframeMeta(
            df_id=df_id,
            description=description,
            rows=len(df),
            columns=list(df.columns),
            created_by=created_by,
            sample=sample,
        )
        with self._lock:
            self.dataframes[df_id] = df
            self.dataframe_meta[df_id] = meta
        logger.info("[State] %s: registered %s (%d rows x %d cols) by %s",
                    self.session_id, df_id, meta.rows, len(meta.columns), created_by)
        return meta

    def register_file(self, *, name: str, path: str, size_bytes: int,
                      mime_type: str = "application/octet-stream",
                      created_by_step: Optional[str] = None) -> FileMeta:
        file_id = self.next_file_id()
        meta = FileMeta(file_id=file_id, name=name, path=path,
                        size_bytes=size_bytes, mime_type=mime_type,
                        created_by_step=created_by_step)
        with self._lock:
            self.files[file_id] = meta
        return meta

    def get_df(self, df_id: str):
        """Достать pandas DF по id."""
        if df_id not in self.dataframes:
            raise KeyError(f"Dataframe {df_id} не найден в session state. "
                           f"Доступны: {list(self.dataframes.keys())}")
        return self.dataframes[df_id]

    def llm_snapshot(self) -> dict:
        """Компактный snapshot для prompt'а LLM (Planner/Reflector)."""
        return {
            "session_id": self.session_id,
            "active_focus": self.active_focus,
            "dataframes": [m.to_llm_snapshot()
                           for m in list(self.dataframe_meta.values())[-10:]],
            "files": [m.to_llm_snapshot()
                      for m in list(self.files.values())[-5:]],
            "recent_messages": self.history[-6:],
        }


# --- per-process registry ------------------------------------


def get_session_state(session_id: str) -> SessionState:
    """Достать (или создать) state для session_id."""
    with _STATES_LOCK:
        if session_id not in _STATES:
            _STATES[session_id] = SessionState(session_id=session_id)
        return _STATES[session_id]


def drop_session_state(session_id: str) -> None:
    with _STATES_LOCK:
        _STATES.pop(session_id, None)


def _format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} Б"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} КБ"
    return f"{n / 1024 / 1024:.1f} МБ"