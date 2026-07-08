"""审批决策:校验审批人 + 幂等 + 批准调 Casdoor 加组 / 拒绝置 denied。
APP-AWARE(设计评审 #2 #16):准入组按应用区分 —— f"approved-{app}",
批准/拒绝/幂等检查都在 (sso_open_id, app) 这个组合键上进行,不会误碰
该用户在其它 app 下的准入状态。"""
from __future__ import annotations
import logging
from even_auth_gov import casdoor_admin as ca, approval_store

logger = logging.getLogger(__name__)

def _group_for(app: str) -> str:
    return f"approved-{app}"

async def handle(action: str, operator_id: str, owner: str, sso_open_id: str, app: str, client) -> dict:
    if not sso_open_id:
        return {"status": "error", "message": "Missing applicant id"}
    if not owner or operator_id != owner:
        logger.warning("SSO decision rejected: %s not approver", operator_id)
        return {"status": "error", "message": "Only the approver can act"}
    rec = approval_store.get(sso_open_id, app)
    if not rec:
        return {"status": "error", "message": "Application not found"}
    if rec.get("status") != "pending":
        return {"status": "ok", "message": f"Already processed: {rec.get('status')}"}
    group = _group_for(app)
    if action == "sso_approve":
        ok = await ca.add_to_group(client, user_id=sso_open_id, group=group)
        if not ok:
            return {"status": "error", "message": "Casdoor add-to-group failed; retry"}
        approval_store.mark_approved(sso_open_id, app, operator_id)
        return {"status": "ok", "message": f"Approved: {rec.get('name','')}"}
    # 拒绝:#11 也撤 Casdoor 组(防御:即便此前误入准入组,拒绝即清干净)
    try:
        await ca.remove_from_group(client, user_id=sso_open_id, group=group)
    except Exception as e:
        logger.warning("Deny 撤组失败 %s: %s(已标 denied,非致命)", sso_open_id, e)
    approval_store.mark_denied(sso_open_id, app, operator_id)
    return {"status": "ok", "message": f"Denied: {rec.get('name','')}"}
