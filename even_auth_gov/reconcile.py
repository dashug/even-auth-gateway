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
import asyncio
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

def _hours_since(ts: str, now: datetime) -> float | None:
    try:
        return (now - datetime.fromisoformat(ts)).total_seconds() / 3600
    except Exception:
        return None

def _pending_needing_escalation() -> list[tuple[dict, float]]:
    """审批人第一次收到卡片后超过 escalate_after_hours 仍待批、还没升级过的记录。
    从"第一次送达"而不是申请时间算起:卡片之前一直没推送成功的,审批人刚收到就升级不公平;
    也不能用 notified_at —— 催办会刷新它。老记录没有 first_notified_at 时退回申请时间。"""
    limit = settings.escalate_after_hours()
    if limit <= 0:
        return []
    out, now = [], datetime.now(timezone.utc)
    for r in approval_store.all_records().values():
        if r.get("status") != "pending" or r.get("escalated_at") or not r.get("notified_at"):
            continue
        waited = _hours_since(r.get("first_notified_at") or r.get("applied_at") or "", now)
        if waited is not None and waited > limit:
            out.append((r, waited))
    return out

# 每小时的待批扫描与每日对账可能同时触发,串行化以免同一张卡片补发/升级两次。
_SWEEP_LOCK = asyncio.Lock()

async def sweep_pending() -> None:
    """待审批的补发、催办、超时升级。调度器每小时跑一次(SSO_PENDING_SWEEP_CRON),每日对账也先跑一次。"""
    from even_auth_gov import webhook
    async with _SWEEP_LOCK:
        # 补发卡住的待审批卡片(#12:发失败/审批人误配 → 不再永久 pending 无人知)。
        # 每条记录自带 app,按 app 找回其审批人(settings.approver_for)—— 不同 app 可能是不同人。
        for r in _pending_needing_card():
            open_id = r.get("open_id", "")
            app = r.get("app") or settings.default_app()
            if await webhook.notify_pending(open_id, app, r.get("name", ""), r.get("email", "")):
                logger.info("Reconcile: 补发待审批卡片 %s app=%s", open_id, app)
        # 超时升级(里程碑 3 / PRD 风险项"审批人缺位"):卡片另发给升级对象,升级对象和原审批人
        # 谁先点算谁(decision 按记录上的 escalated_to 放行)。每条申请只升级一次。
        target = settings.escalate_to()
        for r, waited in _pending_needing_escalation():
            open_id = r.get("open_id", "")
            app = r.get("app") or settings.default_app()
            if not target or target == "ou_xxx" or target == settings.approver_for(app):
                continue   # 没配升级对象,或审批人本身就是升级对象:没有更上一级
            note = f"审批人收到该申请已 {int(waited)} 小时仍未处理,升级给你。你和原审批人谁先处理都算。"
            try:
                await webhook.send_approval_card({"open_id": open_id, "name": r.get("name", ""),
                                                  "email": r.get("email", ""), "app": app,
                                                  "to": target, "note": note})
            except Exception as e:
                logger.warning("超时升级卡片发送失败 %s app=%s → %s: %s(下次扫描重试)", open_id, app, target, e)
                continue
            approval_store.mark_escalated(open_id, app, target)
            logger.info("Reconcile: 待批超时 %.0fh,已升级 %s app=%s → %s", waited, open_id, app, target)

async def run(client) -> None:
    # 0) 待审批补发/催办/超时升级(调度器另有每小时一次的同一扫描)。
    await sweep_pending()
    # 0b) 受管账号扫描(里程碑 4a:到期 / 负责人已停用;每小时任务也跑,内部有锁)。
    from even_auth_gov import account_lifecycle
    try:
        await account_lifecycle.sweep(client)
    except Exception as e:
        logger.warning("Reconcile: 受管账号扫描失败: %s", e)
    # 1) 兜底重试:上次禁用失败的(安全攸关,优先)。apply 内部会重试+成功则转 disabled,再失败仍标 disable_failed+告警。
    # 离职是按用户的 —— 同一用户可能在多个 app 记录上都是 disable_failed,去重后只重试一次。
    for open_id in _distinct_open_ids_by_status("disable_failed"):
        logger.info("Reconcile: 重试上次禁用失败的 %s", open_id)
        await offboard.apply(open_id, "", "reconcile-retry", client)
    # 2) 常规对账:检查集 = 本地 approved 用户 ∪ Casdoor 任一 approved-* 组的飞书成员。
    # #8:直接加进 Casdoor 组、绕过本地审批库的用户(迁移/手工授权)也要被对账,否则永远漏掉。
    # 去重后每人只查一次飞书、departed 时只 offboard 一次(offboard.apply 按用户禁用)。
    from even_auth_gov import casdoor_admin
    check_ids = set(_distinct_open_ids_by_status("approved")) | set(await casdoor_admin.list_approved_feishu_members(client))
    for open_id in sorted(check_ids):
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
