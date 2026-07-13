"""
POST /api/chat/sessions - создание сессии.
POST /api/chat/stream
- SSE-стрим (fallback, для loca1-режима).

WS
/api/chat/ws

- WebSocket-crpum (default ana prod / DataLab)
почему два transport'a?
Корпоративный WAF Сбера буфериТ все HTTP-responses целиком (inspect'ит
body + CTaBnT Content-Length). SSE/chunked-transfer Tam HE CTPHMNT,
даже с 16KB padding и keepalive - WAF дождётся завершения upstream и
только потом отдаст всё одним блоком (а часто просто таймаутит и
отдаёт 599).
Mebsocket - единственный transport, который проходит через их WAF
(они используют WS для самого Jupyter + kernel, поэтому он
гарантированно работает).
Подход взят из presenton (см. servers/fastapi/utils/sse_to_wI.py) -
там этот путь уже был пройден.

"""
from __future__ import annotations
import json
import logging
import re
import uuid
from typing import Optional
import traceback
import asyncio
import os
import datetime
import pandas as pd
import numpy as np
from datetime import datetime as dt_datetime
from pathlib import Path
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pyspark import SparkConf
from pyspark.sql import SparkSession
import pyspark.sql.functions as F
from backend.agent.agent_flow import run_agent
from backend.agent.flow import relay_to_ws, with_heartbeat
from backend.storage.database import SessionRepo, get_db
from local_qwen import extract_search_params
from backend.IOR_pipeline_search import build_and_cache_small_index, search_small_index, _SMALL_FAISS_SESSION_CACHE

_spark_session = None
def get_spark_session():
    global _spark_session
    if _spark_session is None:
        logger.info("[SPARK_INIT] Инициализация глобальной сессии Spark]")

        ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        conf = SparkConf().setAppName('ior_assistant_export' + ts)
        conf.setAll([
            ("spark.ui.enabled","true"),
            ("spark.master","local[*]"),
            ("spark.executor. cores","2"),
            ("spark.executor.memory","8g"),
            ("spark.executor.memoryoverhead", "1g"),
            ("spark.driver.memory", "8g"),
            ("spark.driver.maxResultsize","8g"),
            ("spark.port.maxRetries","100"),
        ])
        _spark_session = SparkSession.builder.config(conf=conf).enableHiveSupport().getOrCreate()
        logger.info("[SPARK_INIT] Spark 3anywen")
    return _spark_session

logger = logging.getLogger(__name__)
LAST_RETRIEVED_IORS_CACHE = {}
SESSION_SEARCH_CONTEXT = {}
router = APIRouter(prefix="/api/chat", tags=["chat"])

def get_session_history(session_id: str) -> list:
    print(f"[HISTORY_GET] session={session_id}", flush=True)
    print(f"[HISTORY_GET] all keys={list(_SMALL_FAISS_SESSION_CACHE.keys())}", flush=True)
    history = _SMALL_FAISS_SESSION_CACHE.get(session_id, {}).get("history", [])
    print(f"[HISTORY_GET] found={len(history)} messages", flush=True)
    return history

def update_session_history(session_id: str, user_message: str, assistant_response: str):
    cache = _SMALL_FAISS_SESSION_CACHE.get(session_id, {})
    history = cache.get("history", [])
    history.append({"role": "user", "content": user_message})
    history.append({"role": "assistant", "content": assistant_response})
    cache["history"] = history[-10:]
    _SMALL_FAISS_SESSION_CACHE[session_id] = cache
    print(f"[HISTORY_SAVE] session={session_id} total={len(cache['history'])}", flush=True)
    print(f"[HISTORY_SAVE] keys in cache={list(_SMALL_FAISS_SESSION_CACHE.keys())}", flush=True)

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None

class NewSessionRequest(BaseModel):
    title: Optional[str] = None

@router.post("/sessions")
async def create_session(req: NewSessionRequest):
    with get_db() as db:
        s = SessionRepo.create(db, title=req.title)
        return {"session_id": s.id, "title": s.title}

from backend.pipeline_search import search_pipeline, faiss_loaded, bm25_indexes

@router.post("/stream")
async def chat_stream(req: ChatRequest):
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="Сообщение пустое")
    
    session_id = req.session_id
    if not session_id:
        with get_db() as db:
            s = SessionRepo.create(db)
            session_id = s.id
    return StreamingResponse(
        # with_heartbeat: 16KB initial pad + 2KB keepalive kaxoyb cekyHoy
        # если основной iплег молчит. Пробивает буферы корп. nginx /
        # JupyterHub-прокси (паттерн из presenton, проверен в сбер-проде).
        with_heartbeat(
            run_agent(session_id=session_id, user_message=req.message),
            interval=1.0,
        ), 
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform", # no-transform: HAProxy
            "Pragma": "no-cache",
            "X-Accel-Buffering": "no",          # nginx
            "Connection": "keep-alive",
            "Content-Encoding": "identity",
            "X-Session-Id": str(session_id),    # sanpeтить gzip
        },
    )

from backend.pipeline_search import search_pipeline as run_complaints_pipeline
from backend.pipeline_search import faiss_loaded, bm25_indexes
from backend.gigachat_extractor import summarize_complaints
from backend.storage.database import MessageRepo, SessionRepo
from backend. IOR_pipeline_search import search_pipeline as run_ior_pipeline
from local_qwen import summarize_iors
# - WebSocket endpoint (default dna prod / DataLab)

@router.websocket("/ws")
async def chat_ws(websocket: WebSocket):
    """WebSocket-BapnaHT /stream.
    Протокол:
    Client + first frame: {"message": " ... ", "session_id": " ... " | null}
    Client + cancel frame: {"cancel": true} - отменяет идущий Spark job
    Server + text frames:
    {"event": "<name>", "data": <json>}
    Если session_id не передан, сервер создаёт новую сессию и шлёт
    ("event":"session", "data":{"session_id":" ... "}} до начала run_chat.
    """
    from backend.skills.runners.notebook_runner import get_runner
    await websocket.accept()
    cancel_listener_task = None
    try:
        logger.info("[ws] accepted, waiting for first frame")
        first_frame = await websocket.receive_text()
        logger.info(f"[ws] first frame received: {first_frame[:200]}")
        try:
            payload = json. loads(first_frame)
        except Exception:
            await websocket.send_text(json.dumps({
                "event": "error",
                "data": {"message": "Invalid first frame (expected JSON)"},
            }))
            await websocket.close()
            return

        message = (payload.get("message") or "").strip()
        session_id = payload.get("session_id") or None
        mode = payload.get("mode", "agent")
        if not message:
            await websocket.send_text(json.dumps({
                "event": "error",
                "data": {"message": "Сообщение пустое"},
            }))
            await websocket.close()
            return
        
        if not session_id:
            with get_db() as db:
                s = SessionRepo.create(db)
                session_id = s.id
        await websocket.send_text(json.dumps({
            "event": "session",
            "data": {"session_id": session_id},
        }))
        print(f"DEBUG: Получено сообщение: '{message}'")

        mode = payload.get("mode", "agent")
        logger.info(f"[ws] mode={mode}, message={message[:50]}")
        is_complaint = (mode == "pipeline")
        is_ior = (mode == "ior_pipeline")

        if is_complaint:
            from backend.storage.database import FileRepo, get_db
            from backend.agent.complaint_hypothesis import (
                _determine_complaint_route,
                _COMPLAINTS_SESSION_CACHE,
                generate_complaint_hypothesis_narrative,
                classify_complaint_intent,
                search_complaints_cache,
                answer_complaint_details,
                answer_complaint_follow_up,
                answer_complaint_dialog
            )
            try:
                # Helper to send activities
                async def send_activity(aid: str, kind: str, title: str, detail: str = None, status: str = "active"):
                    await websocket.send_text(json.dumps({
                        "event": "activity",
                        "data": {
                            "id": aid,
                            "kind": kind,
                            "title": title,
                            "detail": detail,
                            "status": status
                        }
                    }, ensure_ascii=False))

                route = _determine_complaint_route(session_id)
                logger.info(f"[COMPLAINT ROUTE] session={session_id} -> {route}")

                if route == "analytical":
                    # Step 1: Start search activity
                    await send_activity(
                        aid="fb_search_operation",
                        kind="action",
                        title="Поиск обращений в базе",
                        detail="Ищу подходящие обращения через гибридный векторный и текстовый поиск...",
                        status="active"
                    )
                    await websocket.send_text(json.dumps({
                        "event": "status",
                        "data": {"steps": [
                            {"step": "search", "label": "Ищу похожие обращения в базе...", "status": "active"},
                        ]}
                    }, ensure_ascii=False))
                    await asyncio.sleep(0.1)

                    params = extract_search_params(message)
                    logger.info(f"[pipeline] params from LLM: {params}")
                    df_result = run_complaints_pipeline(
                        query=params["query"],
                        faiss_idx=faiss_loaded,
                        bm25_indexes=bm25_indexes,
                        top_k=params["top_k"],
                        date_range=params["date_range"]
                    )

                    if 'text' in df_result.columns:
                        df_result = df_result.drop(columns=['text'])

                    # Populate complaints cache
                    id_to_text_map = {}
                    for _, row in df_result.iterrows():
                        cid = str(row.get('id', ''))
                        desc = row.get('Короткое описание', '')
                        dialogue = row.get('Транскрибация диалога', '')
                        date_str = row.get('date', '')
                        if cid:
                            id_to_text_map[cid] = {
                                "id": cid,
                                "desc": desc,
                                "dialogue": dialogue,
                                "date": date_str
                            }
                    _COMPLAINTS_SESSION_CACHE[session_id] = {
                        "id_to_text_map": id_to_text_map
                    }

                    # Step 2: Complete search activity
                    await send_activity(
                        aid="fb_search_operation",
                        kind="action",
                        title="Поиск завершен",
                        detail=f"Найдено {len(df_result)} релевантных обращений.",
                        status="done"
                    )

                    # Step 3: Start hypothesis generation activity
                    await send_activity(
                        aid="fb_summary_operation",
                        kind="think",
                        title="Анализ и гипотезы",
                        detail="Формируем гипотезы о корневых причинах с помощью GigaChat...",
                        status="active"
                    )

                    await websocket.send_text(json.dumps({
                        "event": "status",
                        "data": {
                            "steps": [
                                {"step": "search", "label": "Поиск завершен", "status": "done"},
                                {"step": "result", "label": f"🟢 Найдено обращений: {len(df_result)}", "status": "done"},
                                {"step": "summary", "label": "Анализ обращений и формирование гипотез...", "status": "active"}
                            ]
                        }
                    }, ensure_ascii=False))

                    # Step 4: Export spreadsheet
                    file_id = str(uuid.uuid4())
                    data_dir = Path("/home/datalab/nfs/disrupt_tester2/data/generated_files")
                    data_dir.mkdir(parents=True, exist_ok=True)

                    xlsx_path = data_dir / f"{file_id}.xlsx"
                    csv_path = data_dir / f"{file_id}.csv"

                    df_result.to_excel(xlsx_path, index=False)
                    df_result.to_csv(csv_path, index=False, encoding="utf-8")

                    with get_db() as db:
                        f = FileRepo.add(
                            db,
                            session_id=session_id,
                            file_path=str(xlsx_path),
                            file_name=f"osiris_{params['query'][:30]}.xlsx",
                            size_bytes=xlsx_path.stat().st_size,
                            total_rows=len(df_result),
                            status="ready",
                        )
                        FileRepo.update_progress(
                            db,
                            file_id=f.id,
                            csv_path=str(csv_path),
                            status="ready"
                        )
                        file_id = f.id

                    preview_k = params["top_k"] or 5
                    top_n = df_result.head(preview_k).copy()
                    top_n.insert(0, 'Ранг релевантности', range(1, len(top_n) + 1))

                    sample_headers = list(top_n.columns)
                    sample = [[str(v) if v is not None else "-" for v in row] for row in top_n.values.tolist()]

                    await websocket.send_text(json.dumps({
                        "event": "file",
                        "data": {
                            "file_id": file_id,
                            "name": f"osiris_{params['query'][:30]}.xlsx ({len(df_result)} обращений)",
                            "rows": len(df_result),
                            "columns": len(df_result.columns),
                            "sample_headers": sample_headers,
                            "sample": sample,
                            "has_csv": True,
                            "status": "ready",
                        }
                    }, ensure_ascii=False))

                    # Generate narrative
                    file_info = {
                        "name": f"osiris_{params['query'][:30]}.xlsx",
                        "size": f"{xlsx_path.stat().st_size / 1024:.1f} KB"
                    }
                    summary_text = await generate_complaint_hypothesis_narrative(
                        user_msg=message,
                        df=df_result,
                        file_info=file_info
                    )

                    # Stream narrative token by token to chat
                    chunk_size = 4
                    for i in range(0, len(summary_text), chunk_size):
                        chunk = summary_text[i:i + chunk_size]
                        await websocket.send_text(json.dumps({
                            "event": "token",
                            "data": {"text": chunk}
                        }, ensure_ascii=False))
                        await asyncio.sleep(0.05)

                    # Step 5: Complete hypothesis generation activity
                    await send_activity(
                        aid="fb_summary_operation",
                        kind="think",
                        title="Анализ завершен",
                        detail="Краткий отчет и гипотезы выведены в интерфейс.",
                        status="done"
                    )

                    await websocket.send_text(json.dumps({
                        "event": "status",
                        "data": {
                            "steps": [
                                {"step": "search", "label": "Поиск завершен", "status": "done"},
                                {"step": "result", "label": f"🟢 Найдено обращений: {len(df_result)}", "status": "done"},
                                {"step": "summary", "label": "Анализ и гипотезы сформированы", "status": "done"}
                            ]
                        }
                    }, ensure_ascii=False))

                    # Save history in DB
                    with get_db() as db:
                        MessageRepo.add(db, session_id=session_id, role='user', content=message)
                        MessageRepo.add(
                            db,
                            session_id=session_id,
                            role='assistant',
                            content=summary_text,
                            meta={
                                "skill_id": None,
                                "skill_title": None,
                                "file_id": file_id,
                                "stats": None,
                                "excel": {
                                    "file_id": file_id,
                                    "name": f"osiris_{params['query'][:30]}.xlsx ({len(df_result)} обращений)",
                                    "rows": len(df_result),
                                    "columns": len(df_result.columns),
                                    "sample_headers": sample_headers,
                                    "sample": sample,
                                    "has_csv": True,
                                    "status": "ready",
                                },
                                "dossier": None,
                                "followups": None,
                                "sseSteps": [
                                    {"step": "search", "label": "Поиск завершен", "status": "done"},
                                    {"step": "result", "label": f"🟢 Найдено обращений: {len(df_result)}", "status": "done"},
                                    {"step": "summary", "label": "Анализ и гипотезы сформированы", "status": "done"}
                                ],
                                "plan": None,
                                "step_results": []
                            }
                        )
                        SessionRepo.update_title(db, session_id=session_id, title=params['query'][:50])
                        SessionRepo.touch(db, session_id=session_id)

                    # Send done event to clear frontend loader shimmer
                    await websocket.send_text(json.dumps({
                        "event": "done",
                        "data": {}
                    }))

                elif route == "follow_up":
                    import time
                    session_data = _COMPLAINTS_SESSION_CACHE.get(session_id, {})
                    id_to_text_map: dict = session_data.get("id_to_text_map", {})
                    history = get_session_history(session_id)

                    timeline = []
                    started_time = time.perf_counter()

                    def _ts() -> str:
                        return f"+{time.perf_counter() - started_time:0.1f}s"

                    def make_status_payload(step_id: str, label: str, status: str = "active") -> dict:
                        for t in timeline:
                            if t.get("status") == "active":
                                t["status"] = "done"
                        timeline.append({
                            "step": step_id,
                            "label": label,
                            "time": _ts(),
                            "status": status
                        })
                        return {"event": "status", "data": {"steps": list(timeline)}}

                    # 1. Start thinking status
                    await websocket.send_text(json.dumps(
                        make_status_payload("thinking", "💬 Анализирую запрос...", "active"),
                        ensure_ascii=False
                    ))
                    await asyncio.sleep(0.05)

                    response_text = None

                    # Check for ID matches (standalone word or substring of keys)
                    id_matches = []
                    for cid in id_to_text_map.keys():
                        if str(cid).lower() in message.lower():
                            id_matches.append(cid)

                    if id_matches:
                        await websocket.send_text(json.dumps(
                            make_status_payload("generating", "✍️ Формирую детальный анализ по обращениям...", "active"),
                            ensure_ascii=False
                        ))
                        matched_complaints = [id_to_text_map[cid] for cid in id_matches]
                        try:
                            response_text = await asyncio.to_thread(
                                answer_complaint_details,
                                user_query=message,
                                complaints=matched_complaints,
                                history=history
                            )
                        except Exception as e:
                            logger.error(f"[COMPLAINT FOLLOW_UP] answer_complaint_details failed: {e}")
                            response_text = f"Не удалось извлечь детали обращений: {e}"
                    else:
                        # General follow-up / QA based on all cached complaints
                        await websocket.send_text(json.dumps(
                            make_status_payload("intent", "🔍 Определение намерения...", "active"),
                            ensure_ascii=False
                        ))
                        intent = await asyncio.to_thread(classify_complaint_intent, message)
                        
                        if intent == "search":
                            await websocket.send_text(json.dumps(
                                make_status_payload("search", "🔎 Поиск релевантных обращений...", "active"),
                                ensure_ascii=False
                            ))
                            matched_list = search_complaints_cache(message, id_to_text_map)
                            await websocket.send_text(json.dumps(
                                make_status_payload("generating", "✍️ Формирую аналитический ответ...", "active"),
                                ensure_ascii=False
                            ))
                            try:
                                response_text = await asyncio.to_thread(
                                    answer_complaint_follow_up,
                                    user_query=message,
                                    complaints=matched_list,
                                    history=history
                                )
                            except Exception as e:
                                logger.error(f"[COMPLAINT FOLLOW_UP] answer_complaint_follow_up failed: {e}")
                                response_text = "Произошла ошибка при анализе обращений."
                        else:
                            await websocket.send_text(json.dumps(
                                make_status_payload("generating", "💬 Формирую ответ в режиме диалога...", "active"),
                                ensure_ascii=False
                            ))
                            try:
                                response_text = await asyncio.to_thread(
                                    answer_complaint_dialog,
                                    user_query=message,
                                    history=history
                                )
                            except Exception as e:
                                logger.error(f"[COMPLAINT FOLLOW_UP] answer_complaint_dialog failed: {e}")
                                response_text = "Произошла ошибка при ведении диалога."

                    # Stream text tokens with pacing
                    update_session_history(session_id, message, response_text)

                    def chunk_text(text: str, words_per_chunk: int = 4) -> list[str]:
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

                    for chunk in chunk_text(response_text):
                        await websocket.send_text(json.dumps({
                            "event": "token",
                            "data": {"text": chunk}
                        }, ensure_ascii=False))
                        await asyncio.sleep(0.012)

                    await websocket.send_text(json.dumps(
                        make_status_payload("done", "🟢 Ответ готов", "done"),
                        ensure_ascii=False
                    ))

                    duration_ms = int((time.perf_counter() - started_time) * 1000)
                    await websocket.send_text(json.dumps({
                        "event": "done",
                        "data": {"file_id": None, "duration_ms": duration_ms}
                    }, ensure_ascii=False))

                    # Save history in DB
                    try:
                        with get_db() as db:
                            MessageRepo.add(db, session_id=session_id, role="user", content=message)
                            MessageRepo.add(db, session_id=session_id, role="assistant", content=response_text)
                            logger.info(f"[COMPLAINT] История обновлена для сессии {session_id}")
                    except Exception as db_err:
                        logger.error(f"[COMPLAINT] Ошибка записи в БД: {db_err}")

            except Exception as e:
                logger.error(f"[pipeline] Ошибка: {str(e)}", exc_info=True)
                await websocket.send_text(json.dumps({
                    "event": "error",
                    "data": {"message": f"Ошибка пайплайна: {str(e)}"}
                }))
            await websocket.close()
            return
        
        if is_ior:
            from local_qwen import (
                ask_local_qwen,
                answer_detail_with_qwen,
                answer_follow_up_with_qwen,
                answer_dialog_with_qwen,              # см. ниже - добавить в local_qwen.py
            )
            from backend.IOR_pipeline_search import (
                _SMALL_FAISS_SESSION_CACHE,
                search_small_index,
                build_and_cache_small_index,
                search_pipeline,                      # fallback
                faiss_loaded,
                bm25_indexes,
            )
            from backend.agent.agent_flow import run_agent
            from backend.storage.database import FileRepo, MessageRepo, SessionRepo, get_db

            async def send_fallback_activity(websocket, aid: str, kind: str, title: str, detail: str = None, status: str = "active"):
                payload = {
                    "id": aid,
                    "kind": kind,
                    "title": title,
                    "detail": detail,
                    "status": status
                }
                await websocket.send_text(json.dumps({"event": "activity", "data": payload}, ensure_ascii=False))

            def _get_ior_session(session_id: str) -> dict:
                """Возвращает текущий IOR-кэш сессии."""
                return _SMALL_FAISS_SESSION_CACHE.get(session_id, {})

            def _save_ior_session(session_id: str, data: dict):
                """Сохраняет IOR-кэш сессии."""
                existing = _SMALL_FAISS_SESSION_CACHE.setdefault(session_id, {})
                existing.update(data)

            def _determine_ior_route(session_id: str) -> str:
                """
                Детерминированный роутинг по состоянию RAM-кэша.

                Переходим в follow_up только если:
                - id_to_text_map существует и непустой

                Иначе - analytical (новая выгрузка).
                Падение внутри follow_up НЕ сбрасывает кэш,
                поэтому следующий запрос снова попадет в follow_up.
                """
                session_data = _SMALL_FAISS_SESSION_CACHE.get(session_id, {})
                id_to_text_map = session_data.get("id_to_text_map", {})

                if id_to_text_map and len(id_to_text_map) > 0:
                    logger.debug(
                        f"[IOR ROUTE] session={session_id} -> follow_up "
                        f"(кэш: {len(id_to_text_map)} записей)"
                    )
                    return "follow_up"

                logger.debug(f"[IOR ROUTE] session={session_id} -> analytical (кэш пуст)")
                return "analytical"

            # --------------------------------------------------------------------------
            # Определяем маршрут
            # --------------------------------------------------------------------------
            route = _determine_ior_route(session_id)
            logger.info(f"[IOR ROUTE] session={session_id} -> {route}")

            # ==============================================================================
            # БЛОК 4: ВЕТКА 1: ANALYTICAL - запуск агента и обработка его кэша (IMG_7092)
            # ==============================================================================

            if route == "analytical":
                agent_success = False
                try:
                    logger.info(f"[IOR AGENT] Запуск первичной выгрузки для сессии {session_id}")

                    session_data = _get_ior_session(session_id)
                    # Reset query-specific success markers from previous turns to prevent cache pollution
                    session_data.pop("agent_file_id", None)
                    session_data.pop("target_ids", None)
                    session_data.pop("id_to_text_map", None)
                    _save_ior_session(session_id, session_data)

                    clarify_strikes = session_data.get("clarify_strikes", 0)

                    await relay_to_ws(websocket, run_agent(
                        session_id=session_id,
                        user_message=message,
                        clarify_strikes=clarify_strikes
                    ))

                    # После завершения агента проверяем - записал ли он данные в кэш
                    session_data = _get_ior_session(session_id)
                    print(f"[DEBUG_FULL] session_data keys: {list(session_data.keys())}, last_ask_user_stuck={session_data.get('last_ask_user_stuck')!r}")
                    print(f"[DEBUG_FULL] keys={list(session_data.keys())}")
                    print(f"[DEBUG_FULL] last_ask_user_stuck={session_data.get('last_ask_user_stuck')!r}")
                    print(f"[DEBUG_FULL] stuck_strikes={session_data.get('stuck_strikes')!r}")
                    print(f"[DEBUG_FULL] target_ids={bool(session_data.get('target_ids'))!r}")
                    print(f"[DEBUG_FULL] agent_file_id={session_data.get('agent_file_id')!r}")

                    if session_data.get("target_ids") and session_data.get("id_to_text_map"):
                        logger.info(f"[LAZY_FAISS] Сборка индекса после успешного агента для {session_id}") # noqa: E501

                        build_and_cache_small_index(
                            session_id=session_id,
                            target_ids=session_data["target_ids"],
                            id_to_text_map=session_data["id_to_text_map"]
                        )
                        agent_success = True
                        session_data["stuck_strikes"] = 0
                        _save_ior_session(session_id, session_data)
                        logger.info(
                            f"[IOR AGENT] Выгрузка успешна: "
                            f"{len(session_data['target_ids'])} записей"
                        )

                    # ==============================================================================
                    # БЛОК 5: Аналитический ответ и суммаризация через Qwen (IMG_7093)
                    # ==============================================================================

                    has_results = bool(session_data.get("target_ids") or session_data.get("agent_file_id"))
                    if agent_success and has_results:
                        from backend.agent.state import get_session_state
                        state = get_session_state(session_id)
                        narrative = ""
                        if state.history:
                            for msg_item in reversed(state.history):
                                if msg_item.get("role") == "assistant" and msg_item.get("content"):
                                    narrative = msg_item.get("content")
                                    break
                        update_session_history(session_id, message, narrative or "Выгрузка завершена успешно.")
                        session_data["stuck_strikes"] = 0
                        _save_ior_session(session_id, session_data)
                        logger.info("[IOR AGENT] Выгрузка успешна, история сохранена.")

                    # ==============================================================================
                    # БЛОК 6: Обработка стопоров, уточнений и запуск fallback логики (IMG_7094)
                    # ==============================================================================

                    elif session_data.get("agent_file_id"):
                        # Агент СОЗДАЛ файл-выгрузку, но без построчных описаний
                        # (агрегированная/табличная выгрузка по процессам/ТБ/суммам).
                        # Это УСПЕХ: файл уже отдан клиенту по SSE, агент дал нарратив.
                        # FAISS/суммаризация не нужны, fallback-поиск запускать НЕ надо.
                        agent_success = True
                        session_data["stuck_strikes"] = 0
                        _save_ior_session(session_id, session_data)
                        logger.info(
                            "[IOR AGENT] Файл-выгрузка готова без построчного FAISS "
                            "(агрегат) - успех, fallback не нужен"
                        )
                    elif "last_ask_user_stuck" in session_data and not session_data["last_ask_user_stuck"]:
                        # Обычное диалоговое уточнение (не технический затык) не должно приводить к fallback
                        session_data["clarify_strikes"] = 0
                        _save_ior_session(session_id, session_data)
                        agent_success = True
                        logger.info("[IOR AGENT] Обычное уточнение/вопрос к пользователю - fallback не нужен")
                    elif session_data.get("last_ask_user_stuck"):
                        strikes = session_data.get("stuck_strikes", 0) + 1
                        if strikes < 2:
                            # Первый стопор, даем пользователю шанс уточнить
                            # Не запускаем fallback
                            session_data["stuck_strikes"] = strikes
                            _save_ior_session(session_id, session_data)
                            agent_success = True
                            logger.info(f"[IOR AGENT] Технический стопор #{strikes}, ждем уточнения пользователя")
                        else:
                            # Пользователь уточнил, но все равно не вышло
                            session_data["stuck_strikes"] = 0
                            session_data["last_ask_user_stuck"] = False
                            _save_ior_session(session_id, session_data)
                            logger.info(f"[IOR AGENT] Повторный технический стопор - уходим в fallback")
                    else:
                        logger.warning("[IOR AGENT] Агент завершился, но данные в каше не найдены")

                except Exception as agent_err:
                    logger.error(f"[IOR AGENT] Агент упал: {agent_err}", exc_info=True)

                # --- Попытка 2 (fallback): search_pipeline ---
                if not agent_success:
                    logger.info("[IOR AGENT] Запускаю fallback на search_pipeline...")

                    # Задаем уникальные ID для шагов анимации на фронте
                    fb_search_id = "fb_search_operation"
                    fb_spark_id = "fb_spark_export"
                    fb_summary_id = "fb_summary_operation"

                    # функция-хелпер для отправки шагов в UI-компонент вашего коллеги
                    async def send_fb_activity(aid: str, kind: str, title: str, detail: str = None, status: str = "active"):
                        await websocket.send_text(json.dumps({
                            "event": "activity",
                            "data": {
                                "id": aid,
                                "kind": kind,       # "action" или "think"
                                "title": title,
                                "detail": detail,
                                "status": status    # "active", "done", "failed"
                            }
                        }, ensure_ascii=False))

                    try:
                        # ШАГ 1: Запускаем семантический поиск по FAISS/BM25 (IMG_7095)
                        await send_fb_activity(
                            aid=fb_search_id,
                            kind="action",
                            title="Запуск резервного поиска",
                            detail="Агент не смог сформировать специфику. Ищу через гибридный поиск по векторам и тексту...",
                            status="active"
                        )

                        params = extract_search_params(message)

                        await send_fb_activity(
                            fb_search_id, "action", "Поиск в глобальном индексе ИОР",
                            f"Запрос к FAISS/BM25: '{params.get('query') or message}'", "active"
                        )

                        df_result = run_ior_pipeline(
                            query=params.get("query") or message,
                            faiss_idx=faiss_loaded,
                            bm25_indexes=bm25_indexes,
                            top_k=params.get("top_k"),
                            date_range=params.get("date_range"),
                        )

                        if df_result is not None and not df_result.empty and "incident_sid" in df_result.columns:
                            target_ids = [str(x) for x in df_result["incident_sid"].dropna().unique().tolist()]

                            id_to_text_map = dict(zip(df_result["incident_sid"].astype(str), df_result["Текст_ИОР"].astype(str)))
                            _SMALL_FAISS_SESSION_CACHE[session_id] = {
                                "target_ids": target_ids,
                                "id_to_text_map": id_to_text_map
                            }

                            build_and_cache_small_index(session_id, target_ids, id_to_text_map)

                            await send_fb_activity(fb_search_id, "action", "Поиск завершен", f"Найдено {len(target_ids)} релевантных инцидентов.", "done")

                            # ШАГ 2: Поднимаем Спарк и выкачиваем полные 67+ колонок (IMG_7095 / IMG_7096)
                            await send_fb_activity(
                                aid=fb_spark_id,
                                kind="action",
                                title="Формирование Excel",
                                detail="Подключаемся к Hive/HDFS для выгрузки всех аналитических колонок по инцидентам...",
                                status="active"
                            )

                            # ==============================================================================
                            # БЛОК 7: Интеграция со Spark и сохранение полной выгрузки (IMG_7096)
                            # ==============================================================================

                            spark = get_spark_session()
                            table_name = "arnsdpsbx_t_team_sva_oarb_4.d6_base_of_knowledge_ior"
                            df_spark = spark.table(table_name)
                            # Поиск колонки incdnt_sid без учета регистра в Spark
                            incdnt_sid_col = next((c for c in df_spark.columns if c.lower() == "incdnt_sid"), "incdnt_sid")
                            df_filtered = df_spark.filter(df_spark[incdnt_sid_col].isin(target_ids))
                            df_pandas = await asyncio.to_thread(df_filtered.toPandas)
                            try:
                                df_pandas.columns = [str(c).lower() for c in df_pandas.columns]
                            except Exception as e:
                                logger.warning("[DIRECT_EXPORT] Failed to normalize columns: %s", e)
                            total_rows = len(df_pandas)
                            logger.info(f"[DIRECT_EXPORT] Загружено из Spark: {total_rows}")

                            df_pandas = df_pandas.replace({pd.NaT: None, np.nan: None})

                            # Сохранение файлов на диск
                            output_dir = "/home/datalab/nfs/ior_assistant/data/generated_files"
                            os.makedirs(output_dir, exist_ok=True)

                            import datetime as dt
                            ts_now = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
                            filename = f"Выгрузка_ИОР_{ts_now}"
                            excel_path = os.path.join(output_dir, filename)

                            # Регистрируем файлы в FileRepo БД
                            file_id = str(uuid.uuid4())
                            data_dir = Path("/home/datalab/nfs/ior_assistant/data/generated_files")
                            xlsx_path = data_dir / f"{file_id}.xlsx"
                            csv_path = data_dir / f"{file_id}.csv"

                            # Запись на NFS диски (фикс 1.3: пишем df_pandas, а не узкий df_result)
                            await asyncio.to_thread(df_pandas.to_excel, xlsx_path, index=False)
                            await asyncio.to_thread(df_pandas.to_csv, csv_path, index=False, encoding="utf-8")

                            try:
                                with get_db() as db:
                                    f = FileRepo.add(
                                        db,
                                        session_id=session_id,
                                        file_path=str(xlsx_path),
                                        file_name=f"osiris_{params.get('query')[:30]}.xlsx",
                                        size_bytes=xlsx_path.stat().st_size,
                                        total_rows=len(df_pandas),
                                        status="ready"
                                    )
                                    FileRepo.update_progress(
                                        db,
                                        file_id=f.id,
                                        csv_path=str(csv_path),
                                        status="ready"
                                    )
                                    file_id = f.id
                            except Exception as db_file_err:
                                logger.error(f"[IOR FALLBACK] Ошибка сохранения файла в БД: {db_file_err}")
                                file_id = file_id

                            # Закрываем шаг Спарка как успешный
                            await send_fb_activity(fb_spark_id, "action", "Excel готов", f"Сформирован полный файл на {total_rows} строк.", "done")

                            # ==============================================================================
                            # БЛОК 8: Предобработка строк, отправка файла и шаг суммаризации (IMG_7097)
                            # ==============================================================================

                            # Отправляем файл на скачивание (появление плашки XLSX в интерфейсе)
                            sample_headers = list(df_pandas.columns)
                            sample_data_rows = df_pandas.head(5).values.tolist()

                            sample = []
                            for row in sample_data_rows:
                                processed_row = []
                                for v in row:
                                    if v is None:
                                        processed_row.append("")
                                    elif hasattr(v, 'isoformat'):
                                        processed_row.append(v.isoformat())
                                    elif isinstance(v, float) and v != v:
                                        processed_row.append("")
                                    elif len(str(v)) > 35:
                                        processed_row.append(str(v)[:35] + "...")
                                    else:
                                        processed_row.append(str(v))
                                sample.append(processed_row)

                            file_event_payload = {
                                "event": "file",
                                "data": {
                                    "file_id": file_id,
                                    "name": f"osiris_{params.get('query')[:30]}.xlsx ({len(df_pandas)} ИОРов)",
                                    "rows": len(df_pandas),
                                    "columns": len(df_pandas.columns),
                                    "sample_headers": sample_headers,
                                    "sample": sample,
                                    "has_csv": True,
                                    "status": "ready"
                                }
                            }

                            # ШАГ 3: Запускаем Qwen для генерации суммаризации (Возвращаем её!)
                            await send_fb_activity(
                                aid=fb_summary_id,
                                kind="think",
                                title="Выполняем суммаризацию...",
                                detail="Локальная модель Qwen обрабатывает смысловые описания инцидентов операционного риска...",
                                status="active"
                            )

                            descriptions_list = [str(text) for sid, text in id_to_text_map.items()]
                            summary_text_raw = ""
                            summary_label = ""

                            if descriptions_list:
                                try:
                                    qwen_dict_response = await asyncio.to_thread(
                                        summarize_iors,
                                        topic=message,
                                        descriptions=descriptions_list[:25] # Защита от OOM
                                    )

                                    # ==============================================================================
                                    # БЛОК 9: Стриминг токенов суммаризации и запись истории в БД (IMG_7098)
                                    # ==============================================================================

                                    if isinstance(qwen_dict_response, dict):
                                        summary_text_raw = qwen_dict_response.get("summary", "Не удалось извлечь текст.")
                                        summary_label = "summary_found" if qwen_dict_response.get("answer_found") else "no_summary"
                                    else:
                                        summary_text_raw = str(qwen_dict_response)
                                except Exception as qwen_err:
                                    logger.error(f"[IOR FALLBACK] Ошибка в summarize_iors: {qwen_err}")
                                    summary_text_raw = "Не удалось сгенерировать суммаризацию результатов поиска."

                            # Стриминг ответа порциями (токенами), чтобы текст красиво печатался
                            chunk_size = 4
                            for i in range(0, len(summary_text_raw), chunk_size):
                                chunk = summary_text_raw[i:i + chunk_size]
                                await websocket.send_text(json.dumps({
                                    "event": "token",
                                    "data": {"text": chunk}
                                }, ensure_ascii=False))
                                await asyncio.sleep(0.05)

                            # Отправляем файл на скачивание (появление плашки XLSX в интерфейсе) после генерации текста
                            if file_event_payload:
                                await websocket.send_text(json.dumps(file_event_payload, ensure_ascii=False))

                            # Закрываем шаг суммаризации
                            await send_fb_activity(fb_summary_id, "think", "Анализ завершен", "Краткий отчет выведен в интерфейс.", "done")

                            # Запись истории сообщений в БД
                            try:
                                with get_db() as db:
                                    MessageRepo.add(db, session_id=session_id, role="user", content=message)
                                    excel_meta = {
                                        "name": f"osiris_{params.get('query')[:30]}.xlsx ({len(df_pandas)} ИОРов)",
                                        "size": f"{xlsx_path.stat().st_size / (1024 * 1024):.1f} МБ" if xlsx_path.exists() else "-",
                                        "rows": len(df_pandas),
                                        "columns": len(df_pandas.columns),
                                        "sample_headers": sample_headers,
                                        "sample": sample,
                                        "has_csv": True,
                                        "status": "ready"
                                    }
                                    MessageRepo.add(db, session_id=session_id, role="assistant",
                                                    content=f"Найдено {len(df_result)} инцидентов. Выполнен поиск по базе.",
                                                    meta={"file_id": file_id, "excel": excel_meta})
                                    SessionRepo.update_title(db, session_id=session_id, title=params.get('query')[:50])
                                    SessionRepo.touch(db, session_id=session_id)
                            except Exception as db_msg_err:
                                logger.error(f"[IOR FALLBACK] Ошибка записи сообщений в БД: {db_msg_err}")

                            update_session_history(session_id, message, summary_text_raw)
                            agent_success = True
                            logger.info(f"[IOR FALLBACK] Найдено {len(target_ids)} записей через search_pipeline.")

                        else:
                            # Если база вернула пустоту
                            await send_fb_activity(fb_search_id, "action", "Поиск завершен", "В базе данных не найдено подходящих инцидентов.", "done")
                            logger.warning("[IOR FALLBACK] Не удалось определить колонки или df_result пуст")

                    except Exception as fb_err:
                        logger.error(f"[IOR FALLBACK] search_pipeline упал: {fb_err}", exc_info=True)
                        await send_fb_activity(fb_search_id, "action", "Критическая ошибка поиска", str(fb_err), "failed")

                if not agent_success:
                    await websocket.send_text(json.dumps({
                        "event": "error",
                        "data": {"message": "Не удалось выполнить выгрузку. Попробуйте переформулировать запрос."}
                    }, ensure_ascii=False))
                await websocket.close()
                return
            
            # ==============================================================================
# ВЕТКА 2: FOLLOW_UP – единый чат (бывшие detail + follow_up + followup_dialog)
# ==============================================================================
            elif route == "follow_up":
                import time
                from local_qwen import classify_intent_with_qwen
                session_data = _get_ior_session(session_id)

                logger.info(f"[IOR_DEBUG] session_data keys: {list(session_data.keys())}")
                logger.info(f"[IOR_DEBUG] 'index' in session data: {'index' in session_data}")
                logger.info(f"[IOR_DEBUG] id_to_text_map len: {len(session_data.get('id_to_text_map', {}))}")
                id_to_text_map: dict = session_data.get("id_to_text_map", {})
                history = get_session_history(session_id)

                timeline = []
                started_time = time.perf_counter()

                def _ts() -> str:
                    return f"+{time.perf_counter() - started_time:0.1f}s"

                def make_status_payload(step_id: str, label: str, status: str = "active") -> dict:
                    for t in timeline:
                        if t.get("status") == "active":
                            t["status"] = "done"
                    timeline.append({
                        "step": step_id,
                        "label": label,
                        "time": _ts(),
                        "status": status
                    })
                    return {"event": "status", "data": {"steps": list(timeline)}}

                # 1. Начало обработки
                await websocket.send_text(json.dumps(
                    make_status_payload("thinking", "💬 Анализирую запрос...", "active"),
                    ensure_ascii=False
                ))
                await asyncio.sleep(0.05)

                qwen_response = None

                # Случай А: EVE-ID в запросе
                eve_matches = re.findall(r'EVE-\d+', message, re.IGNORECASE)
                if eve_matches:
                    await websocket.send_text(json.dumps(
                        make_status_payload("generating", "✍️ Формирую подробный ответ по инцидентам...", "active"),
                        ensure_ascii=False
                    ))
                    ior_texts = {
                        sid: id_to_text_map[sid]
                        for sid in eve_matches
                        if sid in id_to_text_map
                    }
                    if ior_texts:
                        logger.info(f"[IOR FOLLOW_UP] EVE-режим: {list(ior_texts.keys())}")
                        try:
                            formatted_ior_string = ""
                            for sid, text in ior_texts.items():
                                formatted_ior_string += f"--- ИОР ID: {sid} ---\n{text}\n\n\n"

                            qwen_response = await asyncio.to_thread(
                                answer_detail_with_qwen,
                                user_query=message,
                                ior_texts=formatted_ior_string,
                                history=history,
                            )
                        except Exception as e:
                            logger.error(f"[IOR FOLLOW_UP] answer_detail_with_qwen упал: {e}")
                            # Fallback: строим строку вручную и зовем ask_local_qwen
                            formatted = "\n".join(
                                f"--- ИОР ID: {sid} ---\n{text}\n\n\n"
                                for sid, text in ior_texts.items()
                            )
                            try:
                                qwen_response = await asyncio.to_thread(
                                    ask_local_qwen,
                                    [
                                        {"role": "system", "content": (
                                            "Ты ИИ-аналитик системы ИОР. "
                                            "Проанализируй предоставленные тексты инцидентов и ответь на вопрос."
                                        )},
                                        {"role": "user", "content": (
                                            f"Вопрос: {message}\n\nТексты инцидентов:\n{formatted}"
                                        )},
                                    ],
                                    1500,
                                )
                            except Exception as e2:
                                logger.error(f"[IOR FOLLOW_UP] ask_local_qwen fallback упал: {e2}")
                                qwen_response = "Не удалось обработать запрос по инциденту. Попробуйте ещё раз."
                    else:
                        # ID есть в запросе, но не в кэше сессии
                        qwen_response = (
                            f"ID {', '.join(eve_matches)} не найдены в выгрузке текущей сессии. "
                            "Уточните запрос."
                        )

                # Случай Б: FAISS-поиск (EVE-ID не найден)
                elif "index" in session_data:
                    # Сначала определяем интент пользователя
                    await websocket.send_text(json.dumps(
                        make_status_payload("intent", "🔍 Определение намерения...", "active"),
                        ensure_ascii=False
                    ))
                    
                    intent = "search"
                    try:
                        intent = await asyncio.to_thread(classify_intent_with_qwen, message)
                        logger.info(f"[IOR FOLLOW_UP] Classified intent for message '{message[:50]}': {intent}")
                    except Exception as e:
                        logger.error(f"[IOR FOLLOW_UP] Failed to classify intent: {e}")
                        intent = "search"  # fallback
                    
                    if intent == "search":
                        await websocket.send_text(json.dumps(
                            make_status_payload("search", "🔎 Поиск релевантных инцидентов...", "active"),
                            ensure_ascii=False
                        ))
                        try:
                            retrieved = search_small_index(
                                session_id=session_id,
                                query=message,
                                threshold=0.5,
                                max_candidates=25,
                            )
                            if retrieved:
                                descriptions = [{"id": r["id"], "text": r["text"]} for r in retrieved]
                                logger.info(f"[IOR FOLLOW_UP] FAISS нашёл {len(descriptions)} релевантных")
                                
                                await websocket.send_text(json.dumps(
                                    make_status_payload("generating", "✍️ Формирую аналитический ответ...", "active"),
                                    ensure_ascii=False
                                ))
                                qwen_response = await asyncio.to_thread(
                                    answer_follow_up_with_qwen,
                                    user_query=message,
                                    descriptions=descriptions,
                                    history=history,
                                )
                            else:
                                logger.info("[IOR FOLLOW_UP] FAISS ничего не нашёл -> чистый диалог")
                        except Exception as e:
                            logger.error(f"[IOR FOLLOW_UP] search_small_index упал: {e}")

                # Случай В: нет индекса или FAISS ничего не вернул или интент = chat -> просто чат
                if qwen_response is None:
                    await websocket.send_text(json.dumps(
                        make_status_payload("generating", "💬 Формирую ответ в режиме диалога...", "active"),
                        ensure_ascii=False
                    ))
                    try:
                        if history:
                            qwen_response = await asyncio.to_thread(
                                answer_dialog_with_qwen,
                                user_query=message,
                                history=history,
                            )
                        else:
                            qwen_response = await asyncio.to_thread(
                                answer_follow_up_with_qwen,
                                user_query=message,
                                descriptions=[],
                                history=[],
                            )
                    except Exception as e:
                        logger.error(f"[IOR FOLLOW_UP] ask_local_qwen упал: {e}")
                        qwen_response = "Произошла ошибка. Попробуйте ещё раз."

                # Приводим к строке
                if isinstance(qwen_response, dict):
                    summary_text = qwen_response.get("summary", str(qwen_response))
                else:
                    summary_text = str(qwen_response) if qwen_response else "Нет ответа."

                # Стриминг
                update_session_history(session_id, message, summary_text)

                # Построчное разбиение по словам для плавного и красивого стриминга (как в analytical агенте)
                def chunk_text(text: str, words_per_chunk: int = 4) -> list[str]:
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

                for chunk in chunk_text(summary_text):
                    await websocket.send_text(json.dumps({
                        "event": "token",
                        "data": {"text": chunk}
                    }, ensure_ascii=False))
                    await asyncio.sleep(0.012)

                await websocket.send_text(json.dumps(
                    make_status_payload("done", "🟢 Ответ готов", "done"),
                    ensure_ascii=False
                ))

                duration_ms = int((time.perf_counter() - started_time) * 1000)
                await websocket.send_text(json.dumps({
                    "event": "done",
                    "data": {"file_id": None, "duration_ms": duration_ms}
                }, ensure_ascii=False))

                # Сохраняем в БД
                try:
                    with get_db() as db:
                        MessageRepo.add(db, session_id=session_id, role="user", content=message)
                        MessageRepo.add(db, session_id=session_id, role="assistant", content=summary_text)
                        logger.info(f"[IOR] История обновлена для сессии {session_id}")
                except Exception as db_err:
                    logger.error(f"[IOR] Ошибка записи в БД: {db_err}")

                return

        # --- Phase 4.1: фоновый слушатель cancel-frames от клиента ---
        # Параллельно с relay_to_ws слушаем WS на предмет {"cancel":true}
        # frame'а — если приходит, вызываем runner.cancel_session() ->
        # spark.sparkContext.cancelJobGroup() прерывает идущий job.
        async def _listen_for_cancel():
            try:
                while True:
                    msg = await websocket.receive_text()
                    try:
                        data = json.loads(msg)
                    except Exception:
                        continue

                    if data.get("cancel"):
                        runner = get_runner()
                        was_cancelled = runner.cancel_session(session_id)
                        try:  # ПБ: останавливаем и ReAct-контроллер (не только spark job)
                            from backend.agent.state import get_session_state
                            get_session_state(session_id).cancelled = True
                        except Exception:  # noqa: BLE001
                            pass
                        logger.info("[ws] cancel request session=%s result=%s",
                                    session_id, was_cancelled)
                        try:
                            await websocket.send_text(json.dumps({
                                "event": "cancelled",
                                "data": {"was_active": was_cancelled},
                            }))
                        except Exception:
                            pass
            except WebSocketDisconnect:
                return
            except Exception:
                return

        cancel_listener_task = asyncio.create_task(_listen_for_cancel())

        # Переиспользуем run_agent() из flow.py — он async-генератор,
        # yield-ит SSE-formatted строки. relay_to_ws парсит их и шлёт
        # каждую как WebSocket text-frame.

        await relay_to_ws(websocket, run_agent(
            session_id=session_id,
            user_message=message,
        ))
        await websocket.close()
    except WebSocketDisconnect:
        logger.info("[ws] client disconnected during stream")
        return
    except Exception as exc:
        logger.exception("[ws] error: %s", exc)
        try:
            await websocket.send_text(json.dumps({
                "event": "error",
                "data": {"message": f"Ошибка: {exc}"},
            }))
        except Exception:
            pass
        await websocket.close()
    except Exception:
        pass
    finally:
        if cancel_listener_task is not None:
            cancel_listener_task.cancel()
            try:
                await cancel_listener_task
            except (asyncio.CancelledError, Exception):
                pass
            