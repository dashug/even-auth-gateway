"""每日对账兜底:对 approved 用户查飞书在职状态,frozen/resigned/exited → 禁用。
API 错误 fail-open(飞书 get-user 无可靠 not-found 码,见 CS Hub 前置 spec)。"""
from __future__ import annotations
import json, logging, os
from datetime import datetime, timezone
from pathlib import Path
from even_auth_gov import offboard

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

def _all_records() -> dict:
    raw = os.getenv("APPROVAL_STORE_FILE", "").strip() or "data/approvals.json"
    p = Path(raw)
    if not p.exists():
        return {}
    try:
        recs = json.loads(p.read_text(encoding="utf-8")).get("records", {})
        return recs if isinstance(recs, dict) else {}
    except Exception:
        return {}

def _open_ids_by_status(*statuses: str) -> list[str]:
    want = set(statuses)
    return [oid for oid, r in _all_records().items() if r.get("status") in want]

def _pending_needing_card() -> list[dict]:
    """待审批但卡片没送达(notified_at 空)或催办到期的记录。"""
    out, now = [], datetime.now(timezone.utc)
    for oid, r in _all_records().items():
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
    # 0) 补发卡住的待审批卡片(#12:发失败/审批人误配 → 不再永久 pending 无人知)
    from even_auth_gov import webhook, approval_store
    for r in _pending_needing_card():
        try:
            await webhook.send_approval_card(
                {"open_id": r.get("open_id", ""), "name": r.get("name", ""), "email": r.get("email", "")}
            )
            approval_store.mark_notified(r.get("open_id", ""))
            logger.info("Reconcile: 补发待审批卡片 %s", r.get("open_id"))
        except Exception as e:
            logger.warning("Reconcile: 补发卡片失败 %s: %s", r.get("open_id"), e)
    # 1) 兜底重试:上次禁用失败的(安全攸关,优先)。apply 内部会重试+成功则转 disabled,再失败仍标 disable_failed+告警。
    for open_id in _open_ids_by_status("disable_failed"):
        logger.info("Reconcile: 重试上次禁用失败的 %s", open_id)
        await offboard.apply(open_id, "", "reconcile-retry", client)
    # 2) 常规对账:approved 用户查飞书在职状态
    for open_id in _open_ids_by_status("approved"):
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
