"""
agent_flow.run_agent – главный entry для WS-стрима.

ReAct-агент: контроллер run_agent_v2 как фоновая корутина, питающая event_queue/
yield-цикл, который конвертит события в SSE для фронта. Файл-выгрузку создаёт
QuerySpec-компилятор/тулы (state.files); регистрация в БД + FAISS-хвост для
follow-up – здесь же.

(Старый blind make_plan->execute_plan->narrate путь удалён – заменён контроллером.)
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncIterator

from backend.agent.state import get_session_state
from backend.storage.database import FileRepo, MessageRepo, SessionRepo, get_db

logger = logging.getLogger(__name__)


# ——— SSE serialization helpers ——————————————————————————————————————


def _scrub_for_json(obj):
    """Рекурсивно превращает dict/list в JSON-safe форму:
    - float NaN/Inf -> None (иначе json.dumps выдаёт литерал NaN,
      который JS JSON.parse() не понимает -> клиент роняет всё событие)
    - datetime/Timestamp/date/NaT -> isoformat str
    - numpy.int64/float64 -> python int/float (с NaN-чисткой)
    - всё прочее (несериализуемое) -> str(obj)
    """
    import math
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        return None if math.isnan(obj) or math.isinf(obj) else obj
    if isinstance(obj, dict):
        return {str(k): _scrub_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub_for_json(v) for v in obj]
    # datetime, Timestamp, date, NaT
    if hasattr(obj, "isoformat"):
        try:
            return obj.isoformat()
        except Exception:
            return str(obj)
    # numpy.int64 / numpy.float64 / pandas-scalars
    if hasattr(obj, "item"):
        try:
            v = obj.item()
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                return None
            return v
        except Exception:
            return str(obj)
    return str(obj)


def sse(event: str, data: dict) -> str:
    # NaN/Inf/datetime/numpy в payload ломают JS JSON.parse -> клиент
    # теряет step_done event -> шаг навечно остаётся в running. Скрабим всё.
    safe = _scrub_for_json(data)
    return f"event: {event}\ndata: {json.dumps(safe, ensure_ascii=False)}\n\n"


_INITIAL_PAD_BYTES = 16 * 1024
_KEEPALIVE_BYTES = 2 * 1024
SSE_INITIAL_PADDING = ":" + (" " * _INITIAL_PAD_BYTES) + "\n\n"
SSE_KEEPALIVE = ":" + ("k" * _KEEPALIVE_BYTES) + "\n\n"


# ——— Главный flow (ReAct-контроллер) ——————————————————————————


async def run_agent(*, session_id: str,
                    user_message: str, clarify_strikes: int = 0) -> AsyncIterator[str]:
    """ReAct-контроллер run_agent_v2 как фоновая корутина, стримит SSE-события."""
    state = get_session_state(session_id)
    state.history.append({"role": "user", "content": user_message})
    # сбрасываем маркер успеха прошлого turn'а (ставится ниже только если СЕЙЧАС
    # создан файл) – иначе IOR-роут увидит stale-маркер и решит, что выгрузка была.
    try:
        from backend.IOR_pipeline_search import _SMALL_FAISS_SESSION_CACHE
        _SMALL_FAISS_SESSION_CACHE.get(session_id, {}).pop("agent_file_id", None)
    except Exception:  # noqa: BLE001
        pass
    started = time.perf_counter()
    timeline: list[dict] = []

    def _ts() -> str:
        return f"+{time.perf_counter() - started:0.1f}s"

    def push(step_id: str, label: str, status: str = "active") -> dict:
        for t in timeline:
            if t.get("status") == "active":
                t["status"] = "done"
        timeline.append({"step": step_id, "label": label, "time": _ts(), "status": status})
        return {"steps": list(timeline)}

    event_queue: asyncio.Queue = asyncio.Queue()

    async def emit(event: str, data: dict) -> None:
        await event_queue.put((event, data))

    # сохранить user-сообщение
    try:
        with get_db() as db:
            sess = SessionRepo.get(db, session_id)
            if sess and not sess.title:
                title = user_message[:60] + ("..." if len(user_message) > 60 else "")
                SessionRepo.update_title(db, session_id, title)
            if sess:
                MessageRepo.add(db, session_id=session_id, role="user",
                                content=user_message)
                SessionRepo.touch(db, session_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("[agent_flow] не смог сохранить user msg: %s", e)

    yield sse("status", push("controller", "Анализ запроса..."))

    from backend.agent.controller import run_agent_v2
    exec_task = asyncio.create_task(
        run_agent_v2(state=state, user_msg=user_message, emit=emit, clarify_strikes=clarify_strikes)
    )
    get_task = asyncio.create_task(event_queue.get())

    while True:
        done, _ = await asyncio.wait(
            {exec_task, get_task}, return_when=asyncio.FIRST_COMPLETED, timeout=2.0
        )
        new_events = []
        if get_task in done:
            new_events.append(get_task.result())
            while not event_queue.empty():
                try:
                    new_events.append(event_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            get_task = asyncio.create_task(event_queue.get())

        for event, data in new_events:
            yield sse(event, data)
            label = _event_to_label(event, data)
            if label:
                yield sse("status", push(f"{event}:{data.get('step_id', '')}", label))

        if exec_task.done():
            while not event_queue.empty():
                try:
                    e2, d2 = event_queue.get_nowait()
                    yield sse(e2, d2)
                except asyncio.QueueEmpty:
                    break
            break

    if not get_task.done():
        get_task.cancel()
        try:
            await get_task
        except (asyncio.CancelledError, Exception):
            pass

    try:
        turn = await exec_task
    except Exception as e:  # noqa: BLE001
        logger.exception("[agent_flow] контроллер упал: %s", e)
        yield sse("error", {"message": f"Контроллер упал: {e}"})
        yield sse("done", {"ok": False})
        return

    # контроллер уже эмитнул clarification (если был ask_user) – просто закрываем turn
    if turn.ask_user:
        is_stuck = bool(getattr(turn, "stuck", False))
        print(f"[DEBUG_TURN] turn type={type(turn)}, turn.ok={turn.ok}, stuck attr={getattr(turn, 'stuck', 'NO_ATTR')!r}")
        try:
            from backend.IOR_pipeline_search import _SMALL_FAISS_SESSION_CACHE
            sd = _SMALL_FAISS_SESSION_CACHE.setdefault(session_id, {})
            sd["last_ask_user_stuck"] = bool(is_stuck)
        except Exception as cache_err:
            logger.warning("[agent_flow] не смог записать last_ask_user_stuck: %s", cache_err)
        yield sse("done", {"asked_user": True, "stuck": is_stuck})
        return

    # богатая мета файла (строки/колонки/превью) – из ФИНАЛЬНОГО DF компилятора
    # (он остаётся в state после эдикции промежуточных). Иначе UI-карточка = «0 строк».
    f_rows, f_cols, f_headers, f_sample = 0, 0, [], []
    final_df_id = next((o.payload.get("file_id") for o in reversed(turn.history)
                        if o.payload.get("file_id")), None)
    if final_df_id and final_df_id in getattr(state, "dataframes", {}):
        try:
            fdf = state.dataframes[final_df_id]
            cols = list(fdf.columns)[:12]
            f_rows, f_cols = len(fdf), len(fdf.columns)
            f_headers = [str(c) for c in cols]
            for _, row in fdf.head(5).iterrows():
                f_sample.append(["" if row[c] is None else str(row[c])[:200] for c in cols])
        except Exception as e:  # noqa: BLE001
            logger.warning("[agent_flow] file meta: %s", e)

    # регистрируем файлы, созданные тулами/компилятором (state.files)
    file_id_db = None
    f_meta = None
    for f_meta in state.files.values():
        if getattr(f_meta, "_db_id", None):
            file_id_db = f_meta._db_id
            continue
        try:
            with get_db() as db:
                f = FileRepo.add(db, session_id=session_id, file_path=f_meta.path,
                                 file_name=f_meta.name, size_bytes=f_meta.size_bytes,
                                 status="ready", total_rows=f_rows or None)

                file_id_db = f.id
                f_meta._db_id = file_id_db
                yield sse("file", {"file_id": file_id_db, "name": f_meta.name,
                                   "size": _fmt_size(f_meta.size_bytes), "status": "ready",
                                   "rows": f_rows, "columns": f_cols,
                                   "sample": f_sample, "sample_headers": f_headers})
        except Exception as e:  # noqa: BLE001
            logger.warning("[agent_flow] FileRepo.add: %s", e)

    # Маркер успеха для IOR-роута: файл-выгрузка готова. Агрегированные выгрузки
    # (по процессам/ТБ) НЕ имеют построчных описаний -> FAISS-кэш ниже не наполнится,
    # но это НЕ провал агента – роут должен это понимать и НЕ уходить в fallback-поиск.
    if file_id_db:
        try:
            from backend.IOR_pipeline_search import _SMALL_FAISS_SESSION_CACHE
            _SMALL_FAISS_SESSION_CACHE.setdefault(session_id, {})["agent_file_id"] = file_id_db
        except Exception as e:  # noqa: BLE001
            logger.warning("[agent_flow] success-marker: %s", e)

    # ——— result-пакет: методология / воронка / превью / ключевые числа (П1/П2/П4) ———
    result_pkg = None
    try:
        rdf_id = turn.df_id or final_df_id
        rdf = state.dataframes.get(rdf_id) if rdf_id else None
        if turn.spec_resolved and rdf is not None:
            from backend.agent.result import build_result_package
            result_pkg = build_result_package(turn.spec_resolved, rdf, turn.funnel,
                                              turn.warnings, file_id_db)

            result_pkg["query"] = user_message  # для «сохранить отчёт» / повтора (П5)
            yield sse("result", result_pkg)
    except Exception as e:  # noqa: BLE001
        logger.warning("[agent_flow] result package: %s", e)

    # нарратив: final_text контроллера (или fallback)
    narrative = turn.final_text or _fallback_narrative(turn, state)
    for chunk in _chunk_text(narrative):
        yield sse("token", {"text": chunk})
        await asyncio.sleep(0.012)

    try:
        with get_db() as db:
            MessageRepo.add(db, session_id=session_id, role="assistant",
                            content=narrative,
                            meta={"file_id": file_id_db, "sseSteps": timeline,
                                  "controller": "iterative", "result": result_pkg})
    except Exception as e:  # noqa: BLE001
        logger.warning("[agent_flow] save assistant msg: %s", e)
    state.history.append({"role": "assistant", "content": narrative})

    duration_ms = int((time.perf_counter() - started) * 1000)
    yield sse("status", push("done", "Готово", "done"))
    yield sse("done", {"file_id": file_id_db, "duration_ms": duration_ms})

    # FAISS-хвост для follow-up'ов по построчным выгрузкам
    try:
        import os
        import pandas as pd
        if f_meta and os.path.exists(f_meta.path):
            df_agent = pd.read_excel(f_meta.path)
            id_col = next((c for c in ["incdnt_id", "incdnt_sid", "Идентификатор события",
                                       "Идентификационный ключ инцидента операционного риска"]
                           if c in df_agent.columns), None)
            desc_col = next((c for c in ["incdnt_desc", "Текст_ИОР", "incdnt_description",
                                         "описание", "incdnt_full_descr_txt", "Подробное описание"]
                             if c in df_agent.columns), None)
            if id_col and desc_col:
                from backend.IOR_pipeline_search import _SMALL_FAISS_SESSION_CACHE
                # update(), НЕ перезапись – сохраняем agent_file_id-маркер выше
                _SMALL_FAISS_SESSION_CACHE.setdefault(session_id, {}).update({
                    "target_ids": df_agent[id_col].dropna().astype(str).unique().tolist(),
                    "id_to_text_map": dict(zip(df_agent[id_col].astype(str),
                                              df_agent[desc_col].fillna(""))),
                })
    except Exception as e:  # noqa: BLE001
        logger.error("[agent_flow] FAISS prep: %s", e, exc_info=True)


# ——— Helpers ——————————————————————————————————————————————————————


def _fallback_narrative(turn, state) -> str:
    parts = ["✅ Готово."]
    if state.files:
        parts.append("\n\n📁 Файлы:")
        for f in state.files.values():
            parts.append(f"\n – {f.name} ({_fmt_size(f.size_bytes)})")
    return "".join(parts)


def _event_to_label(event: str, data: dict) -> str:
    if event == "step_started":
        return f"⚙️ {data.get('tool', '?')} (step {data.get('step_id', '?')})"
    if event == "step_done":
        return f"✔️ {data.get('tool', '?')}: {data.get('summary', 'done')[:80]}"
    if event == "step_failed":
        return f"⚠️ {data.get('tool', '?')} упал: {(data.get('error') or '')[:80]}"
    return ""


def _chunk_text(text: str, words_per_chunk: int = 4) -> list[str]:
    if not text:
        return []
    words = text.split(" ")
    chunks = []
    for i in range(0, len(words), words_per_chunk):
        piece = " ".join(words[i:i + words_per_chunk])
        if i + words_per_chunk < len(words):
            piece += " "
        chunks.append(piece)
    return chunks


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} Б"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} КБ"
    return f"{n / 1024 / 1024:.1f} МБ"