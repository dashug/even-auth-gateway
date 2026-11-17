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
import logging
import os
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from even_auth_gov import (webhook as wh, settings, feishu_ws, scheduler, approval_store, logging_setup,
                           casdoor_admin as ca, id_token)

logger = logging.getLogger(__name__)

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
    # 这个密钥只给 Casdoor 用;/request-access 改为验用户的 id_token,接入应用不持有它。
    header = os.getenv("CASDOOR_WEBHOOK_HEADER", "X-Webhook-Token")
    return request.headers.get(header, "")

def build_app() -> FastAPI:
    logging_setup.setup()   # 挂 even_auth_gov 日志(否则审批/离职全静默,见 logging_setup)
    app = FastAPI(title="even-auth-gov", lifespan=_lifespan)

    async def _send_card(info: dict):
        """推审批卡片给该 app 的审批人。没真正送达就抛异常(见 webhook.send_approval_card 契约),
        否则会被误记 notified_at、reconcile 不再补发(#12)。"""
        if os.getenv("TESTING"):
            return
        from even_auth_gov.feishu import build_sso_approval_card, send_card
        app_name = info.get("app", "") or settings.default_app()
        owner = info.get("to") or settings.approver_for(app_name)   # to:重发给点击人 / 超时升级对象
        if not owner or owner == "ou_xxx":
            raise RuntimeError(f"应用 {app_name} 未配置审批人(APPROVER_<APP> / APPROVER_FEISHU_ID)")
        # 审批即赋角:该应用在 Casdoor 建了 <app>-<role> 就按角色出批准按钮。读不到清单时降级为
        # 普通"批准"按钮,不因此卡住推送或拖到对账补发;真点下去时 decision 会再查一次角色,
        # 该应用其实分了角色就拒绝并重发带角色按钮的卡片(角色强制)。
        async with ca.make_client() as client:
            roles = await ca.list_app_roles(client, app_name, settings.known_apps())
        if roles is None:
            logger.warning("读取 %s 的角色清单失败,审批卡片不带角色按钮", app_name)
        ok = await send_card(owner, build_sso_approval_card(
            info.get("open_id", ""), info.get("name", ""), info.get("email", ""), app_name, roles=roles or [],
            note=info.get("note", ""),
        ))
        if not ok:
            raise RuntimeError(f"飞书 send_card 失败(审批人 {owner}, app={app_name})")
    wh.send_approval_card = _send_card

    @app.get("/healthz")
    async def healthz():
        """探活(容器 healthcheck)。进程活着即 200;后台组件状态只作展示,不判死 ——
        WS 断线有自带的退避重连,不该因此被编排系统反复重启。"""
        return {"status": "ok", "feishu_ws": feishu_ws.is_running(), "scheduler": scheduler.is_running()}

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

        body = {"id_token": "<用户刚登录该应用拿到的 Casdoor id_token>"}。申请人与应用全部取自
        token 里 Casdoor 签名过的 claim(见 id_token.py),body 里其它字段一律忽略。
        400 缺 token / 401 token 无效 / 403 应用未登记或账号已停用 / 503 拿不到 Casdoor 公钥或账号状态。"""
        try:
            body = await request.json()
        except Exception:
            body = {}
        tok = body.get("id_token") if isinstance(body, dict) else None
        if not isinstance(tok, str) or not tok.strip():
            return JSONResponse({"error": "缺少 id_token"}, status_code=400)
        try:
            async with ca.make_client() as client:
                who = await id_token.verify(client, tok.strip())
                # token 默认 168h 有效,签名对不代表人还在职:以 Casdoor 账号实时状态为准。
                account = await ca.get_user(client, who["open_id"])
        except id_token.TokenRejected as e:
            logger.warning("request-access 拒绝(%s): %s", e.status, e)
            return JSONResponse({"error": str(e)}, status_code=e.status)
        app_name, open_id = who["app"], who["open_id"]
        if account is None:
            return JSONResponse({"error": "暂时无法确认该账号在 Casdoor 的状态,请稍后重试"}, status_code=503)
        if account.get("isForbidden"):
            logger.warning("request-access 拒绝(403): %s 账号已停用 app=%s", open_id, app_name)
            return JSONResponse({"error": "账号已停用"}, status_code=403)
        if ca.approved_group(app_name) in (account.get("groups") or []):
            return {"status": "approved", "app": app_name}   # 已有准入(如迁移进来的老用户),不必再批
        is_new = approval_store.mark_pending(open_id, app_name, {"name": who["name"], "email": who["email"]})
        if is_new:
            await wh.notify_pending(open_id, app_name, who["name"], who["email"])
        return {"status": "ok" if is_new else "exists", "app": app_name}

    # 飞书审批卡片回调走 WS 长连接(feishu_ws._on_card,由 lark-oapi SDK 验证来源),不开 HTTP 路由。
    # 明文 HTTP 回调无法验证飞书来源:operator.open_id 不是密文(出现在日志/卡片/组成员 API 里、可枚举),
    # 一旦暴露 = "任何能网络触达的人一条 curl 即可伪造审批、自助给自己开通所有应用"。
    # 若将来确需 HTTP 回调模式,必须实现完整飞书回调契约(url_verification + verification_token + encrypt_key AES 解密 + 验签)后再开。
    return app
