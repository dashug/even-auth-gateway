"""每日对账兜底:对 approved 用户查飞书在职状态,frozen/resigned/exited → 禁用。
API 错误 fail-open(飞书 get-user 无可靠 not-found 码,见 CS Hub 前置 spec)。

APP-AWARE(设计评审 #2 #16):approval_store 现在按 (open_id, app) 记,同一
用户可能有多条 approved 记录(每个应用一条)。离职判定/禁用是按**用户**的
(见 offboard.py),所以这里按 open_id 去重后只查一次飞书、只 offboard 一次 ——
否则同一个真实离职的人会被 apply() 重复调用 N 次(N=他被批准的应用数)。
disable_failed 重试同理去重。待批卡片补发(#12)则相反,是按 (open_id, app)
逐条处理的 —— 一个人可能对 app A 已批、对 app B 还在 pending 补发卡片,互不影响。
"""
from __future__ import annotations
import logging
import os
from datetime import datetime, timezone
from even_auth_gov import offboard, approval_store, settings

logger = logging.getLogger(__name__)

# 待审批卡片补发阈值:发失败(notified_at 空)必补;成功但超此秒数仍 pending 也重发一次催办。
_PENDING_RESEND_SECONDS = int(os.getenv("SSO_PENDING_RESEND_SECONDS", str(86400)))

# 飞书 get-user 对"open_id 不存在(硬删除)"返回此 code(实测 2026-07-08)。
# 区别于超时/5xx 等瞬时错误 —— 前者=人没了要禁用,后者=API 抖动要 fail-open。
FEISHU_USER_NOT_FOUND = 99992351

class FeishuUserNotFound(Exception):
    """飞书里已查无此 open_id(硬删除)。视为离职,须禁用。"""

async def fetch_feishu_status(open_id: str):
    """查飞书用户状态对象(有 is_frozen/is_resigned/is_exited);复用 CS Hub get_client + contact.v3.user.aget。"""
    from even_auth_gov.feishu import get_client
    from lark_oapi.api.contact.v3 import GetUserRequest
    client = get_client()
    req = GetUserRequest.builder().user_id(open_id).user_id_type("open_id").build()
    resp = await client.contact.v3.user.aget(req)
    if not resp.success():
        if resp.code == FEISHU_USER_NOT_FOUND:
            raise FeishuUserNotFound(open_id)
        raise RuntimeError(f"feishu get-user {resp.code} {resp.msg}")
    return resp.data.user.status if resp.data and resp.data.user else None

def _distinct_open_ids_by_status(*statuses: str) -> list[str]:
    """去重的 open_id 列表 —— 同一用户可能对多个 app 都命中同一 status,只处理一次。
    保序(dict 插入序)让结果具确定性,方便测试断言。"""
    want = set(statuses)
    seen: set[str] = set()
    out: list[str] = []
    for r in approval_store.all_records().values():
        if r.get("status") not in want:
            continue
        oid = r.get("open_id") or ""
        if oid and oid not in seen:
            seen.add(oid)
            out.append(oid)
    return out

def _pending_needing_card() -> list[dict]:
    """待审批但卡片没送达(notified_at 空)或催办到期的记录。按 (open_id, app) 逐条 ——
    同一用户对不同 app 的 pending 状态互相独立,都要各自补发。"""
    out, now = [], datetime.now(timezone.utc)
    for r in approval_store.all_records().values():
        if r.get("status") != "pending":
            continue
        na = r.get("notified_at") or ""
        if not na:
            out.append(r); continue
        try:
            if (now - datetime.fromisoformat(na)).total_seconds() > _PENDING_RESEND_SECONDS:
                out.append(r)
        except Exception:
            out.append(r)
    return out

async def run(client) -> None:
    # 0) 补发卡住的待审批卡片(#12:发失败/审批人误配 → 不再永久 pending 无人知)。
    # 每条记录自带 app,按 app 找回其审批人(settings.approver_for)—— 不同 app 可能是不同人。
    from even_auth_gov import webhook
    for r in _pending_needing_card():
        open_id = r.get("open_id", "")
        app = r.get("app") or settings.default_app()
        try:
            await webhook.send_approval_card(
                {"open_id": open_id, "name": r.get("name", ""), "email": r.get("email", ""), "app": app}
            )
            approval_store.mark_notified(open_id, app)
            logger.info("Reconcile: 补发待审批卡片 %s app=%s", open_id, app)
        except Exception as e:
            logger.warning("Reconcile: 补发卡片失败 %s app=%s: %s", open_id, app, e)
    # 1) 兜底重试:上次禁用失败的(安全攸关,优先)。apply 内部会重试+成功则转 disabled,再失败仍标 disable_failed+告警。
    # 离职是按用户的 —— 同一用户可能在多个 app 记录上都是 disable_failed,去重后只重试一次。
    for open_id in _distinct_open_ids_by_status("disable_failed"):
        logger.info("Reconcile: 重试上次禁用失败的 %s", open_id)
        await offboard.apply(open_id, "", "reconcile-retry", client)
    # 2) 常规对账:approved 用户查飞书在职状态。同一用户可能对多个 app 都是 approved,
    # 去重后只查一次飞书、departed 时只 offboard 一次(offboard.apply 本身是按用户禁用的)。
    for open_id in _distinct_open_ids_by_status("approved"):
        try:
            status = await fetch_feishu_status(open_id)
        except FeishuUserNotFound:
            # #5 修复:硬删除的用户,飞书查无此人 → 视为离职禁用,不再 fail-open 永久漏掉。
            # (残余风险:该 code 也覆盖"格式非法 id",但 store 里的 id 均来自真实登录、格式合法 → 命中即真删除)
            logger.warning("Reconcile: %s 在飞书已不存在(硬删除)→ 禁用", open_id)
            await offboard.apply(open_id, "", "reconcile-deleted", client)
            continue
        except Exception as e:
            # 瞬时错误(超时/5xx/限流):fail-open 不误禁,等下次对账
            logger.warning("Reconcile: feishu status error for %s: %s — leaving untouched (fail-open)", open_id, e)
            continue
        if offboard.offboard_flags(status):
            await offboard.apply(open_id, "", "reconcile", client)
