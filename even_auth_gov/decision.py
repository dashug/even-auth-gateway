"""审批决策:校验审批人 + 幂等 + 批准调 Casdoor 加组 / 拒绝置 denied。"""
from __future__ import annotations
import logging
from even_auth_gov import casdoor_admin as ca, approval_store

logger = logging.getLogger(__name__)
APPROVED_GROUP = "approved-operators"

async def handle(action: str, operator_id: str, owner: str, sso_open_id: str, client) -> dict:
    if not sso_open_id:
        return {"status": "error", "message": "Missing applicant id"}
    if not owner or operator_id != owner:
        logger.warning("SSO decision rejected: %s not approver", operator_id)
        return {"status": "error", "message": "Only the approver can act"}
    rec = approval_store.get(sso_open_id)
    if not rec:
        return {"status": "error", "message": "Application not found"}
    if rec.get("status") != "pending":
        return {"status": "ok", "message": f"Already processed: {rec.get('status')}"}
    if action == "sso_approve":
        ok = await ca.add_to_group(client, user_id=sso_open_id, group=APPROVED_GROUP)
        if not ok:
            return {"status": "error", "message": "Casdoor add-to-group failed; retry"}
        approval_store.mark_approved(sso_open_id, operator_id)
        return {"status": "ok", "message": f"Approved: {rec.get('name','')}"}
    approval_store.mark_denied(sso_open_id, operator_id)
    return {"status": "ok", "message": f"Denied: {rec.get('name','')}"}
