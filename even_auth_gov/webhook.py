"""Casdoor webhook 接收:验签 → 新用户 → mark_pending + 推飞书审批卡片。
验签方案见 casdoor-findings.md(Phase 0 Task 0.4;默认 HMAC-SHA256 共享密钥)。"""
from __future__ import annotations
import hmac, hashlib, json, logging, os
from even_auth_gov import approval_store

logger = logging.getLogger(__name__)

def _verify(body: bytes, signature: str) -> bool:
    secret = os.getenv("CASDOOR_WEBHOOK_SECRET", "").encode()
    if not secret:
        return False
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")

# 由 app.py 启动时注入真实实现(发飞书卡片);测试用 monkeypatch 覆盖
async def send_approval_card(info: dict) -> None:  # pragma: no cover - overridden at runtime
    raise NotImplementedError

async def handle(body: bytes, signature: str) -> dict:
    if not _verify(body, signature):
        logger.warning("Casdoor webhook signature invalid")
        return {"status": "rejected"}
    try:
        evt = json.loads(body)
    except Exception:
        return {"status": "rejected"}
    if evt.get("action") not in ("signup", "add-user"):
        return {"status": "ignored"}
    user = evt.get("user") or {}
    open_id = user.get("id") or ""
    if not open_id:
        return {"status": "ignored"}
    is_new = approval_store.mark_pending(open_id, {"name": user.get("name", ""), "email": user.get("email", "")})
    if is_new:
        await send_approval_card({"open_id": open_id, "name": user.get("name", ""), "email": user.get("email", "")})
    return {"status": "ok"}
