"""飞书 contact WS 事件 → 离职引擎。判定复用 offboard.offboard_flags。"""
from __future__ import annotations
import logging, os
import httpx
from even_auth_gov import offboard

logger = logging.getLogger(__name__)

def _client():
    return httpx.AsyncClient(base_url=os.getenv("CASDOOR_ENDPOINT", "http://127.0.0.1:8000"))

async def on_user_deleted(data) -> None:
    try:
        obj = data.event.object if data.event else None
        if obj is not None:
            async with _client() as c:
                await offboard.apply(getattr(obj, "open_id", "") or "", getattr(obj, "name", "") or "",
                                     "offboard_deleted", c)
    except Exception as e:
        logger.exception("on_user_deleted error: %s", e)

async def on_user_updated(data) -> None:
    try:
        ev = data.event
        obj = ev.object if ev else None
        old = getattr(ev, "old_object", None) if ev else None
        if obj is None:
            return
        now_off = offboard.offboard_flags(getattr(obj, "status", None))
        was_off = offboard.offboard_flags(getattr(old, "status", None)) if old is not None else False
        if now_off and not was_off:
            async with _client() as c:
                await offboard.apply(getattr(obj, "open_id", "") or "", getattr(obj, "name", "") or "",
                                     "offboard_event", c)
    except Exception as e:
        logger.exception("on_user_updated error: %s", e)
