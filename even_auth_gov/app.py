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
from fastapi import FastAPI, Request, Response
from even_auth_gov import webhook as wh, settings, feishu_ws, scheduler

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

    # 飞书审批卡片回调走 WS 长连接(feishu_ws._on_card,由 lark-oapi SDK 验证来源),不开 HTTP 路由。
    # 明文 HTTP 回调无法验证飞书来源:operator.open_id 不是密文(出现在日志/卡片/组成员 API 里、可枚举),
    # 一旦暴露 = "任何能网络触达的人一条 curl 即可伪造审批、自助给自己开通所有应用"。
    # 若将来确需 HTTP 回调模式,必须实现完整飞书回调契约(url_verification + verification_token + encrypt_key AES 解密 + 验签)后再开。
    return app
