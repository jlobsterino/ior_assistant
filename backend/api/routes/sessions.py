"""
Эндпоинты для работы с сессиями (история).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException

from backend.storage.database import (MessageRepo, SessionRepo, get_db,
                                      msg_to_dict, session_to_dict)

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

_WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def _classify(dt: datetime, now: datetime) -> tuple[str, str]:
    """Возвращает (group, time_label)."""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    yday = today - timedelta(days=1)
    week_start = today - timedelta(days=now.weekday())
    last_week_start = week_start - timedelta(days=7)

    if dt >= today:
        return "Сегодня", dt.strftime("%H:%M")
    if dt >= yday:
        return "Вчера", "вчера"
    if dt >= week_start:
        return "Эта неделя", _WEEKDAYS[dt.weekday()]
    if dt >= last_week_start:
        return "Прошлая неделя", _WEEKDAYS[dt.weekday()]
    if dt.year == now.year:
        return dt.strftime("%B"), dt.strftime("%d.%m")
    return str(dt.year), dt.strftime("%d.%m.%Y")


def _enrich(session: dict) -> dict:
    """Добавляет поля group/time для UI."""
    try:
        last = datetime.fromisoformat(session["last_message_at"])
    except Exception:
        last = datetime.utcnow()
    group, t = _classify(last, datetime.utcnow())
    session["group"] = group
    session["time"] = t
    return session


@router.get("")
async def list_sessions():
    with get_db() as db:
        sessions = SessionRepo.list_for_user(db)
        items = [_enrich(session_to_dict(s)) for s in sessions]
        return {"sessions": items}


@router.get("/{session_id}")
async def get_session(session_id: str):
    with get_db() as db:
        s = SessionRepo.get(db, session_id)
        if s is None:
            raise HTTPException(404, "Сессия не найдена")
        out = session_to_dict(s, with_messages=True)
        return _enrich(out)


@router.delete("/{session_id}")
async def delete_session(session_id: str):
    with get_db() as db:
        SessionRepo.archive(db, session_id)
        return {"ok": True}