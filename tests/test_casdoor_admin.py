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
