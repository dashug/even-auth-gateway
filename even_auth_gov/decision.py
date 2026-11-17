"""审批决策:校验审批人 + 幂等 + 批准调 Casdoor 加组 / 拒绝置 denied。
APP-AWARE(设计评审 #2 #16):准入组按应用区分 —— f"approved-{app}",
批准/拒绝/幂等检查都在 (sso_open_id, app) 这个组合键上进行,不会误碰
该用户在其它 app 下的准入状态。

审批即赋角(F-5.5.3):卡片按钮带 role 时,先核对 role 是该应用在 Casdoor 里启用的角色,
再赋角色,最后加准入组 —— 组才是"能不能进"的门,任何一步失败都不开门、申请保持待批。
准入仍以组为准、角色只是附加:不分角色的应用零改动,离职清组即断权,不用另撤角色。"""
from __future__ import annotations
import logging
from even_auth_gov import casdoor_admin as ca, approval_store, settings

logger = logging.getLogger(__name__)

def _group_for(app: str) -> str:
    return f"approved-{app}"

async def handle(action: str, operator_id: str, owner: str, sso_open_id: str, app: str, client,
                 role: str = "") -> dict:
    """返回 {"status": ok|error, "message": 给审批人看的结果}。真正发生状态转换时另带
    "decision": approved|denied —— 卡片回调据此通知申请人(幂等重复点击不带,不重复通知);
    带角色批准成功时另带 "role": 角色显示名。"""
    if not sso_open_id:
        return {"status": "error", "message": "卡片缺少申请人 id"}
    if not owner or operator_id != owner:
        logger.warning("SSO decision rejected: %s not approver", operator_id)
        return {"status": "error", "message": "只有该应用的审批人可以操作"}
    rec = approval_store.get(sso_open_id, app)
    if not rec:
        return {"status": "error", "message": "找不到这条申请"}
    name = rec.get("name", "") or sso_open_id
    if rec.get("status") != "pending":
        return {"status": "ok", "message": f"{name} 的申请已处理过: {rec.get('status')}"}
    group = _group_for(app)
    if action == "sso_approve":
        role_display = ""
        if role:
            roles = await ca.list_app_roles(client, app, settings.known_apps())
            if roles is None:
                return {"status": "error", "message": f"批准 {name} 失败:读取 {app} 的角色清单失败,申请仍待批,请稍后再点一次"}
            match = next((r for r in roles if r["name"] == role), None)
            if match is None:
                logger.warning("SSO approve rejected: role %s not an enabled role of app=%s", role, app)
                return {"status": "error", "message": f"角色 {role} 不是 {app} 当前启用的角色,未批准"}
            if not await ca.assign_role(client, user_id=sso_open_id, role=role):
                return {"status": "error", "message": f"批准 {name} 失败:Casdoor 分配角色失败,申请仍待批,请稍后再点一次"}
            role_display = match["display"]
        ok = await ca.add_to_group(client, user_id=sso_open_id, group=group)
        if not ok:
            return {"status": "error", "message": f"批准 {name} 失败:Casdoor 加组失败,申请仍待批,请稍后再点一次批准"}
        approval_store.mark_approved(sso_open_id, app, operator_id, role=role)
        logger.info("SSO approved %s app=%s role=%s by %s", sso_open_id, app, role or "-", operator_id)
        result = {"status": "ok", "message": f"已批准 {name}", "decision": "approved"}
        if role_display:
            result["message"] += f"(角色:{role_display})"
            result["role"] = role_display
        return result
    if action != "sso_deny":
        return {"status": "error", "message": f"未知操作: {action}"}
    # 拒绝:#11 也撤 Casdoor 组(防御:即便此前误入准入组,拒绝即清干净)
    try:
        await ca.remove_from_group(client, user_id=sso_open_id, group=group)
    except Exception as e:
        logger.warning("Deny 撤组失败 %s: %s(已标 denied,非致命)", sso_open_id, e)
    approval_store.mark_denied(sso_open_id, app, operator_id)
    logger.info("SSO denied %s app=%s by %s", sso_open_id, app, operator_id)
    return {"status": "ok", "message": f"已拒绝 {name}", "decision": "denied"}
