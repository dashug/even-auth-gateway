"""每日对账兜底:对 approved 用户查飞书在职状态,frozen/resigned/exited → 禁用。
API 错误 fail-open(飞书 get-user 无可靠 not-found 码,见 CS Hub 前置 spec)。"""
from __future__ import annotations
import json, logging, os
from pathlib import Path
from even_auth_gov import offboard

logger = logging.getLogger(__name__)

async def fetch_feishu_status(open_id: str):
    """查飞书用户状态对象(有 is_frozen/is_resigned/is_exited);复用 CS Hub get_client + contact.v3.user.aget。"""
    from even_auth_gov.feishu import get_client
    from lark_oapi.api.contact.v3 import GetUserRequest
    client = get_client()
    req = GetUserRequest.builder().user_id(open_id).user_id_type("open_id").build()
    resp = await client.contact.v3.user.aget(req)
    if not resp.success():
        raise RuntimeError(f"feishu get-user {resp.code} {resp.msg}")
    return resp.data.user.status if resp.data and resp.data.user else None

def _open_ids_by_status(*statuses: str) -> list[str]:
    raw = os.getenv("APPROVAL_STORE_FILE", "").strip() or "data/approvals.json"
    p = Path(raw)
    if not p.exists():
        return []
    try:
        recs = json.loads(p.read_text(encoding="utf-8")).get("records", {})
    except Exception:
        return []
    want = set(statuses)
    return [oid for oid, r in recs.items() if r.get("status") in want]

async def run(client) -> None:
    # 1) 兜底重试:上次禁用失败的(安全攸关,优先)。apply 内部会重试+成功则转 disabled,再失败仍标 disable_failed+告警。
    for open_id in _open_ids_by_status("disable_failed"):
        logger.info("Reconcile: 重试上次禁用失败的 %s", open_id)
        await offboard.apply(open_id, "", "reconcile-retry", client)
    # 2) 常规对账:approved 用户查飞书在职状态
    for open_id in _open_ids_by_status("approved"):
        try:
            status = await fetch_feishu_status(open_id)
        except Exception as e:
            logger.warning("Reconcile: feishu status error for %s: %s — leaving untouched", open_id, e)
            continue
        if offboard.offboard_flags(status):
            await offboard.apply(open_id, "", "reconcile", client)
