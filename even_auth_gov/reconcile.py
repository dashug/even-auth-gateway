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

def _approved_open_ids() -> list[str]:
    raw = os.getenv("APPROVAL_STORE_FILE", "").strip() or "data/approvals.json"
    p = Path(raw)
    if not p.exists():
        return []
    try:
        recs = json.loads(p.read_text(encoding="utf-8")).get("records", {})
    except Exception:
        return []
    return [oid for oid, r in recs.items() if r.get("status") == "approved"]

async def run(client) -> None:
    for open_id in _approved_open_ids():
        try:
            status = await fetch_feishu_status(open_id)
        except Exception as e:
            logger.warning("Reconcile: feishu status error for %s: %s — leaving untouched", open_id, e)
            continue
        if offboard.offboard_flags(status):
            await offboard.apply(open_id, "", "reconcile", client)
