"""治理服务 FastAPI 组装:webhook 路由 + /request-access + 飞书卡片回调。
飞书集成与配置均为网关自带(even_auth_gov.feishu / .settings),不依赖 cshub。

APP-AWARE(设计评审 #2 #16):Casdoor signup webhook 只在用户**首次**注册时
触发一次,落到 settings.default_app()。第二个及以后接入的应用,靠
/request-access 主动上报"某用户在等它的批准"(同一用户可能已为第一个应用
批准过,但从未替第二个应用申请过)。两条路径最终都调同一个 _send_card
helper,按 info["app"] 选审批人(settings.approver_for)+ 卡片带 app 标识。

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
from even_auth_gov import webhook as wh, settings, feishu_ws, scheduler, approval_store

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

def _webhook_token(request: Request) -> str:
    # Casdoor 无 body 签名,认证靠 webhook 配置的自定义头传共享 token。
    # header 名由 CASDOOR_WEBHOOK_HEADER 配(默认 X-Webhook-Token),须与 Casdoor 后台 webhook 的 header 名一致。
    # /request-access 复用同一套共享 token:接入应用拿到这个密钥,即可向网关上报待批用户。
    header = os.getenv("CASDOOR_WEBHOOK_HEADER", "X-Webhook-Token")
    return request.headers.get(header, "")

def build_app() -> FastAPI:
    app = FastAPI(title="even-auth-gov", lifespan=_lifespan)

    async def _send_card(info: dict):
        if os.getenv("TESTING"):
            return
        from even_auth_gov.feishu import build_sso_approval_card, send_card
        app_name = info.get("app", "") or settings.default_app()
        owner = settings.approver_for(app_name)
        if owner and owner != "ou_xxx":
            await send_card(owner, build_sso_approval_card(
                info.get("open_id", ""), info.get("name", ""), info.get("email", ""), app_name
            ))
    wh.send_approval_card = _send_card

    @app.post("/casdoor/webhook")
    async def casdoor_webhook(request: Request):
        body = await request.body()
        result = await wh.handle(body, _webhook_token(request))
        return Response(status_code=401 if result["status"] == "rejected" else 200)

    @app.post("/request-access")
    async def request_access(request: Request):
        """应用 #2+ 上报"某用户在等本应用的批准"。Casdoor signup webhook 只在用户
        首次注册时触发一次,已注册用户第一次接触新应用不会再有 webhook 事件——
        接入方需要自己调这个端点补上 mark_pending + 推审批卡片(带 app 标识)。
        鉴权与 /casdoor/webhook 同一套共享 token,未带/带错 → 401。"""
        if not wh._verify(_webhook_token(request)):
            return Response(status_code=401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}
        app_name = (body.get("app") or "").strip()
        open_id = (body.get("open_id") or "").strip()
        if not app_name or not open_id:
            return Response(status_code=400)
        name = body.get("name", "")
        email = body.get("email", "")
        is_new = approval_store.mark_pending(open_id, app_name, {"name": name, "email": email})
        if is_new:
            await _send_card({"open_id": open_id, "name": name, "email": email, "app": app_name})
            approval_store.mark_notified(open_id, app_name)
        return {"status": "ok" if is_new else "exists"}

    # 飞书审批卡片回调走 WS 长连接(feishu_ws._on_card,由 lark-oapi SDK 验证来源),不开 HTTP 路由。
    # 明文 HTTP 回调无法验证飞书来源:operator.open_id 不是密文(出现在日志/卡片/组成员 API 里、可枚举),
    # 一旦暴露 = "任何能网络触达的人一条 curl 即可伪造审批、自助给自己开通所有应用"。
    # 若将来确需 HTTP 回调模式,必须实现完整飞书回调契约(url_verification + verification_token + encrypt_key AES 解密 + 验签)后再开。
    return app
