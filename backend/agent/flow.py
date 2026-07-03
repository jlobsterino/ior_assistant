"""
WS/SSE transport-helpers.

Этот модуль больше НЕ содержит бизнес-логики чата – она вся в
`backend.agent.agent_flow.run_agent()`. Здесь только утилиты для
WebSocket-relay и SSE-fallback'а:

* sse(event, data)        - формат `event: ...\ndata: ...\n\n`
* SSE_INITIAL_PADDING /   - anti-buffer для прокси/WAF
  SSE_KEEPALIVE
* with_heartbeat(...)     - обёртка для SSE-fallback'а
* relay_to_ws(...)        - SSE -> WebSocket-frame'ы
* _safe_send_text         - защита от send-after-close
* _ws_heartbeat           - пинг WS чтобы WAF не закрыл idle
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncIterator

logger = logging.getLogger(__name__)


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


_INITIAL_PAD_BYTES = 16 * 1024
_KEEPALIVE_BYTES = 2 * 1024

SSE_INITIAL_PADDING = ":" + (" " * _INITIAL_PAD_BYTES) + "\n\n"
SSE_KEEPALIVE = ":" + ("k" * _KEEPALIVE_BYTES) + "\n\n"


# ——— relay_to_ws + ws_heartbeat ——————————————————————————————————————


async def _safe_send_text(websocket, payload: str) -> bool:
    """Шлёт WS text-frame, возвращает False если соединение уже закрыто."""
    from starlette.websockets import WebSocketState
    if (getattr(websocket, "application_state", None) == WebSocketState.DISCONNECTED
            or getattr(websocket, "client_state", None) == WebSocketState.DISCONNECTED):
        return False
    try:
        await websocket.send_text(payload)
        return True
    except RuntimeError:
        return False
    except Exception as exc:  # noqa: BLE001
        logger.debug("[ws] send failed: %s", exc)
        return False


async def relay_to_ws(websocket, sse_iter: AsyncIterator[str]) -> None:
    """Парсит SSE-stream и шлёт каждый event как WS text-frame
    `{"event": ..., "data": {...}}`."""
    import json as _json
    hb_task = asyncio.create_task(_ws_heartbeat(websocket, 5.0))
    buffer = ""
    try:
        async for sse_chunk in sse_iter:
            buffer += sse_chunk
            while "\n\n" in buffer:
                block, buffer = buffer.split("\n\n", 1)
                event_name = ""
                data_parts: list[str] = []
                for line in block.split("\n"):
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        data_parts.append(line[5:].lstrip())
                if not data_parts:
                    continue
                raw_data = "\n".join(data_parts)
                try:
                    data_obj = _json.loads(raw_data)
                except Exception:
                    data_obj = raw_data
                ok = await _safe_send_text(websocket, _json.dumps(
                    {"event": event_name or "message", "data": data_obj},
                    ensure_ascii=False,
                ))
                if not ok:
                    return
    finally:
        hb_task.cancel()
        try:
            await hb_task
        except (asyncio.CancelledError, Exception):
            pass


async def _ws_heartbeat(websocket, interval: float) -> None:
    """Пинг WebSocket каждые `interval` сек."""
    import json as _json
    try:
        while True:
            await asyncio.sleep(interval)
            ok = await _safe_send_text(websocket, _json.dumps(
                {"event": "ping", "data": {}}, ensure_ascii=False,
            ))
            if not ok:
                return
    except asyncio.CancelledError:
        return


# ——— with_heartbeat – SSE-fallback обёртка ——————————————————————————

_SENTINEL_DONE = object()


async def with_heartbeat(inner: AsyncIterator[str],
                         interval: float = 1.0) -> AsyncIterator[str]:
    """Гарантирует чанк не реже чем раз в `interval` сек (SSE-keepalive
    + 16KB initial padding для пробивки proxy-буфера)."""
    queue: asyncio.Queue = asyncio.Queue()

    async def _producer():
        try:
            async for chunk in inner:
                await queue.put(chunk)
        except Exception as exc:  # noqa: BLE001
            await queue.put(("__error__", exc))
            return
        await queue.put(_SENTINEL_DONE)

    producer_task = asyncio.create_task(_producer())
    yield SSE_INITIAL_PADDING
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=interval)
            except asyncio.TimeoutError:
                yield SSE_KEEPALIVE
                continue
            if item is _SENTINEL_DONE:
                return
            if isinstance(item, tuple) and item and item[0] == "__error__":
                raise item[1]
            yield item
    finally:
        if not producer_task.done():
            producer_task.cancel()
            try:
                await producer_task
            except (asyncio.CancelledError, Exception):
                pass