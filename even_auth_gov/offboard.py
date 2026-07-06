"""离职引擎:飞书事件/对账 → 调 Casdoor 禁用 + 移组 + 记审计。
禁用失败返回 False 供上层重试(安全攸关:宁可重试不可漏禁)。"""
from __future__ import annotations
import logging
from even_auth_gov import casdoor_admin as ca, approval_store

logger = logging.getLogger(__name__)
APPROVED_GROUP = "approved-operators"

def offboard_flags(status) -> bool:
    if status is None:
        return False
    return bool(getattr(status, "is_frozen", False)
                or getattr(status, "is_resigned", False)
                or getattr(status, "is_exited", False))

async def apply(open_id: str, name: str, reason: str, client) -> bool:
    if not open_id:
        return False
    rec = approval_store.get(open_id)
    if rec and rec.get("status") == "disabled":
        return True
    ok = await ca.disable_user(client, user_id=open_id)
    await ca.remove_from_group(client, user_id=open_id, group=APPROVED_GROUP)
    if ok:
        approval_store.mark_disabled(open_id, reason)
        logger.info("Offboard disabled in Casdoor: %s (%s) via %s", name, open_id, reason)
    else:
        logger.warning("Offboard disable FAILED for %s — needs retry", open_id)
    return ok
