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
import os
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
    for o in reversed(turn.history):
        if not o.ok:
            continue
        if o.payload.get("file_id"):
            f_rows = o.payload.get("rows") or 0
            df_id = turn.df_id or o.payload.get("df_id")
            if not df_id:
                df_id = next((x.payload.get("df_id") for x in reversed(turn.history)
                              if x.payload.get("df_id")), None)
            
            if df_id and df_id in getattr(state, "dataframes", {}):
                try:
                    fdf = state.dataframes[df_id]
                    cols = list(fdf.columns)[:12]
                    f_rows = len(fdf)
                    f_cols = len(fdf.columns)
                    f_headers = [str(c) for c in cols]
                    f_sample = []
                    for _, row in fdf.head(5).iterrows():
                        f_sample.append(["" if row[c] is None else str(row[c])[:200] for c in cols])
                except Exception as e:  # noqa: BLE001
                    logger.warning("[agent_flow] file meta from df: %s", e)
            break

    # регистрируем файлы, созданные тулами/компилятором (state.files)
    file_id_db = None
    f_meta = None
    file_events_to_yield = []
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
                file_events_to_yield.append(sse("file", {"file_id": file_id_db, "name": f_meta.name,
                                                   "size": _fmt_size(f_meta.size_bytes), "status": "ready",
                                                   "rows": f_rows, "columns": f_cols,
                                                   "sample": f_sample, "sample_headers": f_headers}))
        except Exception as e:  # noqa: BLE001
            logger.warning("[agent_flow] FileRepo.add: %s", e)

    excel_meta_to_save = None
    if file_id_db and f_meta:
        excel_meta_to_save = {
            "name": f_meta.name,
            "size": _fmt_size(f_meta.size_bytes),
            "rows": f_rows,
            "columns": f_cols,
            "sample": f_sample,
            "sample_headers": f_headers,
            "status": "ready",
            "has_csv": os.path.exists(str(f_meta.path).replace(".xlsx", ".csv")) if getattr(f_meta, "path", None) else False
        }

    # Маркер успеха для IOR-роута: файл-выгрузка готова. Агрегированные выгрузки
    # (по процессам/ТБ) НЕ имеют построчных описаний -> FAISS-кэш ниже не наполнится,
    # Маркер успеха для IOR-роута: файл-выгрузка готова или агент успешно вернул текст.
    # Агрегированные выгрузки (по процессам/ТБ) НЕ имеют построчных описаний -> FAISS-кэш ниже
    # не наполнится, но это НЕ провал агента – роут должен это понимать и НЕ уходить в fallback-поиск.
    try:
        from backend.IOR_pipeline_search import _SMALL_FAISS_SESSION_CACHE
        _SMALL_FAISS_SESSION_CACHE.setdefault(session_id, {})["agent_file_id"] = file_id_db or "text_only"
    except Exception as e:  # noqa: BLE001
        logger.warning("[agent_flow] success-marker: %s", e)

    # Resolve final_df_id from turn or search history if not directly set
    final_df_id = turn.df_id
    if not final_df_id:
        final_df_id = next((o.payload.get("df_id") for o in reversed(turn.history)
                            if o.payload.get("df_id")), None)

    # ——— result-пакет: методология / воронка / превью / ключевые числа (П1/П2/П4) ———
    result_pkg = None
    try:
        rdf_id = turn.df_id or final_df_id
        rdf = state.dataframes.get(rdf_id) if rdf_id else None
        if rdf is None and getattr(state, "dataframes", None):
            last_df_id = list(state.dataframes.keys())[-1]
            rdf = state.dataframes[last_df_id]

        if turn.spec_resolved and rdf is not None:
            from backend.agent.result import build_result_package
            result_pkg = build_result_package(turn.spec_resolved, rdf, turn.funnel,
                                              turn.warnings, file_id_db)
            result_pkg["query"] = user_message  # для «сохранить отчёт» / повтора (П5)
    except Exception as e:  # noqa: BLE001
        logger.warning("[agent_flow] result package: %s", e)

    # Определяем наличие датафрейма (rdf) для гипотезы
    rdf_id = turn.df_id or final_df_id
    rdf = state.dataframes.get(rdf_id) if rdf_id else None
    if rdf is None and getattr(state, "dataframes", None):
        last_df_id = list(state.dataframes.keys())[-1]
        rdf = state.dataframes[last_df_id]

    file_info = {}
    if state.files:
        first_file = list(state.files.values())[0]
        file_info = {
            "id": getattr(first_file, "_db_id", None) or file_id_db,
            "name": getattr(first_file, "name", ""),
            "path": getattr(first_file, "path", "")
        }

    # нарратив строится по схеме: Отчет (описание) ->  Гипотеза -> График
    narrative = ""
    if rdf is not None and not rdf.empty:
        # 1. Отчет (описание выгрузки)
        report_desc = ""
        if turn.final_text:
            low_text = turn.final_text.lower()
            if "гипотез" in low_text or "аномал" in low_text or len(turn.final_text) > 400:
                if state.files:
                    first_file = list(state.files.values())[0]
                    report_desc = f"### Отчет сформирован\n\nСформирован файл выгрузки: **{first_file.name}** ({_fmt_size(first_file.size_bytes)}, строк: {len(rdf)}).\n"
                else:
                    report_desc = f"### Отчет сформирован\n\nДанные загружены (строк: {len(rdf)}).\n"
            else:
                report_desc = turn.final_text
        else:
            if state.files:
                first_file = list(state.files.values())[0]
                report_desc = f"### Отчет сформирован\n\nСформирован файл выгрузки: **{first_file.name}** ({_fmt_size(first_file.size_bytes)}, строк: {len(rdf)}).\n"
            else:
                report_desc = f"### Отчет сформирован\n\nДанные загружены (строк: {len(rdf)}).\n"

        if report_desc and not report_desc.endswith("\n\n"):
            report_desc = report_desc.rstrip() + "\n\n"

        # 2. Гипотеза + 3.  График (строятся в generate_hypothesis_narrative)
        from backend.agent.hypothesis import generate_hypothesis_narrative
        yield sse("status", push("hypothesis", "Анализирую данные и генерирую гипотезу...", "active"))
        yield sse("activity", {"id": "hypothesis", "kind": "think", "title": "Анализирую данные и генерирую гипотезу", "detail": "Строим гипотезы по выгрузке...", "status": "active"})
        hypothesis_narrative = await generate_hypothesis_narrative(user_message, rdf, file_info, session_id)
        yield sse("status", push("hypothesis", "Анализирую данные и генерирую гипотезу...", "done"))
        yield sse("activity", {"id": "hypothesis", "kind": "think", "title": "Анализ завершен", "detail": "Гипотеза сформирована.", "status": "done"})
        narrative = report_desc + hypothesis_narrative
    else:
        # Если данных для анализа нет, выводим ответ контроллера или fallback
        narrative = turn.final_text or _fallback_narrative(turn, state)
            
    for chunk in _chunk_text(narrative):
        yield sse("token", {"text": chunk})
        await asyncio.sleep(0.012)

    # Выводим Excel превью и файлы ПОСЛЕ того как текст и график были отстримлены
    for file_ev in file_events_to_yield:
        yield file_ev
    if result_pkg:
        yield sse("result", result_pkg)

    try:
        with get_db() as db:
            MessageRepo.add(db, session_id=session_id, role="assistant",
                            content=narrative,
                            meta={"file_id": file_id_db, "sseSteps": timeline,
                                  "controller": "iterative", "result": result_pkg,
                                  "excel": excel_meta_to_save})
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
        df_agent = None
        id_col = None
        desc_col = None
        
        # 1. Сначала ищем детальный df в памяти сессии (чтобы избежать потери точности при чтении Excel для больших ID)
        if getattr(state, "dataframes", None):
            for df_key, df_val in reversed(list(state.dataframes.items())):
                try:
                    df_val_copy = df_val.copy()
                    df_val_copy.columns = [str(c).lower() for c in df_val_copy.columns]
                    cid_col = next((c for c in ["incdnt_sid", "incdnt_id", "идентификатор события",
                                                "идентификационный ключ инцидента операционного риска"]
                                   if c in df_val_copy.columns), None)
                    cdesc_col = next((c for c in ["incdnt_desc", "текст_иор", "incdnt_description",
                                                  "описание", "incdnt_full_descr_txt", "подробное описание"]
                                     if c in df_val_copy.columns), None)
                    if cid_col and cdesc_col:
                        df_agent = df_val_copy
                        id_col = cid_col
                        desc_col = cdesc_col
                        logger.info(f"[agent_flow] Найдена детальная таблица '{df_key}' ({len(df_val)} строк) для построчного FAISS из памяти сессии")
                        break
                except Exception as df_err:
                    logger.warning("[agent_flow] Failed processing df %s from memory for FAISS: %s", df_key, df_err)
                    
        # 2. Если в памяти сессии ничего не найдено, пробуем выгруженный файл Excel как fallback
        if not (df_agent is not None and id_col and desc_col) and f_meta and os.path.exists(f_meta.path):
            try:
                # Читаем сначала заголовки Excel для динамического определения ID-колонок и сохранения их строкового типа
                df_headers = pd.read_excel(f_meta.path, nrows=0)
                dtype_dict = {}
                for col in df_headers.columns:
                    col_lower = str(col).lower()
                    if any(x in col_lower for x in ("id", "sid", "key", "номер", "идентификатор")):
                        if any(x in col_lower for x in ("cnt", "sum", "amt", "val", "кол", "кол-во", "сумма")):
                            continue
                        dtype_dict[col] = str
                
                df_agent = pd.read_excel(f_meta.path, dtype=dtype_dict)
                df_agent.columns = [str(c).lower() for c in df_agent.columns]
                id_col = next((c for c in ["incdnt_sid", "incdnt_id", "идентификатор события",
                                           "идентификационный ключ инцидента операционного риска"]
                               if c in df_agent.columns), None)
                desc_col = next((c for c in ["incdnt_desc", "текст_иор", "incdnt_description",
                                             "описание", "incdnt_full_descr_txt", "подробное описание"]
                                 if c in df_agent.columns), None)
            except Exception as e:
                logger.warning("[agent_flow] FAISS prep read excel fallback failed: %s", e)
                    
        # 3. Если детальные данные найдены, собираем FAISS-индекс для сессии
        if df_agent is not None and id_col and desc_col:
            if len(df_agent) > 100000:
                df_agent = df_agent.head(100000)
            target_ids = df_agent[id_col].dropna().astype(str).unique().tolist()
            id_to_text_map = dict(zip(df_agent[id_col].astype(str), df_agent[desc_col].fillna("")))
            
            if target_ids and id_to_text_map:
                from backend.IOR_pipeline_search import _SMALL_FAISS_SESSION_CACHE, build_and_cache_small_index
                session_cache = _SMALL_FAISS_SESSION_CACHE.setdefault(session_id, {})
                session_cache.update({
                    "target_ids": target_ids,
                    "id_to_text_map": id_to_text_map,
                })
                build_and_cache_small_index(session_id, target_ids, id_to_text_map)
                logger.info(f"[agent_flow] Успешно построен малый FAISS-индекс для сессии {session_id} на {len(target_ids)} записей")
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
    if event == "notebook_phase":
        return f"📓 {data.get('label', 'выполнение ноутбука')}"
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