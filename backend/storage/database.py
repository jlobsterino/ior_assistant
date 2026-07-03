"""
SQLite storage для сессий, сообщений и файлов.

⚠️ NFS-safe реализация (важно для DataLab / JupyterHub).

В DataLab home-каталоги примонтированы через NFS. У SQLite на NFS две
смертельные проблемы:

1. **WAL mode** использует fcntl-локи на SHM-файле. На NFS они либо
   зависают навсегда, либо отрабатывают непредсказуемо. Если БД когда-то
   была открыта в WAL mode (байты 18-19 заголовка = 2), при следующем
   connect() SQLite пытается создать SHM и захватить NFS lock - hang.

2. Даже в rollback-journal режиме стандартный `vfs=unix` берёт POSIX-локи
   через fcntl, которые на NFS могут зависнуть.

Решение:
• Перед connect патчим заголовок .db файла: WAL→DELETE (бинарно, без
  sqlite3.connect – NFS-safe).
• Удаляем артефакты `-wal` / `-shm` от предыдущего WAL.
• Подключаемся через URI с `vfs=unix-none` – полностью отключает
  fcntl/posix локи. Безопасно, т.к. сервер всегда работает в 1 процесс.
• PRAGMA journal_mode=DELETE + busy_timeout=30000.

Подход взят из agent_follow_up – там этот же баг ловили в DataLab.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import (Column, DateTime, Integer, String, Text, create_engine,
                        ForeignKey, Boolean, text)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, Session

from backend.config import get_settings

logger = logging.getLogger(__name__)

Base = declarative_base()

# ——— Models ——————————————————————————————————————————————————————


class SessionModel(Base):
    __tablename__ = "sessions"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String, nullable=True)          # для будущего LDAP
    title = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    last_message_at = Column(DateTime, default=datetime.utcnow)
    archived = Column(Boolean, default=False)
    pending_state = Column(Text, nullable=True)      # JSON-string зависшего state агента

    messages = relationship("MessageModel", back_populates="session",
                            cascade="all, delete-orphan", order_by="MessageModel.created_at")


class MessageModel(Base):
    __tablename__ = "messages"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String, ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    role = Column(String(20))                       # user / assistant / system
    content = Column(Text)
    meta = Column(Text, nullable=True)              # JSON: skill_id, params, file_id
    created_at = Column(DateTime, default=datetime.utcnow)

    session = relationship("SessionModel", back_populates="messages")


class FileModel(Base):
    __tablename__ = "files"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String, ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    message_id = Column(String, nullable=True)
    file_path = Column(String(500))
    file_name = Column(String(255))
    size_bytes = Column(Integer, default=0)
    skill_id = Column(String(100), nullable=True)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    # — Phased download state ——————————————————————————————————————
    # `status` отражает стадию: 'preparing' пока xlsx ещё пишется,
    # 'ready' когда полностью готов, 'failed' если процесс упал.
    # UI ExcelAttachment рисует spinner при preparing и кнопку download
    # только при ready.
    status = Column(String(20), default="ready")    # preparing | ready | failed
    total_rows = Column(Integer, nullable=True)     # Сколько строк будет в файле
    bytes_written = Column(Integer, default=0)      # Прогресс записи (для preparing)
    error = Column(Text, nullable=True)             # Сообщение об ошибке если failed
    csv_path = Column(String(500), nullable=True)   # CSV-альтернатива (Phase 4)


# ——— Engine (Lazy, NFS-safe) ——————————————————————————————————————

_engine = None
_SessionLocal = None


def _prepare_db(db_path: Path) -> None:
    """
    Подготовка БД перед connect – без sqlite3.connect (NFS-safe).

    1. Патчим байты 18-19 заголовка .db: WAL (2) -> DELETE (1).
       SQLite формат: https://www.sqlite.org/fileformat.html
       Offset 18: File format write version (1=rollback, 2=WAL)
       Offset 19: File format read version  (1=rollback, 2=WAL)

    2. Удаляем -wal/-shm файлы (артефакты предыдущего WAL).
    """
    # Шаг 1: патч заголовка
    if db_path.exists() and db_path.stat().st_size >= 100:
        try:
            with open(db_path, "r+b") as f:
                header = f.read(20)
                if header[:16] == b"SQLite format 3\x00":
                    write_ver = header[18]
                    read_ver = header[19]
                    if write_ver == 2 or read_ver == 2:
                        f.seek(18)
                        f.write(b"\x01\x01")  # 1 = rollback journal (DELETE)
                        logger.info(
                            "[DB] Заголовок БД: WAL -> DELETE journal mode "
                            "(WAL несовместим с NFS/DataLab)"
                        )
        except Exception as e:
            logger.warning("[DB] Не удалось пропатчить заголовок БД: %s", e)

    # Шаг 2: удаляем -wal и -shm
    for suffix in ("-wal", "-shm"):
        stale = Path(str(db_path) + suffix)
        if stale.exists():
            try:
                stale.unlink()
                logger.info("[DB] Удалён %s", stale.name)
            except Exception as e:
                logger.warning("[DB] Не удалось удалить %s: %s", stale.name, e)


def init_db() -> None:
    """Создаёт engine + таблицы. NFS-safe.

    Идемпотентна: можно вызвать несколько раз, engine создаётся однократно.
    """
    global _engine, _SessionLocal
    if _engine is not None:
        return

    cfg = get_settings()
    db_path = cfg.db_path.resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # WAL -> DELETE + чистка артефактов (без sqlite3.connect, NFS-safe)
    _prepare_db(db_path)

    def creator():
        # vfs=unix-none – полностью отключает fcntl/posix локи,
        # из-за которых SQLite на NFS зависала намертво.
        # Безопасно, т.к. uvicorn у нас всегда 1 процесс.
        # На Windows (nt) unix-none VFS отсутствует, подключаемся стандартно.
        import os
        if os.name == "nt":
            return sqlite3.connect(
                str(db_path),
                check_same_thread=False,
                timeout=30.0,
            )
        return sqlite3.connect(
            f"file:{db_path}?mode=rwc&vfs=unix-none",
            uri=True,
            check_same_thread=False,
            timeout=30.0,
        )

    _engine = create_engine(
        "sqlite://",   # URL игнорируется, когда передан creator
        creator=creator,
        future=True,
        echo=False,
    )

    with _engine.connect() as conn:
        # DELETE – надёжен на NFS, в отличие от WAL
        conn.execute(text("PRAGMA journal_mode=DELETE"))
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.execute(text("PRAGMA busy_timeout=30000"))

    logger.info("[DB] SQLite подключена (journal_mode=DELETE, vfs=unix-none)")

    Base.metadata.create_all(_engine)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False,
                                 autocommit=False, future=True)


@contextmanager
def get_db() -> Iterator[Session]:
    if _SessionLocal is None:
        init_db()
    db = _SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


# ——— Repos ————————————————————————————————————————————————————————


class SessionRepo:
    @staticmethod
    def create(db: Session, *, user_id: Optional[str] = None,
               title: Optional[str] = None) -> SessionModel:
        s = SessionModel(user_id=user_id, title=title)
        db.add(s)
        db.flush()
        return s

    @staticmethod
    def get(db: Session, session_id: str) -> Optional[SessionModel]:
        return db.get(SessionModel, session_id)

    @staticmethod
    def list_for_user(db: Session, user_id: Optional[str] = None,
                      limit: int = 50) -> list[SessionModel]:
        q = db.query(SessionModel).filter(SessionModel.archived.is_(False))
        if user_id is not None:
            q = q.filter(SessionModel.user_id == user_id)
        return q.order_by(SessionModel.last_message_at.desc()).limit(limit).all()

    @staticmethod
    def update_title(db: Session, session_id: str, title: str) -> None:
        s = db.get(SessionModel, session_id)
        if s and not s.title:
            s.title = title

    @staticmethod
    def touch(db: Session, session_id: str) -> None:
        s = db.get(SessionModel, session_id)
        if s:
            s.last_message_at = datetime.utcnow()

    @staticmethod
    def archive(db: Session, session_id: str) -> None:
        s = db.get(SessionModel, session_id)
        if s:
            s.archived = True

    @staticmethod
    def set_pending(db: Session, session_id: str, state: Optional[dict]) -> None:
        s = db.get(SessionModel, session_id)
        if s:
            s.pending_state = json.dumps(state, ensure_ascii=False) if state else None

    @staticmethod
    def get_pending(db: Session, session_id: str) -> Optional[dict]:
        s = db.get(SessionModel, session_id)
        if s and s.pending_state:
            try:
                return json.loads(s.pending_state)
            except Exception:
                return None
        return None


class MessageRepo:
    @staticmethod
    def add(db: Session, *, session_id: str, role: str, content: str,
            meta: Optional[dict] = None) -> MessageModel:
        m = MessageModel(
            session_id=session_id,
            role=role,
            content=content,
            meta=json.dumps(meta, ensure_ascii=False) if meta else None,
        )
        db.add(m)
        db.flush()
        return m

    @staticmethod
    def for_session(db: Session, session_id: str,
                    limit: int = 100) -> list[MessageModel]:
        return (db.query(MessageModel)
                .filter(MessageModel.session_id == session_id)
                .order_by(MessageModel.created_at)
                .limit(limit)
                .all())


class FileRepo:
    @staticmethod
    def add(db: Session, *, session_id: str, file_path: str, file_name: str,
            size_bytes: int = 0, skill_id: Optional[str] = None,
            message_id: Optional[str] = None,
            expires_at: Optional[datetime] = None,
            status: str = "ready",
            total_rows: Optional[int] = None) -> FileModel:
        f = FileModel(
            session_id=session_id,
            message_id=message_id,
            file_path=file_path,
            file_name=file_name,
            size_bytes=size_bytes,
            skill_id=skill_id,
            expires_at=expires_at,
            status=status,
            total_rows=total_rows,
        )
        db.add(f)
        db.flush()
        return f

    @staticmethod
    def get(db: Session, file_id: str) -> Optional[FileModel]:
        return db.get(FileModel, file_id)

    @staticmethod
    def update_progress(db: Session, file_id: str, *,
                        bytes_written: Optional[int] = None,
                        status: Optional[str] = None,
                        size_bytes: Optional[int] = None,
                        error: Optional[str] = None,
                        csv_path: Optional[str] = None) -> None:
        f = db.get(FileModel, file_id)
        if not f:
            return
        if bytes_written is not None:
            f.bytes_written = bytes_written
        if status is not None:
            f.status = status
        if size_bytes is not None:
            f.size_bytes = size_bytes
        if error is not None:
            f.error = error
        if csv_path is not None:
            f.csv_path = csv_path


def msg_to_dict(m: MessageModel) -> dict:
    return {
        "id": m.id,
        "role": m.role,
        "content": m.content,
        "meta": json.loads(m.meta) if m.meta else None,
        "created_at": m.created_at.isoformat(),
    }


def session_to_dict(s: SessionModel, with_messages: bool = False) -> dict:
    out = {
        "id": s.id,
        "title": s.title or "Новая сессия",
        "created_at": s.created_at.isoformat(),
        "last_message_at": s.last_message_at.isoformat(),
        "archived": s.archived,
    }
    if with_messages:
        out["messages"] = [msg_to_dict(m) for m in s.messages]
    return out