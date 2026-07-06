"""治理服务 FastAPI 组装:webhook 路由 + 飞书卡片回调。
飞书集成与配置均为网关自带(even_auth_gov.feishu / .settings),不依赖 cshub。

Lifespan wires up two background pieces (skipped under TESTING so existing
app-wiring tests stay green):
  ② Feishu WS long-connection — offboarding events + card callback
  ③ Daily reconcile scheduler
"""
from __future__ import annotations
import asyncio
import contextlib
import os
import httpx
from fastapi import FastAPI, Request, Response
from even_auth_gov import webhook as wh, decision, settings, feishu_ws, scheduler

def _casdoor_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=settings.casdoor_endpoint())

def _owner() -> str:
    return settings.approver_feishu_id()

def _truthy(val: str) -> bool:
    return val.strip().lower() not in ("", "0", "false", "no")

@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI):
    testing = bool(os.getenv("TESTING"))
    if not testing and _truthy(os.getenv("CHANNEL_CLIENTS_ENABLED", "true")):
        feishu_ws.start_ws(asyncio.get_running_loop())
    if not testing and _truthy(os.getenv("SCHEDULER_ENABLED", "true")):
        scheduler.start_scheduler()
    try:
        yield
    finally:
        scheduler.stop_scheduler()
        feishu_ws.stop_ws()

def build_app() -> FastAPI:
    app = FastAPI(title="even-auth-gov", lifespan=_lifespan)

    async def _send_card(info: dict):
        if os.getenv("TESTING"):
            return
        from even_auth_gov.feishu import build_sso_approval_card, send_card
        owner = _owner()
        if owner and owner != "ou_xxx":
            await send_card(owner, build_sso_approval_card(info.get("open_id", ""), info.get("name", ""), info.get("email", "")))
    wh.send_approval_card = _send_card

    @app.post("/casdoor/webhook")
    async def casdoor_webhook(request: Request):
        body = await request.body()
        # Casdoor 无 body 签名,认证靠 webhook 配置的自定义头传共享 token。
        # header 名由 CASDOOR_WEBHOOK_HEADER 配(默认 X-Webhook-Token),须与 Casdoor 后台 webhook 的 header 名一致。
        header = os.getenv("CASDOOR_WEBHOOK_HEADER", "X-Webhook-Token")
        token = request.headers.get(header, "")
        result = await wh.handle(body, token)
        return Response(status_code=401 if result["status"] == "rejected" else 200)

    @app.post("/feishu/card")
    async def feishu_card(request: Request):
        payload = await request.json()
        value = (payload.get("action") or {}).get("value", {})
        operator = (payload.get("operator") or {}).get("open_id", "")
        async with _casdoor_client() as client:
            r = await decision.handle(value.get("action", ""), operator_id=operator, owner=_owner(),
                                      sso_open_id=value.get("sso_open_id", ""), client=client)
        return {"toast": {"type": "success" if r["status"] == "ok" else "error", "content": r["message"]}}

    return app
