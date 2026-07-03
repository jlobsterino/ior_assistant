"""
Скачивание сгенерированных Excel/CSV-файлов + polling-status.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from backend.storage.database import FileRepo, get_db

router = APIRouter(prefix="/api/files", tags=["files"])


@router.get("/{file_id}")
async def download_file(file_id: str):
    """Скачать .xlsx. Если статус != ready -> 425 Too Early."""
    with get_db() as db:
        f = FileRepo.get(db, file_id)
        if f is None:
            raise HTTPException(404, "Файл не найден")
        if f.status == "failed":
            raise HTTPException(500, f"Ошибка генерации: {f.error or 'неизвестная'}")
        if f.status == "preparing":
            raise HTTPException(425, "Файл ещё готовится — ожидайте")
        path = Path(f.file_path)
        if not path.exists():
            raise HTTPException(410, "Файл удалён")
        return FileResponse(
            path,
            filename=f.file_name,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


@router.get("/{file_id}/raw")
async def download_file_raw(file_id: str):
    """Скачать файл в исходном виде с автоматическим определением media_type."""
    import mimetypes
    with get_db() as db:
        f = FileRepo.get(db, file_id)
        if f is None:
            raise HTTPException(404, "Файл не найден")
        path = Path(f.file_path)
        if not path.exists():
            raise HTTPException(410, "Файл удалён")
        
        mime_type, _ = mimetypes.guess_type(path)
        if not mime_type:
            mime_type = "application/octet-stream"
            
        return FileResponse(
            path,
            media_type=mime_type,
            filename=f.file_name,
        )


@router.get("/{file_id}/csv")
async def download_csv(file_id: str):
    """
    Phase 4.2: CSV-альтернатива.

    Юзер кликает «Скачать как CSV» -> отдаём .csv (~10x меньше xlsx,
    открывается в Excel/Google Sheets/любой утилите). Создаётся
    параллельно с xlsx в notebook_runner._real_run_phased.
    """
    with get_db() as db:
        f = FileRepo.get(db, file_id)
        if f is None:
            raise HTTPException(404, "Файл не найден")
        if not f.csv_path:
            raise HTTPException(404, "CSV-альтернатива не создана для этого файла")
        path = Path(f.csv_path)
        if not path.exists():
            raise HTTPException(410, "CSV-файл удалён")
        # Имя для скачивания — заменяем .xlsx -> .csv
        csv_filename = f.file_name.rsplit(".", 1)[0] + ".csv"
        return FileResponse(
            path,
            filename=csv_filename,
            media_type="text/csv; charset=utf-8",
        )


@router.get("/{file_id}/status")
async def file_status(file_id: str):
    """
    Phase 3: Polling-endpoint для UI ExcelAttachment.

    UI с интервалом 1-2с дёргает этот endpoint пока status != ready/failed.
    Возвращает: status, bytes_written, size_bytes, total_rows, error.

    В WS-режиме обновления приходят пушем (file_progress/file event),
    polling — для случаев когда WS отвалился и UI перезагрузился.
    """
    with get_db() as db:
        f = FileRepo.get(db, file_id)
        if f is None:
            raise HTTPException(404, "Файл не найден")
        # Если файл ready но на диске не существует — пометим как failed
        if f.status == "ready":
            path = Path(f.file_path)
            if not path.exists():
                return {
                    "file_id": file_id,
                    "status": "failed",
                    "error": "Файл исчез с диска",
                }
            # Размер на диске может отличаться от того что в БД, обновим
            size_on_disk = path.stat().st_size
            return {
                "file_id": file_id,
                "status": "ready",
                "size_bytes": size_on_disk,
                "total_rows": f.total_rows,
                "has_csv": bool(f.csv_path and Path(f.csv_path).exists()),
            }
        return {
            "file_id": file_id,
            "status": f.status,
            "bytes_written": f.bytes_written or 0,
            "total_rows": f.total_rows,
            "error": f.error,
        }