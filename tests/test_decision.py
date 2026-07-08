import asyncio
from even_auth_gov import decision, approval_store

def _setup(monkeypatch, tmp_path, app="app-a"):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    approval_store.mark_pending("ou_a", app, {"name": "申请人"})
    calls = []
    async def fake_add(client, user_id, group): calls.append(("add", user_id, group)); return True
    async def fake_remove(client, user_id, group): calls.append(("remove", user_id, group)); return True
    async def fake_disable(client, user_id): calls.append(("disable", user_id)); return True
    monkeypatch.setattr(decision.ca, "add_to_group", fake_add)
    monkeypatch.setattr(decision.ca, "remove_from_group", fake_remove)
    monkeypatch.setattr(decision.ca, "disable_user", fake_disable)
    return calls

def test_approve_by_owner_adds_to_group(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path)
    r = asyncio.run(decision.handle("sso_approve", operator_id="ou_boss", owner="ou_boss", sso_open_id="ou_a", app="app-a", client=None))
    assert r["status"] == "ok"
    assert ("add", "ou_a", "approved-app-a") in calls
    assert approval_store.get("ou_a", "app-a")["status"] == "approved"

def test_deny_by_owner_marks_denied_and_revokes_group(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path)
    r = asyncio.run(decision.handle("sso_deny", operator_id="ou_boss", owner="ou_boss", sso_open_id="ou_a", app="app-a", client=None))
    assert r["status"] == "ok"
    assert approval_store.get("ou_a", "app-a")["status"] == "denied"
    assert ("remove", "ou_a", "approved-app-a") in calls   # #11:拒绝也撤 Casdoor 组

def test_approve_by_stranger_rejected(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path)
    r = asyncio.run(decision.handle("sso_approve", operator_id="ou_x", owner="ou_boss", sso_open_id="ou_a", app="app-a", client=None))
    assert r["status"] == "error" and calls == []
    assert approval_store.get("ou_a", "app-a")["status"] == "pending"

def test_idempotent_after_decision(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    asyncio.run(decision.handle("sso_approve", operator_id="ou_boss", owner="ou_boss", sso_open_id="ou_a", app="app-a", client=None))
    r = asyncio.run(decision.handle("sso_approve", operator_id="ou_boss", owner="ou_boss", sso_open_id="ou_a", app="app-a", client=None))
    assert r["status"] == "ok" and "approved" in r["message"]

def test_unknown_applicant_errors(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    r = asyncio.run(decision.handle("sso_approve", operator_id="ou_boss", owner="ou_boss", sso_open_id="ou_ghost", app="app-a", client=None))
    assert r["status"] == "error"

def test_add_to_group_failure_does_not_mark_approved(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    async def fail_add(client, user_id, group): return False
    monkeypatch.setattr(decision.ca, "add_to_group", fail_add)
    r = asyncio.run(decision.handle("sso_approve", operator_id="ou_boss", owner="ou_boss", sso_open_id="ou_a", app="app-a", client=None))
    assert r["status"] == "error"
    assert approval_store.get("ou_a", "app-a")["status"] == "pending"  # not approved if Casdoor add failed


# ── APP-AWARE (design-review #2/#16) ────────────────────────────────────────

def test_approve_one_app_does_not_touch_other_app_group_or_status(monkeypatch, tmp_path):
    """核心保证:同一用户对 app-a 待批、对 app-b 已批。批准 app-a 只加 approved-app-a
    组、只动 app-a 的记录,app-b 的组成员资格与状态完全不受影响。"""
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    approval_store.mark_pending("ou_multi", "app-a", {"name": "多应用用户"})
    approval_store.mark_pending("ou_multi", "app-b", {"name": "多应用用户"})
    approval_store.mark_approved("ou_multi", "app-b", "ou_boss")
    calls = []
    async def fake_add(client, user_id, group): calls.append(("add", user_id, group)); return True
    monkeypatch.setattr(decision.ca, "add_to_group", fake_add)
    r = asyncio.run(decision.handle("sso_approve", operator_id="ou_boss", owner="ou_boss", sso_open_id="ou_multi", app="app-a", client=None))
    assert r["status"] == "ok"
    assert calls == [("add", "ou_multi", "approved-app-a")]   # app-b 的组从未被碰
    assert approval_store.get("ou_multi", "app-a")["status"] == "approved"
    assert approval_store.get("ou_multi", "app-b")["status"] == "approved"   # 未被重置/覆盖
    assert approval_store.get("ou_multi", "app-b")["approved_by"] == "ou_boss"

def test_pending_check_scoped_to_app_unknown_for_other_app(monkeypatch, tmp_path):
    """对 app-a 有 pending 记录,但对 app-b 从未申请过 —— app-b 的决策应报 Application not found。"""
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    approval_store.mark_pending("ou_a", "app-a", {"name": "X"})
    r = asyncio.run(decision.handle("sso_approve", operator_id="ou_boss", owner="ou_boss", sso_open_id="ou_a", app="app-b", client=None))
    assert r["status"] == "error"
    assert approval_store.get("ou_a", "app-b") is None
