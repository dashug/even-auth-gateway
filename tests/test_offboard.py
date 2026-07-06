import asyncio
from types import SimpleNamespace
from even_auth_gov import offboard, approval_store

def _status(**kw):
    base = dict(is_frozen=False, is_resigned=False, is_exited=False)
    base.update(kw); return SimpleNamespace(**base)

def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    approval_store.mark_pending("ou_l", {"name": "离职者"}); approval_store.mark_approved("ou_l", "ou_boss")
    calls = []
    async def fake_disable(client, user_id): calls.append(("disable", user_id)); return True
    async def fake_remove(client, user_id, group): calls.append(("remove", user_id, group)); return True
    monkeypatch.setattr(offboard.ca, "disable_user", fake_disable)
    monkeypatch.setattr(offboard.ca, "remove_from_group", fake_remove)
    return calls

def test_flag_helper():
    assert offboard.offboard_flags(_status(is_frozen=True)) is True
    assert offboard.offboard_flags(_status(is_resigned=True)) is True
    assert offboard.offboard_flags(_status(is_exited=True)) is True
    assert offboard.offboard_flags(_status()) is False
    assert offboard.offboard_flags(None) is False

def test_apply_disables_and_records(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path)
    ok = asyncio.run(offboard.apply("ou_l", "离职者", "offboard_event", client=None))
    assert ok is True
    assert ("disable", "ou_l") in calls
    assert approval_store.get("ou_l")["status"] == "disabled"
    assert approval_store.get("ou_l")["disabled_reason"] == "offboard_event"

def test_apply_already_disabled_is_noop(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path)
    approval_store.mark_disabled("ou_l", "prior")
    ok = asyncio.run(offboard.apply("ou_l", "离职者", "offboard_event", client=None))
    assert ok is True and calls == []   # already disabled → no Casdoor call, no re-record
    assert approval_store.get("ou_l")["disabled_reason"] == "prior"

def test_apply_empty_open_id_returns_false(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    assert asyncio.run(offboard.apply("", "x", "r", client=None)) is False

def test_apply_disable_failure_returns_false_not_recorded(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    async def fail_disable(client, user_id): return False
    monkeypatch.setattr(offboard.ca, "disable_user", fail_disable)
    ok = asyncio.run(offboard.apply("ou_l", "离职者", "offboard_event", client=None))
    assert ok is False
    assert approval_store.get("ou_l")["status"] == "approved"   # not marked disabled if Casdoor disable failed
