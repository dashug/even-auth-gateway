"""飞书 WebSocket 长连接:注册离职事件 + 审批卡片回调,免公网 URL。

复用 CS Hub channels/feishu_ws.py 验证过的核心模式:ws.Client 的事件循环在
模块导入时被 lark-oapi 捕获一次,必须在线程自己的 loop 里跑之前 monkey-patch
`lark_oapi.ws.client.loop`,否则在 uvloop 下会死锁。这里只做最小实现——
只订阅 contact.user.* 离职事件 + card.action.trigger,不处理消息事件。

导入本模块不需要飞书凭据;凭据只在 start_ws() 调用时读取。
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time as _time
from typing import Optional

logger = logging.getLogger(__name__)

_ws_client = None
_ws_thread: Optional[threading.Thread] = None
_ws_stop_event: Optional[threading.Event] = None
_ws_loop: Optional[asyncio.AbstractEventLoop] = None
_main_loop: Optional[asyncio.AbstractEventLoop] = None

_WS_RECONNECT_DELAY_MIN = 10
_WS_RECONNECT_DELAY_MAX = 120


def _on_deleted(data) -> None:
    """contact.user.deleted_v3 — schedule the async handler onto main_loop."""
    from even_auth_gov import ws_events

    try:
        if _main_loop is not None and _main_loop.is_running():
            asyncio.run_coroutine_threadsafe(ws_events.on_user_deleted(data), _main_loop)
        else:
            logger.warning("feishu_ws: main_loop not running, dropping contact.user.deleted_v3 event")
    except Exception:
        logger.exception("feishu_ws: _on_deleted scheduling error")


def _on_updated(data) -> None:
    """contact.user.updated_v3 — schedule the async handler onto main_loop."""
    from even_auth_gov import ws_events

    try:
        if _main_loop is not None and _main_loop.is_running():
            asyncio.run_coroutine_threadsafe(ws_events.on_user_updated(data), _main_loop)
        else:
            logger.warning("feishu_ws: main_loop not running, dropping contact.user.updated_v3 event")
    except Exception:
        logger.exception("feishu_ws: _on_updated scheduling error")


async def _process_card(data) -> None:
    """Async helper: call decision.handle with a fresh casdoor client."""
    import httpx
    from even_auth_gov import decision, settings

    event = data.event
    action_obj = event.action if event else None
    operator = event.operator if event else None
    value = (action_obj.value if action_obj else None) or {}
    if not isinstance(value, dict):
        value = {}

    operator_id = operator.open_id if operator else ""
    async with httpx.AsyncClient(base_url=settings.casdoor_endpoint()) as client:
        await decision.handle(
            value.get("action", ""),
            operator_id=operator_id,
            owner=settings.approver_feishu_id(),
            sso_open_id=value.get("sso_open_id", ""),
            client=client,
        )


def _on_card(data):
    """card.action.trigger — schedule decision.handle onto main_loop.

    Returns a simple toast response immediately for the clicker (mirrors
    cshub's _handle_card_action_event shape); the actual decision work runs
    async on main_loop.
    """
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        CallBackToast,
        P2CardActionTriggerResponse,
    )

    try:
        if _main_loop is not None and _main_loop.is_running():
            asyncio.run_coroutine_threadsafe(_process_card(data), _main_loop)
        else:
            logger.warning("feishu_ws: main_loop not running, dropping card action event")
    except Exception:
        logger.exception("feishu_ws: _on_card scheduling error")

    resp = P2CardActionTriggerResponse()
    resp.toast = CallBackToast()
    resp.toast.type = "info"
    resp.toast.content = "处理中…"
    return resp


def start_ws(main_loop: asyncio.AbstractEventLoop) -> None:
    """Start the Feishu WebSocket client on a daemon thread.

    No-ops (logs + returns) if FEISHU_APP_ID/FEISHU_APP_SECRET are unset —
    import of this module never requires credentials, only calling this does.
    """
    global _ws_thread, _ws_stop_event, _main_loop

    app_id = os.getenv("FEISHU_APP_ID", "")
    app_secret = os.getenv("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        logger.info("FEISHU_APP_ID/APP_SECRET not set, Feishu WS disabled")
        return
    if _ws_thread is not None and _ws_thread.is_alive():
        logger.info("Feishu WS thread already running")
        return

    _main_loop = main_loop
    encrypt_key = os.getenv("FEISHU_ENCRYPT_KEY", "")
    verification_token = os.getenv("FEISHU_VERIFICATION_TOKEN", "")
    _ws_stop_event = threading.Event()

    import lark_oapi as lark

    event_handler = (
        lark.EventDispatcherHandler.builder(encrypt_key, verification_token)
        .register_p2_contact_user_deleted_v3(_on_deleted)
        .register_p2_contact_user_updated_v3(_on_updated)
        .register_p2_card_action_trigger(_on_card)
        .build()
    )

    async def _run_ws_client_async(client) -> None:
        """Drive ws.Client's lifecycle inside ONE coroutine/run_until_complete call.

        The SDK's own `Client.start()` is NOT used here: it calls
        `loop.run_until_complete(self._connect())`, then a bare
        `loop.create_task(self._ping_loop())` (while the loop is technically
        not "running" between the two run_until_complete calls), then
        `loop.run_until_complete(_select())` again. That mixing of
        run_until_complete + create_task deadlocks under uvloop. Driving
        everything from inside a single outer coroutine (so every
        create_task happens while the loop IS running) avoids that.
        """
        import lark_oapi.ws.client as ws_mod
        ws_mod.loop = asyncio.get_running_loop()

        # Bind the lock to the loop that's actually running now.
        client._lock = asyncio.Lock()
        client._auto_reconnect = False  # outer thread loop owns reconnect

        await client._connect()
        if client._conn is None:
            raise RuntimeError("Feishu WS (gateway) connect failed")

        ping_task = asyncio.create_task(client._ping_loop())
        try:
            while not (_ws_stop_event and _ws_stop_event.is_set()):
                await asyncio.sleep(5)
                if client._conn is None:
                    logger.warning("Feishu WS (gateway): connection lost, reconnecting")
                    return
            logger.info("Feishu WS (gateway) stop requested — closing current connection")
        finally:
            if not ping_task.done():
                ping_task.cancel()
            await asyncio.gather(ping_task, return_exceptions=True)
            try:
                await client._disconnect()
            except Exception:
                pass

    def _ws_thread_fn():
        global _ws_client, _ws_loop, _ws_thread

        import lark_oapi.ws.client as ws_mod
        from lark_oapi import ws

        reconnect_delay = _WS_RECONNECT_DELAY_MIN

        while not (_ws_stop_event and _ws_stop_event.is_set()):
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            # REQUIRED: lark-oapi's ws.Client captures the event loop at
            # module import time. Without this monkey-patch the WS deadlocks
            # under uvloop.
            ws_mod.loop = new_loop
            _ws_loop = new_loop

            connect_start = _time.monotonic()
            connection_duration = 0.0
            try:
                client = ws.Client(
                    app_id=app_id,
                    app_secret=app_secret,
                    event_handler=event_handler,
                    log_level=lark.LogLevel.WARNING,
                    auto_reconnect=False,
                )
                _ws_client = client
                logger.info("Feishu WS (gateway) connecting...")
                new_loop.run_until_complete(_run_ws_client_async(client))
            except Exception as e:
                logger.error("Feishu WS (gateway) thread error: %s", e)
            finally:
                _ws_client = None
                connection_duration = _time.monotonic() - connect_start
                try:
                    new_loop.close()
                except Exception:
                    pass
                _ws_loop = None

            if _ws_stop_event and _ws_stop_event.is_set():
                break

            if connection_duration > 60:
                reconnect_delay = _WS_RECONNECT_DELAY_MIN
            else:
                reconnect_delay = min(reconnect_delay * 2, _WS_RECONNECT_DELAY_MAX)

            logger.info("Feishu WS (gateway) reconnecting in %ds...", reconnect_delay)
            if _ws_stop_event and _ws_stop_event.wait(reconnect_delay):
                break

        _ws_thread = None

    t = threading.Thread(target=_ws_thread_fn, daemon=True, name="feishu-ws-gateway")
    _ws_thread = t
    t.start()
    logger.info("Feishu WebSocket client thread started (offboarding events + card callback)")


def stop_ws() -> None:
    """Stop the Feishu WebSocket client."""
    global _ws_client, _ws_thread, _ws_stop_event

    if _ws_stop_event is not None:
        _ws_stop_event.set()
    loop = _ws_loop
    client = _ws_client
    if loop is not None and client is not None and loop.is_running():
        async def _disconnect():
            try:
                await client._disconnect()
            except Exception:
                pass

        try:
            asyncio.run_coroutine_threadsafe(_disconnect(), loop)
        except Exception:
            pass
    if _ws_thread is not None and _ws_thread.is_alive():
        _ws_thread.join(timeout=5)
    _ws_client = None
    _ws_thread = None
    logger.info("Feishu WebSocket client (gateway) stopped")
