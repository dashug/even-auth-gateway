"""Casdoor Admin API 客户端 — 按飞书 open_id 加组/移组/禁用 + 按 open_id/邮箱反查。
已对活实例验证(docs/superpowers/specs/casdoor-findings.md §1&§3):
- 飞书用户的 Casdoor id/sub = 飞书 open_id;name 是随机串;org=CASDOOR_ORG。
- open_id→用户:GET get-user?owner=<org>&userId=<open_id>。
- 改动:GET(按 open_id) → 改 groups/isForbidden → update-user?id=<owner>/<name>&columns= 回写。
- groups 值 org 限定:<org>/<组名>。鉴权:clientId/clientSecret query。
本模块函数的 user_id 参数 = 飞书 open_id(内部解析成 <owner>/<name>)。
"""
from __future__ import annotations
import logging, os
import httpx

logger = logging.getLogger(__name__)

def _auth() -> dict:
    return {"clientId": os.getenv("CASDOOR_CLIENT_ID", ""), "clientSecret": os.getenv("CASDOOR_CLIENT_SECRET", "")}

def _org() -> str:
    return os.getenv("CASDOOR_ORG", "")

def _qualified(group: str) -> str:
    return group if "/" in group else f"{_org()}/{group}"

async def _get_by_open_id(client: httpx.AsyncClient, open_id: str) -> dict | None:
    try:
        resp = await client.get("/api/get-user", params={"owner": _org(), "userId": open_id, **_auth()}, timeout=10.0)
        data = resp.json() if resp.content else {}
        if resp.status_code == 200 and data.get("status") != "error":
            return data.get("data")
        logger.warning("Casdoor get-user(open_id=%s) failed: %s %s", open_id, resp.status_code, data)
        return None
    except Exception as e:
        logger.warning("Casdoor get-user(open_id=%s) error: %s", open_id, e)
        return None

async def _update_user(client: httpx.AsyncClient, casdoor_id: str, user_obj: dict, column: str) -> bool:
    try:
        resp = await client.post("/api/update-user", params={"id": casdoor_id, "columns": column, **_auth()},
                                 json=user_obj, timeout=10.0)
        data = resp.json() if resp.content else {}
        if resp.status_code == 200 and data.get("status") != "error":
            return True
        logger.warning("Casdoor update-user %s(%s) failed: %s %s", casdoor_id, column, resp.status_code, data)
        return False
    except Exception as e:
        logger.warning("Casdoor update-user %s error: %s", casdoor_id, e)
        return False

def _casdoor_id(u: dict) -> str:
    return f"{u.get('owner')}/{u.get('name')}"

async def add_to_group(client: httpx.AsyncClient, user_id: str, group: str) -> bool:
    """user_id = 飞书 open_id。org 限定的 group 加入用户 groups 列表。"""
    u = await _get_by_open_id(client, user_id)
    if u is None:
        return False
    qgroup = _qualified(group)
    groups = list(u.get("groups") or [])
    if qgroup in groups:
        return True
    groups.append(qgroup)
    u["groups"] = groups
    return await _update_user(client, _casdoor_id(u), u, "groups")

async def remove_from_group(client: httpx.AsyncClient, user_id: str, group: str) -> bool:
    u = await _get_by_open_id(client, user_id)
    if u is None:
        return False
    qgroup = _qualified(group)
    u["groups"] = [g for g in (u.get("groups") or []) if g != qgroup]
    return await _update_user(client, _casdoor_id(u), u, "groups")

async def disable_user(client: httpx.AsyncClient, user_id: str) -> bool:
    u = await _get_by_open_id(client, user_id)
    if u is None:
        return False
    u["isForbidden"] = True
    return await _update_user(client, _casdoor_id(u), u, "isForbidden")

async def find_user_by_open_id(client: httpx.AsyncClient, open_id: str) -> str | None:
    """飞书 open_id → Casdoor <org>/<name> 或 None。"""
    u = await _get_by_open_id(client, open_id)
    return _casdoor_id(u) if u and u.get("owner") and u.get("name") else None

async def find_user_by_email(client: httpx.AsyncClient, owner: str, email: str) -> str | None:
    """邮箱 → Casdoor <org>/<name> 或 None(迁移脚本用)。"""
    try:
        resp = await client.get("/api/get-user", params={"owner": owner, "email": email, **_auth()}, timeout=10.0)
        data = resp.json() if resp.content else {}
        u = data.get("data") if resp.status_code == 200 and data.get("status") != "error" else None
        return f"{u['owner']}/{u['name']}" if u and u.get("owner") and u.get("name") else None
    except Exception as e:
        logger.warning("Casdoor find_user_by_email %s error: %s", email, e)
        return None
