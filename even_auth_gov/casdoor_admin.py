"""Casdoor Admin API 客户端 — 按飞书 open_id 加组/移组/禁用 + 按 open_id/邮箱反查。
已对活实例验证(docs/casdoor-findings.md §1&§3):
- 飞书用户的 Casdoor id/sub = 飞书 open_id;name 是随机串;org=CASDOOR_ORG。
- open_id→用户:GET get-user?owner=<org>&userId=<open_id>。
- 改动:GET(按 open_id) → 改 groups/isForbidden → update-user?id=<owner>/<name>&columns= 回写。
- groups 值 org 限定:<org>/<组名>。鉴权:clientId/clientSecret query。
本模块函数的 user_id 参数 = 飞书 open_id(内部解析成 <owner>/<name>)。
"""
from __future__ import annotations
import asyncio, logging, os
import httpx
from even_auth_gov import settings

# #13:组/角色成员都是"整数组读-改-写"(GET user/role → 改列表 → POST 回写),并发改会丢写。
# 进程内串行锁保证同一进程内成员变更不交错。(多副本部署需再上升到分布式锁/Casdoor 端原子操作——见 README 单实例约束。)
# disable_user 走 columns=isForbidden,与组是不同列、Casdoor 只更新指定列 → 不与组操作互相覆盖,无需同锁。
_GROUP_LOCK = asyncio.Lock()

logger = logging.getLogger(__name__)


def make_client() -> httpx.AsyncClient:
    """连 Casdoor 的 httpx 客户端(base_url = CASDOOR_ENDPOINT)。用 `async with` 包住;测试里替换成假 Casdoor。"""
    return httpx.AsyncClient(base_url=settings.casdoor_endpoint())

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

async def get_user(client: httpx.AsyncClient, open_id: str) -> dict | None:
    """飞书 open_id → Casdoor 用户对象(含 groups / isForbidden);查不到或出错返回 None。"""
    return await _get_by_open_id(client, open_id)

def approved_group(app: str) -> str:
    """该应用的准入组(org 限定),即 id_token / 用户 groups 里出现的形式:<org>/approved-<app>。"""
    return _qualified(f"approved-{app}")

async def list_approved_feishu_members(client: httpx.AsyncClient) -> list[str]:
    """org 下所有飞书用户(id 以 ou_ 开头)且在任一 approved-* 组的 open_id 列表。
    #8:补上"直接加进 Casdoor 组(迁移/手工授权)、不在本地审批库"的用户,让 reconcile 也能对账他们。
    密码用户等非飞书身份跳过(不走飞书在职状态对账)。"""
    prefix = f"{_org()}/approved-"
    try:
        resp = await client.get("/api/get-users", params={"owner": _org(), **_auth()}, timeout=15.0)
        data = resp.json() if resp.content else {}
        if resp.status_code != 200 or data.get("status") == "error":
            logger.warning("Casdoor get-users failed: %s %s", resp.status_code, data)
            return []
        users = data.get("data") or []
    except Exception as e:
        logger.warning("Casdoor get-users error: %s", e)
        return []
    out = []
    for u in users:
        oid = u.get("id") or ""
        if u.get("isForbidden"):
            continue  # 已禁用 = 已断权,不必每天再对账/再 offboard 一次
        if oid.startswith("ou_") and any((g or "").startswith(prefix) for g in (u.get("groups") or [])):
            out.append(oid)
    return out

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
    async with _GROUP_LOCK:
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
    async with _GROUP_LOCK:
        u = await _get_by_open_id(client, user_id)
        if u is None:
            return False
        qgroup = _qualified(group)
        u["groups"] = [g for g in (u.get("groups") or []) if g != qgroup]
        return await _update_user(client, _casdoor_id(u), u, "groups")

async def remove_approved_groups(client: httpx.AsyncClient, user_id: str) -> bool:
    """离职用:把用户在 Casdoor 里**实际挂着的**所有 <org>/approved-* 组一次清掉(一次 GET + 一次写)。
    以 Casdoor 为准而非本地审批库 —— 迁移/手工授权进组的用户本地没有记录,也必须清。"""
    prefix = f"{_org()}/approved-"
    async with _GROUP_LOCK:
        u = await _get_by_open_id(client, user_id)
        if u is None:
            return False
        groups = list(u.get("groups") or [])
        kept = [g for g in groups if not (g or "").startswith(prefix)]
        if len(kept) == len(groups):
            return True
        u["groups"] = kept
        return await _update_user(client, _casdoor_id(u), u, "groups")

async def list_app_roles(client: httpx.AsyncClient, app: str, known_apps=()) -> list[dict] | None:
    """该应用在 Casdoor 里建好的角色(命名约定 `<app>-<role>`,见 integration-guide 第一步.3)。
    返回 [{"name": 角色名, "display": 给审批人看的名字}](按 name 排序);已停用的跳过。
    known_apps:名字以 `<app>-` 开头的其它应用(如 app `cs` 之于 `cs-hub`),它们的角色不算本应用的。
    出错返回 None(和"确实没建角色"的 [] 区分开)。"""
    prefix = f"{app}-"
    siblings = [f"{a}-" for a in known_apps if a != app and a.startswith(prefix)]
    try:
        resp = await client.get("/api/get-roles", params={"owner": _org(), **_auth()}, timeout=10.0)
        data = resp.json() if resp.content else {}
        if resp.status_code != 200 or not isinstance(data, dict) or data.get("status") == "error":
            logger.warning("Casdoor get-roles failed: %s %s", resp.status_code, data)
            return None
        roles = data.get("data") or []
    except Exception as e:
        logger.warning("Casdoor get-roles error: %s", e)
        return None
    out = []
    for r in roles:
        name = (r or {}).get("name") or ""
        if not name.startswith(prefix) or any(name.startswith(s) for s in siblings):
            continue
        if r.get("isEnabled") is False:
            continue
        out.append({"name": name, "display": r.get("displayName") or name[len(prefix):]})
    return sorted(out, key=lambda x: x["name"])

async def assign_role(client: httpx.AsyncClient, user_id: str, role: str) -> bool:
    """user_id = 飞书 open_id。把用户加进 <org>/<role> 的 users(元素是 <owner>/<name>);已在则 no-op。
    角色对象整体读-改-写,与组变更共用 _GROUP_LOCK。"""
    role_id = f"{_org()}/{role}"
    async with _GROUP_LOCK:
        u = await _get_by_open_id(client, user_id)
        if u is None:
            return False
        member = _casdoor_id(u)
        try:
            resp = await client.get("/api/get-role", params={"id": role_id, **_auth()}, timeout=10.0)
            data = resp.json() if resp.content else {}
            obj = data.get("data") if resp.status_code == 200 and data.get("status") != "error" else None
            if not obj:
                logger.warning("Casdoor get-role %s 不存在或失败: %s %s", role_id, resp.status_code, data)
                return False
            users = list(obj.get("users") or [])
            if member in users:
                return True
            obj["users"] = users + [member]
            resp = await client.post("/api/update-role", params={"id": role_id, **_auth()}, json=obj, timeout=10.0)
            data = resp.json() if resp.content else {}
            if resp.status_code == 200 and data.get("status") != "error":
                return True
            logger.warning("Casdoor update-role %s failed: %s %s", role_id, resp.status_code, data)
            return False
        except Exception as e:
            logger.warning("Casdoor assign_role %s → %s error: %s", user_id, role_id, e)
            return False

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

async def find_open_id_by_email(client: httpx.AsyncClient, email: str) -> str | None:
    """邮箱 → 飞书 open_id(Casdoor 用户 id 字段)或 None。迁移脚本用:返回值可直接喂给 add_to_group。
    端点见 casdoor-findings §3:GET get-user?owner=<org>&email=<email> + clientId/secret 鉴权。"""
    try:
        resp = await client.get("/api/get-user", params={"owner": _org(), "email": email, **_auth()}, timeout=10.0)
        data = resp.json() if resp.content else {}
        if resp.status_code != 200 or not isinstance(data, dict) or data.get("status") == "error":
            logger.warning("Casdoor get-user(email=%s) failed: %s %s", email, resp.status_code, data)
            return None
        u = data.get("data") or {}
        return u.get("id") or None
    except Exception as e:
        logger.warning("Casdoor find_open_id_by_email %s error: %s", email, e)
        return None

async def find_user_by_email(client: httpx.AsyncClient, owner: str, email: str) -> str | None:
    """邮箱 → Casdoor <org>/<name> 或 None。(迁移脚本要的是 open_id,用 find_open_id_by_email。)"""
    try:
        resp = await client.get("/api/get-user", params={"owner": owner, "email": email, **_auth()}, timeout=10.0)
        data = resp.json() if resp.content else {}
        u = data.get("data") if resp.status_code == 200 and data.get("status") != "error" else None
        return f"{u['owner']}/{u['name']}" if u and u.get("owner") and u.get("name") else None
    except Exception as e:
        logger.warning("Casdoor find_user_by_email %s error: %s", email, e)
        return None
