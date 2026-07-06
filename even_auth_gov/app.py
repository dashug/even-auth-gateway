"""治理服务 FastAPI 组装:webhook 路由 + 飞书卡片回调。
飞书集成与配置均为网关自带(even_auth_gov.feishu / .settings),不依赖 cshub。"""
from __future__ import annotations
import os
import httpx
from fastapi import FastAPI, Request, Response
from even_auth_gov import webhook as wh, decision, settings

def _casdoor_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=settings.casdoor_endpoint())

def _owner() -> str:
    return settings.approver_feishu_id()

def build_app() -> FastAPI:
    app = FastAPI(title="even-auth-gov")

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
        sig = request.headers.get("X-Casdoor-Signature", "")  # header 名以 casdoor-findings.md(0.4.3) 为准
        result = await wh.handle(body, sig)
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
