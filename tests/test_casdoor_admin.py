import asyncio, json, httpx
from even_auth_gov import casdoor_admin as ca

OPEN_ID = "ou_feishu123"

def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://casdoor")

def _user_obj(groups=None, isForbidden=False):
    return {"owner": "even-test", "name": "randname", "id": OPEN_ID,
            "groups": groups if groups is not None else [], "isForbidden": isForbidden}

def _handler_factory(seen, user=None, get_ok=True, update_ok=True):
    def handler(req):
        if req.url.path == "/api/get-user":
            seen["get_params"] = dict(req.url.params)
            if not get_ok:
                return httpx.Response(200, json={"status": "error", "data": None})
            return httpx.Response(200, json={"status": "ok", "data": user})
        if req.url.path == "/api/update-user":
            seen["upd_id"] = req.url.params.get("id")
            seen["upd_columns"] = req.url.params.get("columns")
            seen["upd_body"] = json.loads(req.content)
            return httpx.Response(200, json={"status": "ok" if update_ok else "error", "data": "Affected" if update_ok else None})
        return httpx.Response(404)
    return handler

def test_add_to_group_resolves_open_id_and_qualifies_group(monkeypatch):
    monkeypatch.setenv("CASDOOR_ORG", "even-test")
    seen = {}
    async def run():
        client = _client(_handler_factory(seen, user=_user_obj(groups=[])))
        ok = await ca.add_to_group(client, user_id=OPEN_ID, group="approved-operators")
        assert ok is True
    asyncio.run(run())
    # resolved open_id via userId lookup:
    assert seen["get_params"].get("userId") == OPEN_ID
    assert seen["get_params"].get("owner") == "even-test"
    # updated by <owner>/<name>, columns=groups, group org-qualified:
    assert seen["upd_id"] == "even-test/randname"
    assert seen["upd_columns"] == "groups"
    assert "even-test/approved-operators" in seen["upd_body"]["groups"]

def test_add_to_group_idempotent_when_member(monkeypatch):
    monkeypatch.setenv("CASDOOR_ORG", "even-test")
    seen = {}
    async def run():
        client = _client(_handler_factory(seen, user=_user_obj(groups=["even-test/approved-operators"])))
        assert await ca.add_to_group(client, user_id=OPEN_ID, group="approved-operators") is True
    asyncio.run(run())
    assert "upd_id" not in seen   # already member → no update

def test_remove_from_group_drops_qualified(monkeypatch):
    monkeypatch.setenv("CASDOOR_ORG", "even-test")
    seen = {}
    async def run():
        client = _client(_handler_factory(seen, user=_user_obj(groups=["even-test/approved-operators", "even-test/x"])))
        assert await ca.remove_from_group(client, user_id=OPEN_ID, group="approved-operators") is True
    asyncio.run(run())
    assert "even-test/approved-operators" not in seen["upd_body"]["groups"]
    assert "even-test/x" in seen["upd_body"]["groups"]

def test_disable_user_resolves_and_sets_forbidden(monkeypatch):
    monkeypatch.setenv("CASDOOR_ORG", "even-test")
    seen = {}
    async def run():
        client = _client(_handler_factory(seen, user=_user_obj(isForbidden=False)))
        assert await ca.disable_user(client, user_id=OPEN_ID) is True
    asyncio.run(run())
    assert seen["upd_id"] == "even-test/randname"
    assert seen["upd_columns"] == "isForbidden"
    assert seen["upd_body"]["isForbidden"] is True

def test_unknown_open_id_returns_false(monkeypatch):
    monkeypatch.setenv("CASDOOR_ORG", "even-test")
    seen = {}
    async def run():
        client = _client(_handler_factory(seen, user=None, get_ok=False))
        assert await ca.add_to_group(client, user_id="ou_ghost", group="approved-operators") is False
        assert await ca.disable_user(client, user_id="ou_ghost") is False
    asyncio.run(run())

def test_update_failure_returns_false(monkeypatch):
    monkeypatch.setenv("CASDOOR_ORG", "even-test")
    seen = {}
    async def run():
        client = _client(_handler_factory(seen, user=_user_obj(), update_ok=False))
        assert await ca.disable_user(client, user_id=OPEN_ID) is False
    asyncio.run(run())

def test_find_user_by_open_id_returns_org_name(monkeypatch):
    monkeypatch.setenv("CASDOOR_ORG", "even-test")
    seen = {}
    async def run():
        client = _client(_handler_factory(seen, user=_user_obj()))
        uid = await ca.find_user_by_open_id(client, "ou_feishu123")
        assert uid == "even-test/randname"
    asyncio.run(run())

def test_find_user_by_email_returns_org_name(monkeypatch):
    monkeypatch.setenv("CASDOOR_ORG", "even-test")
    def handler(req):
        if req.url.path == "/api/get-user":
            assert req.url.params.get("email") == "a@e.com"
            return httpx.Response(200, json={"status": "ok", "data": {"owner": "even-test", "name": "alice"}})
        return httpx.Response(404)
    async def run():
        client = _client(handler)
        assert await ca.find_user_by_email(client, owner="even-test", email="a@e.com") == "even-test/alice"
    asyncio.run(run())

def test_list_approved_feishu_members_filters(monkeypatch):
    # #8:只收 id 以 ou_ 开头(飞书用户)且在任一 approved-* 组的成员;密码用户/非准入组跳过。
    monkeypatch.setenv("CASDOOR_ORG", "even-test")
    def handler(req):
        if req.url.path == "/api/get-users":
            return httpx.Response(200, json={"data": [
                {"id": "ou_a", "groups": ["even-test/approved-cs-hub"]},        # 飞书+准入 → 收
                {"id": "ou_b", "groups": ["even-test/other"]},                  # 飞书但非准入组 → 不收
                {"id": "uuid-alice", "groups": ["even-test/approved-cs-hub"]},  # 非飞书(密码)→ 不收
                {"id": "ou_c", "groups": ["even-test/approved-demo-app"]},      # 另一 app 的准入组 → 收
            ]})
        return httpx.Response(404)
    async def run():
        async with _client(handler) as c:
            assert set(await ca.list_approved_feishu_members(c)) == {"ou_a", "ou_c"}
    asyncio.run(run())

def test_list_approved_feishu_members_skips_forbidden(monkeypatch):
    # 已禁用(isForbidden)的账号不再进对账检查集 —— 否则离职后仍挂在组里的人每天被重复 offboard。
    monkeypatch.setenv("CASDOOR_ORG", "even-test")
    def handler(req):
        if req.url.path == "/api/get-users":
            return httpx.Response(200, json={"data": [
                {"id": "ou_live", "groups": ["even-test/approved-cs-hub"], "isForbidden": False},
                {"id": "ou_gone", "groups": ["even-test/approved-cs-hub"], "isForbidden": True},
            ]})
        return httpx.Response(404)
    async def run():
        async with _client(handler) as c:
            return await ca.list_approved_feishu_members(c)
    assert asyncio.run(run()) == ["ou_live"]

def test_remove_approved_groups_drops_every_approved_group_keeps_others(monkeypatch):
    # 离职移组以 Casdoor 实际组为准(不靠本地审批库):所有 <org>/approved-* 一次清掉,其它组保留。
    monkeypatch.setenv("CASDOOR_ORG", "even-test")
    seen = {}
    user = _user_obj(groups=["even-test/approved-cs-hub", "even-test/approved-finance", "even-test/x"])
    async def run():
        async with _client(_handler_factory(seen, user=user)) as c:
            return await ca.remove_approved_groups(c, user_id=OPEN_ID)
    assert asyncio.run(run()) is True
    assert seen["upd_id"] == "even-test/randname"
    assert seen["upd_columns"] == "groups"
    assert seen["upd_body"]["groups"] == ["even-test/x"]

def test_remove_approved_groups_noop_when_none(monkeypatch):
    monkeypatch.setenv("CASDOOR_ORG", "even-test")
    seen = {}
    async def run():
        async with _client(_handler_factory(seen, user=_user_obj(groups=["even-test/x"]))) as c:
            return await ca.remove_approved_groups(c, user_id=OPEN_ID)
    assert asyncio.run(run()) is True
    assert "upd_id" not in seen   # 没有准入组 → 不写


# ── 审批即赋角(F-5.5.3):角色清单取自 Casdoor 的 <app>-<role>,赋角 = 把用户加进 role.users ──
# 接口形状按对活 Casdoor 实测(casdoor-findings §7):get-roles?owner=<org>、get-role?id=<org>/<role>、
# update-role?id=<org>/<role> 整对象回写,users 元素是 "<owner>/<name>"(不是 open_id)。

def _role(name, display="", users=None, enabled=True):
    return {"owner": "even-test", "name": name, "displayName": display, "users": users or [],
            "groups": [], "roles": [], "domains": [], "isEnabled": enabled}

def _roles_handler(seen, roles, user=None, update_ok=True, list_ok=True):
    def handler(req):
        p = req.url.path
        if p == "/api/get-roles":
            seen["roles_params"] = dict(req.url.params)
            if not list_ok:
                return httpx.Response(200, json={"status": "error", "msg": "boom", "data": None})
            return httpx.Response(200, json={"status": "ok", "data": roles})
        if p == "/api/get-user":
            return httpx.Response(200, json={"status": "ok", "data": user})
        if p == "/api/get-role":
            seen["get_role_params"] = dict(req.url.params)
            name = req.url.params["id"].split("/", 1)[1]
            return httpx.Response(200, json={"status": "ok", "data": next((r for r in roles if r["name"] == name), None)})
        if p == "/api/update-role":
            seen["upd_role_params"] = dict(req.url.params)
            seen["upd_role_body"] = json.loads(req.content)
            return httpx.Response(200, json={"status": "ok" if update_ok else "error", "data": "Affected" if update_ok else None})
        return httpx.Response(404)
    return handler

def test_list_app_roles_filters_by_app_prefix_and_enabled(monkeypatch):
    monkeypatch.setenv("CASDOOR_ORG", "even-test")
    monkeypatch.setenv("CASDOOR_CLIENT_ID", "cid")
    seen = {}
    roles = [_role("demo-app-viewer", "查看者"), _role("demo-app-editor", "编辑者"), _role("demo-app-x"),
             _role("other-viewer", "别的应用"), _role("demo-app-off", "已停用", enabled=False)]
    out = asyncio.run(ca.list_app_roles(_client(_roles_handler(seen, roles)), "demo-app"))
    assert out == [{"name": "demo-app-editor", "display": "编辑者"},
                   {"name": "demo-app-viewer", "display": "查看者"},
                   {"name": "demo-app-x", "display": "x"}]      # 没 displayName → 去掉前缀的短名
    assert seen["roles_params"]["owner"] == "even-test"
    assert seen["roles_params"]["clientId"] == "cid"

def test_list_app_roles_excludes_roles_of_longer_sibling_app(monkeypatch):
    """app `cs` 与 app `cs-hub` 并存:`cs-hub-operator` 属于 cs-hub,不能出现在 cs 的卡片上。"""
    monkeypatch.setenv("CASDOOR_ORG", "even-test")
    roles = [_role("cs-admin", "管理员"), _role("cs-hub-operator", "运营")]
    out = asyncio.run(ca.list_app_roles(_client(_roles_handler({}, roles)), "cs", known_apps={"cs", "cs-hub"}))
    assert [r["name"] for r in out] == ["cs-admin"]
    out_hub = asyncio.run(ca.list_app_roles(_client(_roles_handler({}, roles)), "cs-hub", known_apps={"cs", "cs-hub"}))
    assert [r["name"] for r in out_hub] == ["cs-hub-operator"]

def test_list_app_roles_error_is_none_not_empty(monkeypatch):
    """None = 查不到(Casdoor 出错),[] = 该应用确实没建角色 —— 调用方要能区分。"""
    monkeypatch.setenv("CASDOOR_ORG", "even-test")
    assert asyncio.run(ca.list_app_roles(_client(_roles_handler({}, [], list_ok=False)), "demo-app")) is None
    def boom(req): raise httpx.ConnectError("down")
    assert asyncio.run(ca.list_app_roles(_client(boom), "demo-app")) is None
    assert asyncio.run(ca.list_app_roles(_client(_roles_handler({}, [])), "demo-app")) == []

def test_assign_role_adds_user_as_owner_slash_name(monkeypatch):
    monkeypatch.setenv("CASDOOR_ORG", "even-test")
    seen = {}
    roles = [_role("demo-app-viewer", "查看者", users=["even-test/bob"])]
    ok = asyncio.run(ca.assign_role(_client(_roles_handler(seen, roles, user=_user_obj())), OPEN_ID, "demo-app-viewer"))
    assert ok is True
    assert seen["get_role_params"]["id"] == "even-test/demo-app-viewer"
    assert seen["upd_role_params"]["id"] == "even-test/demo-app-viewer"
    assert seen["upd_role_body"]["users"] == ["even-test/bob", "even-test/randname"]

def test_assign_role_idempotent_when_already_member(monkeypatch):
    monkeypatch.setenv("CASDOOR_ORG", "even-test")
    seen = {}
    roles = [_role("demo-app-viewer", users=["even-test/randname"])]
    assert asyncio.run(ca.assign_role(_client(_roles_handler(seen, roles, user=_user_obj())), OPEN_ID, "demo-app-viewer")) is True
    assert "upd_role_body" not in seen

def test_assign_role_unknown_user_or_role_or_update_failure_is_false(monkeypatch):
    monkeypatch.setenv("CASDOOR_ORG", "even-test")
    roles = [_role("demo-app-viewer")]
    assert asyncio.run(ca.assign_role(_client(_roles_handler({}, roles, user=None)), OPEN_ID, "demo-app-viewer")) is False
    assert asyncio.run(ca.assign_role(_client(_roles_handler({}, roles, user=_user_obj())), OPEN_ID, "demo-app-nope")) is False
    assert asyncio.run(ca.assign_role(_client(_roles_handler({}, roles, user=_user_obj(), update_ok=False)), OPEN_ID, "demo-app-viewer")) is False
