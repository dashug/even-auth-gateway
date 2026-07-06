import asyncio
from even_auth_gov import decision, approval_store

def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    approval_store.mark_pending("ou_a", {"name": "申请人"})
    calls = []
    async def fake_add(client, user_id, group): calls.append(("add", user_id, group)); return True
    async def fake_disable(client, user_id): calls.append(("disable", user_id)); return True
    monkeypatch.setattr(decision.ca, "add_to_group", fake_add)
    monkeypatch.setattr(decision.ca, "disable_user", fake_disable)
    return calls

def test_approve_by_owner_adds_to_group(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path)
    r = asyncio.run(decision.handle("sso_approve", operator_id="ou_boss", owner="ou_boss", sso_open_id="ou_a", client=None))
    assert r["status"] == "ok"
    assert ("add", "ou_a", "approved-operators") in calls
    assert approval_store.get("ou_a")["status"] == "approved"

def test_deny_by_owner_marks_denied(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    r = asyncio.run(decision.handle("sso_deny", operator_id="ou_boss", owner="ou_boss", sso_open_id="ou_a", client=None))
    assert r["status"] == "ok"
    assert approval_store.get("ou_a")["status"] == "denied"

def test_approve_by_stranger_rejected(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path)
    r = asyncio.run(decision.handle("sso_approve", operator_id="ou_x", owner="ou_boss", sso_open_id="ou_a", client=None))
    assert r["status"] == "error" and calls == []
    assert approval_store.get("ou_a")["status"] == "pending"

def test_idempotent_after_decision(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    asyncio.run(decision.handle("sso_approve", operator_id="ou_boss", owner="ou_boss", sso_open_id="ou_a", client=None))
    r = asyncio.run(decision.handle("sso_approve", operator_id="ou_boss", owner="ou_boss", sso_open_id="ou_a", client=None))
    assert r["status"] == "ok" and "approved" in r["message"]

def test_unknown_applicant_errors(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    r = asyncio.run(decision.handle("sso_approve", operator_id="ou_boss", owner="ou_boss", sso_open_id="ou_ghost", client=None))
    assert r["status"] == "error"

def test_add_to_group_failure_does_not_mark_approved(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    async def fail_add(client, user_id, group): return False
    monkeypatch.setattr(decision.ca, "add_to_group", fail_add)
    r = asyncio.run(decision.handle("sso_approve", operator_id="ou_boss", owner="ou_boss", sso_open_id="ou_a", client=None))
    assert r["status"] == "error"
    assert approval_store.get("ou_a")["status"] == "pending"  # not approved if Casdoor add failed
