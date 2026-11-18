"""受管账号生命周期(里程碑 4a):真实代码 + 内存版 Casdoor,飞书发送改成捕获。"""
import asyncio
import json
from datetime import timedelta

import pytest

from even_auth_gov import account_lifecycle as lc, accounts as acc, approval_store
from tests._fake_casdoor import FakeCasdoor


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("CASDOOR_ORG", "even-test")
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    monkeypatch.setenv("APPROVER_FEISHU_ID", "ou_root")
    monkeypatch.delenv("ACCOUNT_EVENTS_FILE", raising=False)
    sent = {"cards": [], "texts": [], "card_ok": True}
    async def send_card(to, card, receive_id_type="open_id"):
        sent["cards"].append((to, card)); return sent["card_ok"]
    async def send_text(to, text, receive_id_type="open_id"):
        sent["texts"].append((to, text)); return True
    monkeypatch.setattr("even_auth_gov.feishu.send_card", send_card)
    monkeypatch.setattr("even_auth_gov.feishu.send_text", send_text)
    async def no_sleep(*a, **k): return None
    monkeypatch.setattr("even_auth_gov.offboard.asyncio.sleep", no_sleep)
    fc = FakeCasdoor()
    fc.add(id="ou_boss", displayName="李四", email="boss@e.com")
    fc.add(id="ou_other", displayName="王五", email="other@e.com")
    return {"fc": fc, "sent": sent, "dir": tmp_path}


def run(fc, fn):
    async def go():
        async with fc.client() as c:
            return await fn(c)
    return asyncio.run(go())


def day(n: int) -> str:
    return (acc.today() + timedelta(days=n)).isoformat()


def events(env):
    path = env["dir"] / "account_events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def buttons(card):
    return [b["value"] for b in card["elements"][-1]["actions"]]


def create(env, **over):
    args = dict(kind="partner", name="张三", email="z@partner.com", sponsor_email="boss@e.com", expires=day(30))
    args.update(over)
    return run(env["fc"], lambda c: lc.create(c, **args))


def click(env, action, operator, user_id):
    return run(env["fc"], lambda c: lc.handle_action(action, operator, user_id, c))


# ── 建号 ──

def test_create_builds_disabled_account_and_asks_sponsor(env):
    r = create(env)
    u = env["fc"].users[r["user_id"]]
    assert r["user_id"].startswith("x_") and len(r["password"]) == 20 and r["confirm_sent"] is True
    assert u["isForbidden"] is True and u["needUpdatePassword"] is True and u["email"] == "z@partner.com"
    assert u["properties"] == {"evauth_kind": "partner", "evauth_sponsor": "ou_boss",
                               "evauth_expires": day(30), "evauth_origin": "created"}
    to, card = env["sent"]["cards"][0]
    assert to == "ou_boss"
    assert buttons(card) == [{"action": "acct_confirm", "account_id": r["user_id"]},
                             {"action": "acct_reject", "account_id": r["user_id"]}]
    assert [e["event"] for e in events(env)] == ["created"]
    assert r["password"] not in (env["dir"] / "account_events.jsonl").read_text(encoding="utf-8")


def test_service_account_is_not_forced_to_change_password(env):
    r = create(env, kind="service")
    assert env["fc"].users[r["user_id"]]["needUpdatePassword"] is False


@pytest.mark.parametrize("over,msg", [
    ({"kind": "vendor"}, "类型"),
    ({"expires": "2020-01-01"}, "早于今天"),
    ({"expires": day(181)}, "最长"),
    ({"sponsor_email": "nobody@e.com"}, "不是飞书账号"),
    ({"email": "boss@e.com"}, "已被账号"),
    ({"name": " "}, "姓名"),
])
def test_create_rejects_bad_input_and_creates_nothing(env, over, msg):
    before = set(env["fc"].users)
    with pytest.raises(lc.AccountError, match=msg):
        create(env, **over)
    assert set(env["fc"].users) == before and env["sent"]["cards"] == []


def test_create_rejects_disabled_or_non_feishu_sponsor(env):
    env["fc"].users["ou_boss"]["isForbidden"] = True
    with pytest.raises(lc.AccountError, match="已停用"):
        create(env)
    env["fc"].add(id="x_svc", email="svc@e.com", properties={"evauth_kind": "service"})
    with pytest.raises(lc.AccountError, match="不是飞书账号"):
        create(env, sponsor_email="svc@e.com")


def test_create_aborts_when_casdoor_unreadable(env):
    env["fc"].fail.add("/api/get-user")
    with pytest.raises(lc.AccountError, match="读不到 Casdoor"):
        create(env)


def test_create_reports_card_failure_but_keeps_account(env):
    env["sent"]["card_ok"] = False
    r = create(env)
    assert r["confirm_sent"] is False and r["user_id"] in env["fc"].users


# ── 负责人确认 / 拒绝 ──

def test_sponsor_confirm_enables_created_account(env):
    r = create(env)
    msg = click(env, "acct_confirm", "ou_boss", r["user_id"])
    u = env["fc"].users[r["user_id"]]
    assert "账号已启用" in msg and u["isForbidden"] is False and u["properties"]["evauth_confirmed_at"]
    assert acc.eligibility(u, acc.today()) == ""
    assert [e["event"] for e in events(env)] == ["created", "confirmed"]


def test_only_the_sponsor_can_confirm(env):
    r = create(env)
    assert "只有该账号的负责人" in click(env, "acct_confirm", "ou_other", r["user_id"])
    assert env["fc"].users[r["user_id"]]["isForbidden"] is True


def test_confirm_after_expiry_does_not_enable(env):
    r = create(env)
    env["fc"].props(r["user_id"])["evauth_expires"] = day(-1)
    assert "没有启用" in click(env, "acct_confirm", "ou_boss", r["user_id"])
    assert env["fc"].users[r["user_id"]]["isForbidden"] is True


def test_sponsor_reject_keeps_created_account_disabled(env):
    r = create(env)
    assert "保持停用" in click(env, "acct_reject", "ou_boss", r["user_id"])
    u = env["fc"].users[r["user_id"]]
    assert u["isForbidden"] is True and u["properties"]["evauth_rejected_at"]
    assert "拒绝过" in click(env, "acct_confirm", "ou_boss", r["user_id"])


def test_repeat_confirm_is_idempotent(env):
    r = create(env)
    click(env, "acct_confirm", "ou_boss", r["user_id"])
    assert "已确认过" in click(env, "acct_confirm", "ou_boss", r["user_id"])


def test_unknown_account_or_not_managed(env):
    assert "找不到" in click(env, "acct_confirm", "ou_boss", "x_none")
    assert "不是受管账号" in click(env, "acct_confirm", "ou_boss", "ou_other")


# ── 纳管存量账号 ──

def _legacy(env, forbidden=False):
    return env["fc"].add(id="uuid-legacy", email="legacy@partner.com", displayName="老外包", isForbidden=forbidden,
                         groups=["even-test/approved-app-a"])


def test_register_adopts_existing_account_without_touching_state(env):
    _legacy(env)
    r = run(env["fc"], lambda c: lc.register(c, "legacy@partner.com", "partner", "boss@e.com", day(60)))
    u = env["fc"].users["uuid-legacy"]
    assert r["confirm_sent"] is True and u["isForbidden"] is False and u["groups"] == ["even-test/approved-app-a"]
    assert u["properties"]["evauth_origin"] == "registered" and u["properties"]["evauth_sponsor"] == "ou_boss"
    assert acc.eligibility(u, acc.today()) == "unconfirmed"
    msg = click(env, "acct_confirm", "ou_boss", "uuid-legacy")
    assert "已启用" not in msg and env["fc"].users["uuid-legacy"]["isForbidden"] is False


def test_confirm_does_not_enable_a_disabled_registered_account(env):
    _legacy(env, forbidden=True)
    run(env["fc"], lambda c: lc.register(c, "uuid-legacy", "staff", "boss@e.com", day(60)))
    click(env, "acct_confirm", "ou_boss", "uuid-legacy")
    assert env["fc"].users["uuid-legacy"]["isForbidden"] is True


def test_rejecting_an_enabled_registered_account_disables_it(env):
    _legacy(env)
    run(env["fc"], lambda c: lc.register(c, "uuid-legacy", "partner", "boss@e.com", day(60)))
    assert "已停用" in click(env, "acct_reject", "ou_boss", "uuid-legacy")
    u = env["fc"].users["uuid-legacy"]
    assert u["isForbidden"] is True and u["groups"] == []


def test_register_refuses_feishu_and_already_managed(env):
    with pytest.raises(lc.AccountError, match="飞书账号"):
        run(env["fc"], lambda c: lc.register(c, "ou_other", "partner", "boss@e.com", day(30)))
    r = create(env)
    with pytest.raises(lc.AccountError, match="已经是受管账号"):
        run(env["fc"], lambda c: lc.register(c, r["user_id"], "partner", "boss@e.com", day(30)))


def test_resend_confirm(env):
    r = create(env)
    assert run(env["fc"], lambda c: lc.resend_confirm(c, "z@partner.com")) is True
    assert len(env["sent"]["cards"]) == 2
    click(env, "acct_confirm", "ou_boss", r["user_id"])
    with pytest.raises(lc.AccountError, match="不需要重发"):
        run(env["fc"], lambda c: lc.resend_confirm(c, r["user_id"]))


# ── 到期扫描、续期提醒、续期 ──

def active(env, uid="x_a", sponsor="ou_boss", expires=None, groups=("even-test/approved-app-a",), **props):
    p = {"evauth_kind": "partner", "evauth_sponsor": sponsor, "evauth_expires": expires or day(30),
         "evauth_confirmed_at": "2026-11-01T00:00:00+00:00", "evauth_origin": "created"}
    p.update(props)
    return env["fc"].add(id=uid, displayName=f"受管{uid}", email=f"{uid}@p.com", groups=list(groups),
                         properties={k: v for k, v in p.items() if v is not None})


def sweep(env):
    run(env["fc"], lc.sweep)


@pytest.mark.parametrize("over,reason,event", [
    ({"expires": "2020-01-01"}, "account_expired", "expired"),
    ({"evauth_kind": "vendor"}, "account_invalid", "invalid"),
    ({"evauth_rejected_at": "2026-11-02T00:00:00+00:00"}, "sponsor_rejected", "rejected_disabled"),
])
def test_sweep_disables_lapsed_accounts_via_offboard(env, over, reason, event):
    approval_store.mark_pending("x_a", "app-a", {"name": "a"})
    approval_store.mark_approved("x_a", "app-a", "ou_appr")
    active(env, **over)
    sweep(env)
    u = env["fc"].users["x_a"]
    assert u["isForbidden"] is True and u["groups"] == []
    rec = approval_store.get("x_a", "app-a")
    assert rec["status"] == "disabled" and rec["disabled_reason"] == reason
    assert events(env)[-1]["event"] == event


def test_sweep_disables_when_sponsor_is_disabled_or_gone(env):
    env["fc"].users["ou_other"]["isForbidden"] = True
    active(env, uid="x_a", sponsor="ou_other")
    active(env, uid="x_b", sponsor="ou_ghost")
    sweep(env)
    assert env["fc"].users["x_a"]["isForbidden"] and env["fc"].users["x_b"]["isForbidden"]
    assert any(to == "ou_root" and "负责人已停用" in t for to, t in env["sent"]["texts"])


def test_expiry_notice_goes_to_sponsor(env):
    active(env, expires="2020-01-01")
    sweep(env)
    assert any(to == "ou_boss" and "已停用" in t for to, t in env["sent"]["texts"])


def test_sweep_leaves_feishu_unregistered_disabled_and_healthy_accounts_alone(env):
    env["fc"].add(id="uuid-s", email="s@x.com", groups=["even-test/approved-app-a"])   # 未登记:只拦不停
    active(env, uid="x_off", expires="2020-01-01", groups=())
    env["fc"].users["x_off"]["isForbidden"] = True
    active(env, uid="x_ok")
    sweep(env)
    assert env["fc"].users["uuid-s"]["isForbidden"] is False
    assert env["fc"].users["x_ok"]["isForbidden"] is False
    assert env["fc"].users["ou_boss"]["isForbidden"] is False


def test_sweep_skips_everything_when_users_unreadable(env):
    active(env, expires="2020-01-01")
    env["fc"].fail.add("/api/get-users")
    sweep(env)
    assert env["fc"].users["x_a"]["isForbidden"] is False


def test_reminders_once_per_stage(env, monkeypatch):
    exp = day(14)                      # 先存下来:下面会把"今天"往后挪,day() 会跟着变
    active(env, expires=exp)
    sweep(env); sweep(env)
    assert len(env["sent"]["cards"]) == 1
    to, card = env["sent"]["cards"][0]
    assert to == "ou_boss" and [b["action"] for b in buttons(card)] == ["acct_renew", "acct_no_renew"]
    assert env["fc"].props("x_a")["evauth_reminded"] == f"{exp}:14"
    real_today = acc.today()
    monkeypatch.setattr(acc, "today", lambda: real_today + timedelta(days=11))   # 剩 3 天
    sweep(env)
    assert len(env["sent"]["cards"]) == 2 and env["fc"].props("x_a")["evauth_reminded"] == f"{exp}:3"


def test_reminder_send_failure_is_retried(env):
    active(env, expires=day(3))
    env["sent"]["card_ok"] = False
    sweep(env)
    assert "evauth_reminded" not in env["fc"].props("x_a")


def test_sponsor_renews_from_card(env):
    active(env, expires=day(10), evauth_reminded=f"{day(10)}:14")
    msg = click(env, "acct_renew", "ou_boss", "x_a")
    assert day(100) in msg
    p = env["fc"].props("x_a")
    assert p["evauth_expires"] == day(100) and "evauth_reminded" not in p
    assert events(env)[-1]["event"] == "renewed"
    assert "只有该账号的负责人" in click(env, "acct_renew", "ou_other", "x_a")


def test_no_renew_stops_reminders(env, monkeypatch):
    active(env, expires=day(14))
    assert "不再提醒" in click(env, "acct_no_renew", "ou_boss", "x_a")
    real_today = acc.today()
    monkeypatch.setattr(acc, "today", lambda: real_today + timedelta(days=12))
    sweep(env)
    assert env["sent"]["cards"] == []


def test_renew_after_expiry_reenables_and_allows_reapplying(env):
    approval_store.mark_pending("x_a", "app-a", {"name": "a"})
    approval_store.mark_approved("x_a", "app-a", "ou_appr")
    active(env, expires="2020-01-01")
    sweep(env)
    assert approval_store.mark_pending("x_a", "app-a", {"name": "a"}) is False   # 停用记录挡住重新申请
    r = run(env["fc"], lambda c: lc.renew(c, "x_a", actor="admin"))
    assert r["enabled"] is True and r["reopened"] == 1 and r["expires"] == day(90)
    assert env["fc"].users["x_a"]["isForbidden"] is False
    assert approval_store.get("x_a", "app-a")["status"] == "expired"
    assert approval_store.mark_pending("x_a", "app-a", {"name": "a"}) is True


def test_renew_refusals(env):
    active(env, uid="x_rej", evauth_rejected_at="2026-11-02T00:00:00+00:00")
    with pytest.raises(lc.AccountError, match="拒绝过"):
        run(env["fc"], lambda c: lc.renew(c, "x_rej", actor="admin"))
    env["fc"].users["ou_other"]["isForbidden"] = True
    active(env, uid="x_orphan", sponsor="ou_other")
    env["fc"].users["x_orphan"]["isForbidden"] = True
    with pytest.raises(lc.AccountError, match="负责人已停用"):
        run(env["fc"], lambda c: lc.renew(c, "x_orphan", actor="admin"))
    active(env, uid="x_c")
    with pytest.raises(lc.AccountError, match="最长"):
        run(env["fc"], lambda c: lc.renew(c, "x_c", actor="admin", expires=day(500)))


def test_renew_does_not_enable_unconfirmed_created_account(env):
    r = create(env)
    run(env["fc"], lambda c: lc.renew(c, r["user_id"], actor="admin"))
    assert env["fc"].users[r["user_id"]]["isForbidden"] is True


# ── 负责人离职 ──

from even_auth_gov import offboard


def test_offboarding_a_sponsor_disables_their_accounts(env):
    active(env, uid="x_a")
    active(env, uid="x_b")
    active(env, uid="x_c", sponsor="ou_other")
    ok = run(env["fc"], lambda c: offboard.apply("ou_boss", "李四", "offboard_event", c))
    users = env["fc"].users
    assert ok is True and users["ou_boss"]["isForbidden"]
    assert users["x_a"]["isForbidden"] and users["x_b"]["isForbidden"] and not users["x_c"]["isForbidden"]
    summary = [t for to, t in env["sent"]["texts"] if to == "ou_root"]
    assert len(summary) == 1 and "李四" in summary[0] and "受管x_a" in summary[0] and "受管x_b" in summary[0]
    assert {e["user_id"] for e in events(env) if e["event"] == "sponsor_left"} == {"x_a", "x_b"}


def test_cascade_failure_does_not_change_offboard_result(env):
    active(env)
    env["fc"].fail.add("/api/get-users")
    assert run(env["fc"], lambda c: offboard.apply("ou_boss", "李四", "offboard_event", c)) is True
    assert env["fc"].users["x_a"]["isForbidden"] is False   # 每小时扫描兜底


def test_offboarding_a_managed_account_does_not_cascade(env):
    active(env)
    run(env["fc"], lambda c: offboard.apply("x_a", "受管x_a", "account_expired", c))
    assert not [t for to, t in env["sent"]["texts"] if to == "ou_root"]


def test_active_sponsors(env):
    active(env, uid="x_a")
    active(env, uid="x_b", sponsor="ou_other")
    env["fc"].users["x_b"]["isForbidden"] = True                # 停用账号的负责人不算
    env["fc"].users["ou_gone"] = {**env["fc"].users["ou_other"], "id": "ou_gone", "isForbidden": True}
    active(env, uid="x_c", sponsor="ou_gone")                   # 已停用的负责人不算(扫描会处理)
    assert run(env["fc"], lc.active_sponsors) == ["ou_boss"]


# ── 换负责人、设密码、列表 ──

def transfer(env, ref="x_a", to="other@e.com"):
    return run(env["fc"], lambda c: lc.transfer(c, ref, to))


def test_transfer_needs_new_sponsor_confirmation(env):
    active(env)
    r = transfer(env)
    assert r["confirm_sent"] is True and env["fc"].props("x_a")["evauth_sponsor_pending"] == "ou_other"
    to, card = env["sent"]["cards"][-1]
    assert to == "ou_other" and "负责人改为你" in card["elements"][0]["content"]
    assert env["fc"].props("x_a")["evauth_sponsor"] == "ou_boss"           # 确认前旧负责人有效
    assert "只有该账号的负责人" in click(env, "acct_confirm", "ou_root", "x_a")
    assert "已确认负责" in click(env, "acct_confirm", "ou_other", "x_a")
    p = env["fc"].props("x_a")
    assert p["evauth_sponsor"] == "ou_other" and "evauth_sponsor_pending" not in p
    assert [e["event"] for e in events(env)][-2:] == ["transfer_requested", "transferred"]


def test_new_sponsor_can_decline_transfer(env):
    active(env)
    transfer(env)
    assert "负责人保持不变" in click(env, "acct_reject", "ou_other", "x_a")
    p = env["fc"].props("x_a")
    assert p["evauth_sponsor"] == "ou_boss" and "evauth_sponsor_pending" not in p


def test_transfer_then_renew_restores_an_orphaned_account(env):
    env["fc"].users["ou_boss"]["isForbidden"] = True
    active(env)
    sweep(env)                                                               # 负责人已停用 → 停
    assert env["fc"].users["x_a"]["isForbidden"]
    with pytest.raises(lc.AccountError, match="已停用"):
        transfer(env, to="boss@e.com")
    transfer(env)
    assert "需要管理员续期" in click(env, "acct_confirm", "ou_other", "x_a")
    run(env["fc"], lambda c: lc.renew(c, "x_a", actor="admin"))
    assert env["fc"].users["x_a"]["isForbidden"] is False


def test_transfer_refusals(env):
    active(env)
    with pytest.raises(lc.AccountError, match="一样"):
        transfer(env, to="boss@e.com")
    with pytest.raises(lc.AccountError, match="不是受管账号"):
        transfer(env, ref="ou_other")


def test_set_password_for_feishu_backup_login_and_service(env):
    r = run(env["fc"], lambda c: lc.set_password(c, "boss@e.com"))
    u = env["fc"].users["ou_boss"]
    assert r["must_change"] is True and u["password"] == r["password"] and u["needUpdatePassword"] is True
    active(env, uid="x_svc", evauth_kind="service")
    r = run(env["fc"], lambda c: lc.set_password(c, "x_svc"))
    assert r["must_change"] is False and env["fc"].users["x_svc"]["needUpdatePassword"] is False
    log = (env["dir"] / "account_events.jsonl").read_text(encoding="utf-8")
    assert "password_set" in log and r["password"] not in log


def test_set_password_refusals(env):
    env["fc"].add(id="uuid-s", email="s@x.com")
    with pytest.raises(lc.AccountError, match="register"):
        run(env["fc"], lambda c: lc.set_password(c, "uuid-s"))
    active(env)
    env["fc"].users["x_a"]["isForbidden"] = True
    with pytest.raises(lc.AccountError, match="已停用"):
        run(env["fc"], lambda c: lc.set_password(c, "x_a"))


def test_list_accounts(env):
    active(env, uid="x_a", expires=day(5))
    r = create(env)
    rows = run(env["fc"], lc.list_accounts)
    assert [row["id"] for row in rows] == ["x_a", r["user_id"]]                # 按到期日排序
    assert rows[0]["sponsor_name"] == "李四" and rows[0]["days_left"] == 5 and rows[0]["state"] == "启用/有效"
    assert rows[1]["state"] == "停用/待确认" and rows[1]["kind"] == "外部合作方"
