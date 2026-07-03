"""
saved_reports - легкое хранилище именованных отчетов аудитора (П5).

Аудитор гоняет одни и те же срезы ежемесячно. Сохраняем QuerySpec выгрузки под
именем, чтобы повторить одной кнопкой (в т.ч. за другой период). Хранилище -
простой JSON-файл; офлайн-безопасный модуль (только stdlib), путь через env
REPORTS_STORE (по умолчанию data/saved_reports.json относительно cwd).
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional


def _store_path() -> Path:
    return Path(os.environ.get("REPORTS_STORE", "data/saved_reports.json"))


def _load() -> list:
    p = _store_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:  # noqa: BLE001
        return []


def _write(items: list) -> None:
    p = _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def save_report(name: str, spec: dict, query: Optional[str] = None,
                session_id: Optional[str] = None, now: Optional[float] = None) -> dict:
    """Сохранить (или обновить по имени) отчёт. Возвращает запись."""
    items = _load()
    rec = {
        "id": uuid.uuid4().hex[:12],
        "name": (name or "Без названия").strip()[:120],
        "spec": spec,
        "query": (query or "").strip()[:500],
        "session_id": session_id,
        "created_at": now if now is not None else time.time(),
    }
    # перезапись по имени (без дублей)
    items = [r for r in items if r.get("name") != rec["name"]]
    items.insert(0, rec)
    _write(items[:200])
    return rec


def list_reports(limit: int = 50) -> list:
    """Список отчётов (без полного spec - компактно для UI-списка)."""
    out = []
    for r in _load()[:limit]:
        out.append({"id": r.get("id"), "name": r.get("name"),
                    "query": r.get("query"), "created_at": r.get("created_at")})
    return out


def get_report(report_id: str) -> Optional[dict]:
    return next((r for r in _load() if r.get("id") == report_id), None)


def delete_report(report_id: str) -> bool:
    items = _load()
    new = [r for r in items if r.get("id") != report_id]
    if len(new) == len(items):
        return False
    _write(new)
    return True