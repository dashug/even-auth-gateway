import asyncio, json
from even_auth_gov import migrate_emails as m

def test_migrate_adds_matched_emails_to_group(monkeypatch, tmp_path):
    auth = tmp_path / "admin_auth.json"
    auth.write_text(json.dumps({"allowed_emails": ["a@e.com", "b@e.com"]}), encoding="utf-8")
    added = []
    async def fake_find(client, email): return {"a@e.com": "ou_a", "b@e.com": None}.get(email)
    async def fake_add(client, user_id, group): added.append(user_id); return True
    monkeypatch.setattr(m, "find_user_by_email", fake_find)
    monkeypatch.setattr(m.ca, "add_to_group", fake_add)
    report = asyncio.run(m.run(str(auth), client=None))
    assert added == ["ou_a"]
    assert report["migrated"] == ["a@e.com"] and report["unmatched"] == ["b@e.com"]

def test_migrate_add_failure_counts_unmatched(monkeypatch, tmp_path):
    auth = tmp_path / "admin_auth.json"
    auth.write_text(json.dumps({"allowed_emails": ["a@e.com"]}), encoding="utf-8")
    async def fake_find(client, email): return "ou_a"
    async def fake_add(client, user_id, group): return False   # Casdoor add failed
    monkeypatch.setattr(m, "find_user_by_email", fake_find)
    monkeypatch.setattr(m.ca, "add_to_group", fake_add)
    report = asyncio.run(m.run(str(auth), client=None))
    assert report["migrated"] == [] and report["unmatched"] == ["a@e.com"]

def test_migrate_missing_file_empty_report(monkeypatch, tmp_path):
    report = asyncio.run(m.run(str(tmp_path / "nope.json"), client=None))
    assert report == {"migrated": [], "unmatched": []}


# ── 真实 Casdoor 调用形状(原实现漏 owner + clientId/secret,对活实例必然查不到) ──
import httpx

def _casdoor(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://casdoor")

def test_find_user_by_email_sends_org_and_credentials_returns_open_id(monkeypatch):
    monkeypatch.setenv("CASDOOR_ORG", "even-test")
    monkeypatch.setenv("CASDOOR_CLIENT_ID", "cid")
    monkeypatch.setenv("CASDOOR_CLIENT_SECRET", "csecret")
    seen = {}
    def handler(req):
        seen.update(dict(req.url.params))
        return httpx.Response(200, json={"status": "ok", "data": {"owner": "even-test", "name": "rand", "id": "ou_a"}})
    async def run():
        async with _casdoor(handler) as c:
            return await m.find_user_by_email(c, "a@e.com")
    assert asyncio.run(run()) == "ou_a"     # 返回飞书 open_id(add_to_group 的入参),不是 <org>/<name>
    assert seen["email"] == "a@e.com"
    assert seen["owner"] == "even-test"
    assert seen["clientId"] == "cid" and seen["clientSecret"] == "csecret"

def test_find_user_by_email_casdoor_error_returns_none(monkeypatch):
    monkeypatch.setenv("CASDOOR_ORG", "even-test")
    def handler(req):
        return httpx.Response(200, json={"status": "error", "msg": "Unauthorized operation", "data": None})
    async def run():
        async with _casdoor(handler) as c:
            return await m.find_user_by_email(c, "a@e.com")
    assert asyncio.run(run()) is None

def test_migrate_dry_run_does_not_touch_casdoor_groups(monkeypatch, tmp_path):
    auth = tmp_path / "admin_auth.json"
    auth.write_text(json.dumps({"allowed_emails": ["a@e.com", "b@e.com"]}), encoding="utf-8")
    added = []
    async def fake_find(client, email): return {"a@e.com": "ou_a"}.get(email)
    async def fake_add(client, user_id, group): added.append(user_id); return True
    monkeypatch.setattr(m, "find_user_by_email", fake_find)
    monkeypatch.setattr(m.ca, "add_to_group", fake_add)
    report = asyncio.run(m.run(str(auth), client=None, dry_run=True))
    assert added == []                                   # 演练不写 Casdoor
    assert report["migrated"] == ["a@e.com"] and report["unmatched"] == ["b@e.com"]
