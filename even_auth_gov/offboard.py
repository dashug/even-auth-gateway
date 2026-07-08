"""离职引擎:飞书事件/对账 → 调 Casdoor 禁用 + 移组 + 记审计。

安全攸关(设计文档铁律「绝不漏禁」):禁用失败退避重试;重试用尽仍失败则
标记 disable_failed(供 reconcile 重扫)+ 飞书告警审批人(真人工介入通道,不靠会被丢的日志)。
幂等靠 disable_user 本身的幂等性(再次置 isForbidden=true 无害),**不信本地 store**——
否则本地记 disabled 但 Casdoor 被人工重启用的用户,再离职会被误跳过(漏禁)。

APP-AWARE(设计评审 #2 #16)但离职本身仍是**按用户**的一次性动作:禁用 Casdoor
账号 = 该用户对所有应用的登录能力一次性关闭,不需要、也不应该按 app 分别禁用。
按 app 分的只是"移组"这一步 —— 用户可能同时是多个应用的 approved-<app> 组成员,
需要逐个移除(records_for_open_id 拿到该用户名下所有 app 记录后遍历)。
"""
from __future__ import annotations
import asyncio
import logging
from even_auth_gov import casdoor_admin as ca, approval_store, settings

logger = logging.getLogger(__name__)
_MAX_DISABLE_ATTEMPTS = 3


def offboard_flags(status) -> bool:
    if status is None:
        return False
    return bool(getattr(status, "is_frozen", False)
                or getattr(status, "is_resigned", False)
                or getattr(status, "is_exited", False))


async def _alert_human(open_id: str, name: str, reason: str, err: str) -> None:
    """禁用彻底失败 → 飞书告警审批人,提示手动禁用。告警本身失败只记日志,不影响主流程。"""
    try:
        from even_auth_gov.feishu import send_text
        approver = settings.approver_feishu_id()
        if approver and approver != "ou_xxx":
            await send_text(
                approver,
                f"⚠️ 离职禁用失败,需人工处理\n用户: {name or open_id}\n触发: {reason}\n"
                f"错误: {err}\n该 Casdoor 账号可能仍启用,请手动置 isForbidden=true。",
            )
    except Exception as e:  # pragma: no cover - 告警尽力而为
        logger.error("Offboard 告警发送失败 %s: %s", open_id, e)


async def apply(open_id: str, name: str, reason: str, client) -> bool:
    if not open_id:
        return False
    last_err = ""
    for attempt in range(1, _MAX_DISABLE_ATTEMPTS + 1):
        try:
            ok = await ca.disable_user(client, user_id=open_id)
            last_err = "disable_user 返回 False" if not ok else ""
        except Exception as e:
            ok, last_err = False, str(e)
        if ok:
            # 移组尽力做:禁用已挡住登录,移组失败非致命。用户可能同时在多个
            # 应用的准入组里(records_for_open_id 拿到该用户名下每个 app 记录),逐个移除。
            for rec in approval_store.records_for_open_id(open_id):
                rec_app = rec.get("app") or ""
                if not rec_app:
                    continue
                try:
                    await ca.remove_from_group(client, user_id=open_id, group=f"approved-{rec_app}")
                except Exception as e:
                    logger.warning("Offboard 移组失败 %s app=%s: %s(已禁用,非致命)", open_id, rec_app, e)
            approval_store.mark_all_disabled(open_id, reason)
            logger.info("Offboard 已禁用 Casdoor: %s (%s) via %s(第 %d 次)", name, open_id, reason, attempt)
            return True
        if attempt < _MAX_DISABLE_ATTEMPTS:
            await asyncio.sleep(2 ** attempt)  # 2s → 4s 退避
    # 重试用尽仍失败:留痕 + 告警人工介入
    approval_store.mark_disable_failed_all(open_id, f"{reason}: {last_err}")
    logger.error("Offboard 禁用 %d 次仍失败 %s (%s) — 已告警人工", _MAX_DISABLE_ATTEMPTS, name, open_id)
    await _alert_human(open_id, name, reason, last_err)
    return False
