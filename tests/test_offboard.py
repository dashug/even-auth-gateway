import asyncio
from types import SimpleNamespace
from even_auth_gov import offboard, approval_store

def _status(**kw):
    base = dict(is_frozen=False, is_resigned=False, is_exited=False)
    base.update(kw); return SimpleNamespace(**base)

def _no_sleep(monkeypatch):
    async def _s(*a, **k): return None
    monkeypatch.setattr(offboard.asyncio, "sleep", _s)

def _setup(monkeypatch, tmp_path, disable_ok=True, app="app-a"):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    approval_store.mark_pending("ou_l", app, {"name": "离职者"}); approval_store.mark_approved("ou_l", app, "ou_boss")
    calls = []
    async def fake_disable(client, user_id): calls.append(("disable", user_id)); return disable_ok
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
    assert approval_store.get("ou_l", "app-a")["status"] == "disabled"
    assert approval_store.get("ou_l", "app-a")["disabled_reason"] == "offboard_event"
    assert ("remove", "ou_l", "approved-app-a") in calls   # 按该 app 的准入组移组

def test_apply_always_attempts_disable_even_if_locally_disabled(monkeypatch, tmp_path):
    # #14 修复:不信本地 store。本地记 disabled 但用户可能被人工在 Casdoor 重启用 → 仍幂等禁用一次,不盲跳。
    calls = _setup(monkeypatch, tmp_path)
    approval_store.mark_all_disabled("ou_l", "prior")
    ok = asyncio.run(offboard.apply("ou_l", "离职者", "offboard_event", client=None))
    assert ok is True
    assert ("disable", "ou_l") in calls   # 关键:仍调了 disable(幂等),不再盲信本地状态

def test_apply_empty_open_id_returns_false(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    assert asyncio.run(offboard.apply("", "x", "r", client=None)) is False

def test_apply_disable_failure_retries_marks_failed_and_alerts(monkeypatch, tmp_path):
    # #4 修复:禁用失败 → 退避重试 3 次 → 仍失败则标 disable_failed + 飞书告警审批人。
    calls = _setup(monkeypatch, tmp_path, disable_ok=False)
    _no_sleep(monkeypatch)
    monkeypatch.setenv("APPROVER_FEISHU_ID", "ou_boss")
    alerts = []
    async def fake_send(receive_id, text, **k): alerts.append((receive_id, text)); return True
    monkeypatch.setattr("even_auth_gov.feishu.send_text", fake_send)
    ok = asyncio.run(offboard.apply("ou_l", "离职者", "offboard_event", client=None))
    assert ok is False
    assert sum(1 for c in calls if c[0] == "disable") == 3           # 重试 3 次
    assert approval_store.get("ou_l", "app-a")["status"] == "disable_failed"  # 不再留在 approved
    assert len(alerts) == 1 and alerts[0][0] == "ou_boss"           # 告警审批人

def test_apply_disable_retries_then_succeeds(monkeypatch, tmp_path):
    # 前一次失败、后一次成功 → 最终 disabled,不告警。
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    _no_sleep(monkeypatch)
    approval_store.mark_pending("ou_l", "app-a", {"name": "x"}); approval_store.mark_approved("ou_l", "app-a", "b")
    seq = [False, True]
    async def flaky(client, user_id): return seq.pop(0)
    async def fake_remove(client, user_id, group): return True
    monkeypatch.setattr(offboard.ca, "disable_user", flaky)
    monkeypatch.setattr(offboard.ca, "remove_from_group", fake_remove)
    ok = asyncio.run(offboard.apply("ou_l", "x", "r", client=None))
    assert ok is True
    assert approval_store.get("ou_l", "app-a")["status"] == "disabled"


# ── APP-AWARE (design-review #2/#16): offboarding is per-user, not per-app ──

def test_apply_user_approved_for_two_apps_disables_user_once_marks_both_removes_both_groups(monkeypatch, tmp_path):
    """离职是按用户的:Casdoor disable_user 只调一次(禁用整个账号),但两个 app 的
    approval_store 记录都要转 disabled,两个 app 的准入组都要尝试移除。"""
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    approval_store.mark_pending("ou_l", "app-a", {"name": "离职者"}); approval_store.mark_approved("ou_l", "app-a", "b")
    approval_store.mark_pending("ou_l", "app-b", {"name": "离职者"}); approval_store.mark_approved("ou_l", "app-b", "b")
    calls = []
    async def fake_disable(client, user_id): calls.append(("disable", user_id)); return True
    async def fake_remove(client, user_id, group): calls.append(("remove", user_id, group)); return True
    monkeypatch.setattr(offboard.ca, "disable_user", fake_disable)
    monkeypatch.setattr(offboard.ca, "remove_from_group", fake_remove)
    ok = asyncio.run(offboard.apply("ou_l", "离职者", "offboard_event", client=None))
    assert ok is True
    assert sum(1 for c in calls if c[0] == "disable") == 1   # Casdoor 账号只禁用一次
    removed_groups = {c[2] for c in calls if c[0] == "remove"}
    assert removed_groups == {"approved-app-a", "approved-app-b"}   # 两个 app 的组都移除
    assert approval_store.get("ou_l", "app-a")["status"] == "disabled"
    assert approval_store.get("ou_l", "app-b")["status"] == "disabled"

def test_apply_remove_from_group_failure_for_one_app_does_not_block_the_other(monkeypatch, tmp_path):
    """移组是尽力而为(非致命);其中一个 app 的移组失败,不该影响另一个 app 的移组或最终禁用结果。"""
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    approval_store.mark_pending("ou_l", "app-a", {"name": "离职者"}); approval_store.mark_approved("ou_l", "app-a", "b")
    approval_store.mark_pending("ou_l", "app-b", {"name": "离职者"}); approval_store.mark_approved("ou_l", "app-b", "b")
    calls = []
    async def fake_disable(client, user_id): return True
    async def flaky_remove(client, user_id, group):
        calls.append(group)
        if group == "approved-app-a":
            raise RuntimeError("casdoor down")
        return True
    monkeypatch.setattr(offboard.ca, "disable_user", fake_disable)
    monkeypatch.setattr(offboard.ca, "remove_from_group", flaky_remove)
    ok = asyncio.run(offboard.apply("ou_l", "离职者", "offboard_event", client=None))
    assert ok is True
    assert set(calls) == {"approved-app-a", "approved-app-b"}
    assert approval_store.get("ou_l", "app-a")["status"] == "disabled"
    assert approval_store.get("ou_l", "app-b")["status"] == "disabled"

def test_apply_disable_failed_marks_all_app_records(monkeypatch, tmp_path):
    """#4 兜底:禁用重试用尽仍失败时,该用户名下所有 app 记录都标 disable_failed(而不只是一个)。"""
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    _no_sleep(monkeypatch)
    approval_store.mark_pending("ou_l", "app-a", {"name": "x"}); approval_store.mark_approved("ou_l", "app-a", "b")
    approval_store.mark_pending("ou_l", "app-b", {"name": "x"}); approval_store.mark_approved("ou_l", "app-b", "b")
    async def fake_disable(client, user_id): return False
    monkeypatch.setattr(offboard.ca, "disable_user", fake_disable)
    ok = asyncio.run(offboard.apply("ou_l", "x", "r", client=None))
    assert ok is False
    assert approval_store.get("ou_l", "app-a")["status"] == "disable_failed"
    assert approval_store.get("ou_l", "app-b")["status"] == "disable_failed"
