"""Casdoor webhook 接收:验共享头 token → 新用户 → mark_pending + 推飞书审批卡片。

对齐 Casdoor 真实行为(见 casdoor.org/docs/webhooks):
- Casdoor **不对 body 做 HMAC 签名**;认证靠 webhook 上配置的**自定义请求头**传共享 token。
  故验证 = 比对约定请求头的值 == CASDOOR_WEBHOOK_SECRET(header 名由 app.py 读 CASDOOR_WEBHOOK_HEADER)。
- body 是 Casdoor Record;受影响用户对象在 `extendedUser`(webhook 勾选 isUserExtended 时)
  或 `object`(Record 的对象,JSON 字符串)。`user` 字段是操作者用户名,不是对象。
"""
from __future__ import annotations
import hmac, json, logging, os
from even_auth_gov import approval_store

logger = logging.getLogger(__name__)


def _verify(token: str) -> bool:
    """Casdoor 在配置的自定义头里带共享 token;常量时间比对。未配密钥则 fail-closed 拒绝。"""
    expected = os.getenv("CASDOOR_WEBHOOK_SECRET", "")
    if not expected:
        return False
    return hmac.compare_digest(token or "", expected)


def _extract_user(evt: dict) -> dict:
    """从 Casdoor Record 取受影响用户对象:extendedUser > object(str/dict) > user(dict, 兜底)。"""
    u = evt.get("extendedUser")
    if isinstance(u, dict) and u:
        return u
    obj = evt.get("object")
    if isinstance(obj, dict) and obj:
        return obj
    if isinstance(obj, str) and obj:
        try:
            parsed = json.loads(obj)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    u2 = evt.get("user")
    return u2 if isinstance(u2, dict) else {}


# 由 app.py 启动时注入真实实现(发飞书卡片);测试用 monkeypatch 覆盖
async def send_approval_card(info: dict) -> None:  # pragma: no cover - overridden at runtime
    raise NotImplementedError


async def handle(body: bytes, token: str) -> dict:
    if not _verify(token):
        logger.warning("Casdoor webhook token invalid")
        return {"status": "rejected"}
    try:
        evt = json.loads(body)
    except Exception:
        return {"status": "rejected"}
    if evt.get("action") not in ("signup", "add-user"):
        return {"status": "ignored"}
    user = _extract_user(evt)
    open_id = user.get("id") or ""
    if not open_id:
        return {"status": "ignored"}
    is_new = approval_store.mark_pending(
        open_id, {"name": user.get("name", ""), "email": user.get("email", "")}
    )
    if is_new:
        await send_approval_card(
            {"open_id": open_id, "name": user.get("name", ""), "email": user.get("email", "")}
        )
    return {"status": "ok"}
