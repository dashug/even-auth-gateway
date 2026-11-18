import asyncio
import pytest
from even_auth_gov import decision, approval_store

def _setup(monkeypatch, tmp_path, app="app-a"):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    approval_store.mark_pending("ou_a", app, {"name": "申请人"})
    calls = []
    async def fake_add(client, user_id, group): calls.append(("add", user_id, group)); return True
    async def fake_remove(client, user_id, group): calls.append(("remove", user_id, group)); return True
    async def fake_disable(client, user_id): calls.append(("disable", user_id)); return True
    async def no_roles(client, app, known_apps=()): return []   # 默认:该应用没分角色
    async def fake_remove_roles(client, user_id, app=None, keep="", known_apps=()): return True
    monkeypatch.setattr(decision.ca, "add_to_group", fake_add)
    monkeypatch.setattr(decision.ca, "remove_from_group", fake_remove)
    monkeypatch.setattr(decision.ca, "disable_user", fake_disable)
    monkeypatch.setattr(decision.ca, "list_app_roles", no_roles)
    monkeypatch.setattr(decision.ca, "remove_roles", fake_remove_roles)
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
    async def no_roles(client, app, known_apps=()): return []
    monkeypatch.setattr(decision.ca, "add_to_group", fake_add)
    monkeypatch.setattr(decision.ca, "list_app_roles", no_roles)
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


# ── 结果回告:只有真正发生状态转换时带 decision 字段(供卡片回调通知申请人) ──

def test_result_carries_decision_only_on_transition(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    r1 = asyncio.run(decision.handle("sso_approve", operator_id="ou_boss", owner="ou_boss", sso_open_id="ou_a", app="app-a", client=None))
    assert r1["decision"] == "approved"
    r2 = asyncio.run(decision.handle("sso_approve", operator_id="ou_boss", owner="ou_boss", sso_open_id="ou_a", app="app-a", client=None))
    assert "decision" not in r2          # 重复点击 = 幂等,不再通知申请人

def test_deny_result_carries_denied(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    r = asyncio.run(decision.handle("sso_deny", operator_id="ou_boss", owner="ou_boss", sso_open_id="ou_a", app="app-a", client=None))
    assert r["decision"] == "denied"

def test_failed_approve_carries_no_decision(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    async def fail_add(client, user_id, group): return False
    monkeypatch.setattr(decision.ca, "add_to_group", fail_add)
    r = asyncio.run(decision.handle("sso_approve", operator_id="ou_boss", owner="ou_boss", sso_open_id="ou_a", app="app-a", client=None))
    assert r["status"] == "error" and "decision" not in r

def test_unknown_action_is_rejected_not_treated_as_deny(monkeypatch, tmp_path):
    """卡片 value 被篡改/缺 action 时不能默认走拒绝分支(会把正常申请误标 denied)。"""
    calls = _setup(monkeypatch, tmp_path)
    r = asyncio.run(decision.handle("", operator_id="ou_boss", owner="ou_boss", sso_open_id="ou_a", app="app-a", client=None))
    assert r["status"] == "error"
    assert calls == []
    assert approval_store.get("ou_a", "app-a")["status"] == "pending"


# ── 审批即赋角(F-5.5.3):卡片按钮带 role → 先赋角色、再加准入组(组是门,最后开) ──

ROLES = [{"name": "app-a-viewer", "display": "查看者"}, {"name": "app-a-editor", "display": "编辑者"}]

def _setup_roles(monkeypatch, tmp_path, roles=ROLES, assign_ok=True, cleanup=True):
    """cleanup: remove_roles 的返回值(True/False),或 "raise" = 抛异常。"""
    calls = _setup(monkeypatch, tmp_path)
    async def fake_list(client, app, known_apps=()): calls.append(("list", app)); return roles
    async def fake_assign(client, user_id, role): calls.append(("assign", user_id, role)); return assign_ok
    async def fake_remove_roles(client, user_id, app=None, keep="", known_apps=()):
        calls.append(("remove_roles", user_id, app, keep))
        if cleanup == "raise":
            raise RuntimeError("casdoor down")
        return cleanup
    monkeypatch.setattr(decision.ca, "list_app_roles", fake_list)
    monkeypatch.setattr(decision.ca, "assign_role", fake_assign)
    monkeypatch.setattr(decision.ca, "remove_roles", fake_remove_roles)
    return calls

def _approve(role):
    return asyncio.run(decision.handle("sso_approve", operator_id="ou_boss", owner="ou_boss",
                                       sso_open_id="ou_a", app="app-a", client=None, role=role))

def test_approve_with_role_assigns_role_then_opens_group(monkeypatch, tmp_path):
    calls = _setup_roles(monkeypatch, tmp_path)
    r = _approve("app-a-editor")
    assert r["status"] == "ok" and r["decision"] == "approved" and r["role"] == "编辑者"
    assert "编辑者" in r["message"]
    assert [c[0] for c in calls if c[0] in ("assign", "add")] == ["assign", "add"]
    assert ("assign", "ou_a", "app-a-editor") in calls
    rec = approval_store.get("ou_a", "app-a")
    assert rec["status"] == "approved" and rec["role"] == "app-a-editor"

def test_role_assign_failure_keeps_pending_and_group_closed(monkeypatch, tmp_path):
    calls = _setup_roles(monkeypatch, tmp_path, assign_ok=False)
    r = _approve("app-a-editor")
    assert r["status"] == "error" and "decision" not in r
    assert not any(c[0] == "add" for c in calls)
    assert approval_store.get("ou_a", "app-a")["status"] == "pending"

def test_role_not_belonging_to_app_is_rejected(monkeypatch, tmp_path):
    """回调里的 role 再核一次:必须是该应用当前在 Casdoor 里启用的角色。"""
    calls = _setup_roles(monkeypatch, tmp_path)
    r = _approve("app-b-admin")
    assert r["status"] == "error"
    assert not any(c[0] in ("assign", "add") for c in calls)
    assert approval_store.get("ou_a", "app-a")["status"] == "pending"

def test_role_list_unavailable_rejects_with_retry(monkeypatch, tmp_path):
    calls = _setup_roles(monkeypatch, tmp_path, roles=None)
    r = _approve("app-a-editor")
    assert r["status"] == "error" and "再点" in r["message"]
    assert not any(c[0] in ("assign", "add") for c in calls)
    assert approval_store.get("ou_a", "app-a")["status"] == "pending"

def test_approve_without_role_on_app_without_roles_does_not_touch_roles(monkeypatch, tmp_path):
    calls = _setup_roles(monkeypatch, tmp_path, roles=[])
    r = _approve("")
    assert r["status"] == "ok" and "role" not in r
    assert not any(c[0] in ("assign", "remove_roles") for c in calls)
    assert "role" not in approval_store.get("ou_a", "app-a")


# ── 角色强制(里程碑 3):分了角色的应用,批准必须带角色 ──
# 卡片在读不到角色清单时会降级成普通"批准"按钮;那张卡对分了角色的应用不能用,
# 拒绝并让卡片回调重发一张带角色按钮的卡片(resend_card)。

def test_plain_approve_rejected_when_app_has_roles(monkeypatch, tmp_path):
    calls = _setup_roles(monkeypatch, tmp_path)
    r = _approve("")
    assert r["status"] == "error" and r.get("resend_card") is True and "decision" not in r
    assert "角色" in r["message"]
    assert not any(c[0] in ("assign", "add", "remove_roles") for c in calls)
    assert approval_store.get("ou_a", "app-a")["status"] == "pending"

def test_plain_approve_with_role_list_unavailable_asks_retry(monkeypatch, tmp_path):
    calls = _setup_roles(monkeypatch, tmp_path, roles=None)
    r = _approve("")
    assert r["status"] == "error" and "再点" in r["message"] and not r.get("resend_card")
    assert not any(c[0] == "add" for c in calls)
    assert approval_store.get("ou_a", "app-a")["status"] == "pending"


# ── 角色不留残余:"批准为「X」"= 该应用只留 X;拒绝 = 撤掉该应用全部角色 ──

def test_approve_with_role_first_clears_other_roles_of_that_app(monkeypatch, tmp_path):
    """修"上次赋角成功、加组失败,审批人改点另一个角色 → 两个角色都在"。"""
    calls = _setup_roles(monkeypatch, tmp_path)
    r = _approve("app-a-editor")
    assert r["status"] == "ok"
    assert ("remove_roles", "ou_a", "app-a", "app-a-editor") in calls
    assert [c[0] for c in calls if c[0] in ("remove_roles", "assign", "add")] == ["remove_roles", "assign", "add"]

def test_approve_role_cleanup_failure_keeps_pending_and_group_closed(monkeypatch, tmp_path):
    calls = _setup_roles(monkeypatch, tmp_path, cleanup=False)
    r = _approve("app-a-editor")
    assert r["status"] == "error" and "再点" in r["message"] and "decision" not in r
    assert not any(c[0] in ("assign", "add") for c in calls)
    assert approval_store.get("ou_a", "app-a")["status"] == "pending"


def test_deny_removes_that_apps_roles(monkeypatch, tmp_path):
    calls = _setup_roles(monkeypatch, tmp_path)
    r = asyncio.run(decision.handle("sso_deny", operator_id="ou_boss", owner="ou_boss", sso_open_id="ou_a", app="app-a", client=None))
    assert r["status"] == "ok" and r["decision"] == "denied"
    assert ("remove_roles", "ou_a", "app-a", "") in calls

@pytest.mark.parametrize("cleanup", [False, "raise"])
def test_deny_role_removal_failure_is_non_fatal(monkeypatch, tmp_path, cleanup):
    """拒绝是审批人的决定,撤角色失败只记日志,不影响拒绝生效(没有准入组,残余角色进不了应用)。"""
    calls = _setup_roles(monkeypatch, tmp_path, cleanup=cleanup)
    r = asyncio.run(decision.handle("sso_deny", operator_id="ou_boss", owner="ou_boss", sso_open_id="ou_a", app="app-a", client=None))
    assert r["status"] == "ok"
    assert approval_store.get("ou_a", "app-a")["status"] == "denied"
    assert any(c[0] == "remove_roles" for c in calls)


# ── 超时升级:升级对象和原审批人都能处理这条申请,谁先点算谁 ──

def test_escalation_target_can_decide_and_is_recorded(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path)
    approval_store.mark_escalated("ou_a", "app-a", "ou_esc")
    r = asyncio.run(decision.handle("sso_approve", operator_id="ou_esc", owner="ou_boss", sso_open_id="ou_a", app="app-a", client=None))
    assert r["status"] == "ok" and r["decision"] == "approved"
    assert ("add", "ou_a", "approved-app-a") in calls
    assert approval_store.get("ou_a", "app-a")["approved_by"] == "ou_esc"

def test_escalation_target_of_another_record_cannot_decide(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path)
    approval_store.mark_pending("ou_other", "app-a", {"name": "别人"})
    approval_store.mark_escalated("ou_other", "app-a", "ou_esc")     # 升级的是别人的申请
    r = asyncio.run(decision.handle("sso_approve", operator_id="ou_esc", owner="ou_boss", sso_open_id="ou_a", app="app-a", client=None))
    assert r["status"] == "error" and calls == []
    assert approval_store.get("ou_a", "app-a")["status"] == "pending"


# ── 里程碑 4a:非飞书账号闸门 ──

from datetime import timedelta
from even_auth_gov import accounts


def _external(props=None, forbidden=False):
    return {"id": "x_ext", "owner": "even-test", "name": "x_ext", "isForbidden": forbidden, "properties": props or {}}


def _good_props(**over):
    p = {"evauth_kind": "partner", "evauth_sponsor": "ou_boss",
         "evauth_expires": (accounts.today() + timedelta(days=30)).isoformat(),
         "evauth_confirmed_at": "2026-11-01T00:00:00+00:00"}
    p.update(over)
    return p


def _setup_external(monkeypatch, tmp_path, user):
    calls = _setup(monkeypatch, tmp_path)
    approval_store.mark_pending("x_ext", "app-a", {"name": "张三"})
    async def fake_get_user(client, open_id): return user
    monkeypatch.setattr(decision.ca, "get_user", fake_get_user)
    return calls


def _approve_ext():
    return asyncio.run(decision.handle("sso_approve", operator_id="ou_boss", owner="ou_boss",
                                       sso_open_id="x_ext", app="app-a", client=None))


@pytest.mark.parametrize("user,reason", [
    (_external({}), "unregistered"),
    (_external(_good_props(evauth_expires="2020-01-01")), "expired"),
    (_external(_good_props(evauth_confirmed_at=None)), "unconfirmed"),
    (_external(_good_props(), forbidden=True), "disabled"),
])
def test_approving_ineligible_external_account_is_auto_denied(monkeypatch, tmp_path, user, reason):
    props = {k: v for k, v in user["properties"].items() if v is not None}
    calls = _setup_external(monkeypatch, tmp_path, {**user, "properties": props})
    r = _approve_ext()
    assert r["status"] == "ok" and r["decision"] == "denied" and "已自动拒绝" in r["message"]
    rec = approval_store.get("x_ext", "app-a")
    assert (rec["status"], rec["denied_by"], rec["denied_reason"]) == ("denied", "system", f"ineligible:{reason}")
    assert not any(c[0] == "add" for c in calls)


def test_approving_external_when_casdoor_unreadable_stays_pending(monkeypatch, tmp_path):
    _setup_external(monkeypatch, tmp_path, None)
    r = _approve_ext()
    assert r["status"] == "error" and approval_store.get("x_ext", "app-a")["status"] == "pending"


def test_approving_eligible_external_account_proceeds(monkeypatch, tmp_path):
    calls = _setup_external(monkeypatch, tmp_path, _external(_good_props()))
    assert _approve_ext()["decision"] == "approved"
    assert ("add", "x_ext", "approved-app-a") in calls
