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
