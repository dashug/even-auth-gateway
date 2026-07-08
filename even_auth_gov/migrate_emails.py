"""迁移:把 CS Hub allowed_emails 里的操作员,按邮箱匹配 Casdoor 用户并加入 cs-hub 的准入组。
未匹配(飞书还没登录过、Casdoor 无此用户)的记入 unmatched,待其首登后走正常审批。
端点见 casdoor-findings.md(Phase 0 Task 0.4)。

APP-AWARE(设计评审 #2 #16):这批操作员全部是 cs-hub 试点的老用户,组名跟随
settings.default_app() 走(f"approved-{app}"),与 decision.py 现在用的 per-app
组命名保持一致 —— 不再写死 "approved-operators",否则迁移进去的组会跟
新用户走的审批流程分道扬镳,变成一个孤儿组。"""
from __future__ import annotations
import json, logging
from pathlib import Path
from even_auth_gov import casdoor_admin as ca, settings

logger = logging.getLogger(__name__)

def _approved_group() -> str:
    return f"approved-{settings.default_app()}"

async def find_user_by_email(client, email: str) -> str | None:
    """按邮箱查 Casdoor 用户,返回其 user_id 或 None。端点以 findings 0.4.1 为准。"""
    try:
        resp = await client.get("/api/get-user", params={"email": email})
        data = resp.json() if resp.content else {}
        u = data.get("data") if isinstance(data, dict) else None
        return (u or {}).get("id") if u else None
    except Exception as e:
        logger.warning("find_user_by_email error for %s: %s", email, e)
        return None

async def run(auth_file: str, client) -> dict:
    p = Path(auth_file)
    if not p.exists():
        return {"migrated": [], "unmatched": []}
    try:
        emails = json.loads(p.read_text(encoding="utf-8")).get("allowed_emails", [])
    except Exception:
        return {"migrated": [], "unmatched": []}
    migrated, unmatched = [], []
    group = _approved_group()
    for email in emails:
        uid = await find_user_by_email(client, email)
        if uid and await ca.add_to_group(client, user_id=uid, group=group):
            migrated.append(email)
        else:
            unmatched.append(email)
    logger.info("Email migration: %d migrated, %d unmatched", len(migrated), len(unmatched))
    return {"migrated": migrated, "unmatched": unmatched}
