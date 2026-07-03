"""
Эндпоинты сохранённых отчётов аудитора (П5): сохранить QuerySpec выгрузки под
именем, получить список, повторить (отдаёт query + spec — фронт переотправляет
запрос). Лёгкое JSON-хранилище в backend.agent.saved_reports.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.agent import saved_reports

router = APIRouter(prefix="/api/reports", tags=["reports"])


class SaveReportIn(BaseModel):
    name: str
    spec: dict
    query: str | None = None
    session_id: str | None = None


@router.get("")
def list_reports():
    return {"items": saved_reports.list_reports()}


@router.post("")
def save_report(body: SaveReportIn):
    if not body.name or not body.spec:
        raise HTTPException(status_code=400, detail="name и spec обязательны")
    rec = saved_reports.save_report(body.name, body.spec, body.query, body.session_id)
    return {"ok": True, "id": rec["id"], "name": rec["name"]}


@router.get("/{report_id}")
def get_report(report_id: str):
    rec = saved_reports.get_report(report_id)
    if not rec:
        raise HTTPException(status_code=404, detail="отчёт не найден")
    return rec


@router.delete("/{report_id}")
def delete_report(report_id: str):
    return {"ok": saved_reports.delete_report(report_id)}