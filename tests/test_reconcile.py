import asyncio
from types import SimpleNamespace
from even_auth_gov import reconcile, approval_store

def _status(**kw):
    b = dict(is_frozen=False, is_resigned=False, is_exited=False); b.update(kw); return SimpleNamespace(**b)

def test_reconcile_disables_frozen_keeps_ok_and_failopen(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    for oid in ("ou_ok", "ou_frozen", "ou_err"):
        approval_store.mark_pending(oid, "app-a", {"name": oid}); approval_store.mark_approved(oid, "app-a", "ou_boss")
    async def fake_status(open_id):
        if open_id == "ou_frozen": return _status(is_frozen=True)
        if open_id == "ou_err": raise RuntimeError("api down")
        return _status()
    disabled = []
    async def fake_apply(open_id, name, reason, client): disabled.append((open_id, reason))
    monkeypatch.setattr(reconcile, "fetch_feishu_status", fake_status)
    monkeypatch.setattr(reconcile.offboard, "apply", fake_apply)
    asyncio.run(reconcile.run(client=None))
    assert ("ou_frozen", "reconcile") in disabled
    assert all(o != "ou_ok" for o, _ in disabled)    # healthy 不动
    assert all(o != "ou_err" for o, _ in disabled)    # API 错误 fail-open

def test_reconcile_only_touches_approved(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    approval_store.mark_pending("ou_pending", "app-a", {"name": "p"})   # still pending, not approved
    approval_store.mark_pending("ou_ok", "app-a", {"name": "o"}); approval_store.mark_approved("ou_ok", "app-a", "ou_boss")
    checked = []
    async def fake_status(open_id): checked.append(open_id); return _status()
    monkeypatch.setattr(reconcile, "fetch_feishu_status", fake_status)
    asyncio.run(reconcile.run(client=None))
    assert checked == ["ou_ok"]   # pending user not reconciled

def test_reconcile_empty_store_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    asyncio.run(reconcile.run(client=None))   # must not raise on empty/missing store

def test_reconcile_disables_hard_deleted_but_failopen_on_transient(monkeypatch, tmp_path):
    # #5: 飞书查无此人(硬删除)→ 禁用;瞬时错误 → fail-open 不误禁。
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    approval_store.mark_pending("ou_gone", "app-a", {"name": "g"}); approval_store.mark_approved("ou_gone", "app-a", "b")
    approval_store.mark_pending("ou_down", "app-a", {"name": "d"}); approval_store.mark_approved("ou_down", "app-a", "b")
    async def fake_status(open_id):
        if open_id == "ou_gone": raise reconcile.FeishuUserNotFound(open_id)
        raise RuntimeError("api timeout")   # 瞬时错 → fail-open
    disabled = []
    async def fake_apply(open_id, name, reason, client): disabled.append((open_id, reason))
    monkeypatch.setattr(reconcile, "fetch_feishu_status", fake_status)
    monkeypatch.setattr(reconcile.offboard, "apply", fake_apply)
    asyncio.run(reconcile.run(client=None))
    assert ("ou_gone", "reconcile-deleted") in disabled   # 硬删除 → 禁用
    assert all(o != "ou_down" for o, _ in disabled)        # 瞬时错 → fail-open,不动

def test_reconcile_resends_only_unnotified_pending(monkeypatch, tmp_path):
    # #12:待审批卡片发失败(notified_at 空)→ 对账补发;刚通知过的不重发。
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    from even_auth_gov import webhook, approval_store
    approval_store.mark_pending("ou_stuck", "app-a", {"name": "S"})                             # notified_at=""
    approval_store.mark_pending("ou_ok", "app-a", {"name": "O"}); approval_store.mark_notified("ou_ok", "app-a")
    sent = []
    async def fake_send(info): sent.append(info["open_id"])
    monkeypatch.setattr(webhook, "send_approval_card", fake_send)
    asyncio.run(reconcile.run(client=None))
    assert sent == ["ou_stuck"]                                    # 只补发没送达的
    assert approval_store.get("ou_stuck", "app-a")["notified_at"]           # 补发后留痕

def test_reconcile_includes_casdoor_group_members_not_in_local_store(monkeypatch, tmp_path):
    # #8:直接加进 Casdoor 组、不在本地审批库的用户(迁移/手工授权)也要被对账。
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    approval_store.mark_pending("ou_local", "app-a", {"name": "L"}); approval_store.mark_approved("ou_local", "app-a", "b")
    async def fake_members(client): return ["ou_manual", "ou_local"]   # 手工加的 + 与本地重复的
    monkeypatch.setattr("even_auth_gov.casdoor_admin.list_approved_feishu_members", fake_members)
    checked, disabled = [], []
    async def fake_status(open_id): checked.append(open_id); return _status(is_resigned=True)
    async def fake_apply(open_id, name, reason, client): disabled.append(open_id)
    monkeypatch.setattr(reconcile, "fetch_feishu_status", fake_status)
    monkeypatch.setattr(reconcile.offboard, "apply", fake_apply)
    asyncio.run(reconcile.run(client=None))
    assert set(checked) == {"ou_local", "ou_manual"}    # 本地 ∪ Casdoor 组,去重
    assert set(disabled) == {"ou_local", "ou_manual"}   # 都离职 → 都禁用(含只在组里的手工用户)

def test_reconcile_retries_disable_failed(monkeypatch, tmp_path):
    # #4 兜底:上次禁用失败的记录,每日对账优先重试(reason=reconcile-retry)。
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    approval_store.mark_pending("ou_df", "app-a", {"name": "df"}); approval_store.mark_disable_failed_all("ou_df", "prev fail")
    approval_store.mark_pending("ou_ok", "app-a", {"name": "o"}); approval_store.mark_approved("ou_ok", "app-a", "b")
    applied = []
    async def fake_apply(open_id, name, reason, client): applied.append((open_id, reason))
    async def fake_status(open_id): return _status()
    monkeypatch.setattr(reconcile.offboard, "apply", fake_apply)
    monkeypatch.setattr(reconcile, "fetch_feishu_status", fake_status)
    asyncio.run(reconcile.run(client=None))
    assert ("ou_df", "reconcile-retry") in applied   # disable_failed 被优先重试


# ── APP-AWARE (design-review #2/#16): dedupe by user, per-app card resend ──

def test_reconcile_dedupes_approved_user_across_apps_offboards_once(monkeypatch, tmp_path):
    """用户对 app-a、app-b 都是 approved(同一真人)且已离职 —— 只应查一次飞书状态、
    只应调一次 offboard.apply,而不是每个 app 一次。"""
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    approval_store.mark_pending("ou_multi", "app-a", {"name": "多应用用户"}); approval_store.mark_approved("ou_multi", "app-a", "b")
    approval_store.mark_pending("ou_multi", "app-b", {"name": "多应用用户"}); approval_store.mark_approved("ou_multi", "app-b", "b")
    checked = []
    async def fake_status(open_id):
        checked.append(open_id)
        return _status(is_resigned=True)
    applied = []
    async def fake_apply(open_id, name, reason, client): applied.append((open_id, reason))
    monkeypatch.setattr(reconcile, "fetch_feishu_status", fake_status)
    monkeypatch.setattr(reconcile.offboard, "apply", fake_apply)
    asyncio.run(reconcile.run(client=None))
    assert checked == ["ou_multi"]     # 飞书状态只查了一次
    assert applied == [("ou_multi", "reconcile")]   # offboard 只调了一次

def test_reconcile_dedupes_disable_failed_retry_across_apps(monkeypatch, tmp_path):
    """同一用户在两个 app 的记录上都是 disable_failed —— 重试只应调一次 offboard.apply。"""
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    approval_store.mark_pending("ou_multi", "app-a", {"name": "x"}); approval_store.mark_disable_failed_all("ou_multi", "prev fail")
    applied = []
    async def fake_apply(open_id, name, reason, client): applied.append((open_id, reason))
    monkeypatch.setattr(reconcile.offboard, "apply", fake_apply)
    asyncio.run(reconcile.run(client=None))
    assert applied == [("ou_multi", "reconcile-retry")]   # 去重:只重试一次

def test_reconcile_resends_pending_card_per_app_to_its_own_approver(monkeypatch, tmp_path):
    """同一用户对 app-a 待批未通知、对 app-b 待批未通知 —— 两条都要各自补发,card 携带对应 app。"""
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    from even_auth_gov import webhook
    approval_store.mark_pending("ou_multi", "app-a", {"name": "多应用用户"})
    approval_store.mark_pending("ou_multi", "app-b", {"name": "多应用用户"})
    sent = []
    async def fake_send(info): sent.append((info["open_id"], info["app"]))
    monkeypatch.setattr(webhook, "send_approval_card", fake_send)
    asyncio.run(reconcile.run(client=None))
    assert set(sent) == {("ou_multi", "app-a"), ("ou_multi", "app-b")}
    assert approval_store.get("ou_multi", "app-a")["notified_at"]
    assert approval_store.get("ou_multi", "app-b")["notified_at"]


# ── 超时升级(里程碑 3):待批超过 APPROVAL_ESCALATE_HOURS(默认 24)→ 卡片升级给 APPROVAL_ESCALATE_TO
#    (默认全局兜底审批人),只升级一次;发送失败不记,下次再试 ──
from datetime import datetime, timedelta, timezone

def _aged_pending(open_id, app, hours, notified=True):
    """申请于 hours 小时前提交;notified=True 表示审批人当时就收到了卡片。"""
    approval_store.mark_pending(open_id, app, {"name": open_id, "email": f"{open_id}@e.com"})
    then = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    approval_store._set(open_id, app, applied_at=then)
    if notified:
        approval_store._set(open_id, app, notified_at=then, first_notified_at=then)

def _escalation_env(monkeypatch, tmp_path, fail=False):
    from even_auth_gov import webhook
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    monkeypatch.setenv("APPROVER_FEISHU_ID", "ou_root")
    monkeypatch.setenv("APPROVER_APP_A", "ou_boss")
    monkeypatch.delenv("APPROVAL_ESCALATE_HOURS", raising=False)
    monkeypatch.delenv("APPROVAL_ESCALATE_TO", raising=False)
    sent = []
    async def fake_send(info):
        if fail:
            raise RuntimeError("feishu down")
        sent.append(info)
    monkeypatch.setattr(webhook, "send_approval_card", fake_send)
    return sent

def test_sweep_escalates_stale_pending_once_to_fallback_approver(monkeypatch, tmp_path):
    sent = _escalation_env(monkeypatch, tmp_path)
    _aged_pending("ou_old", "app-a", hours=30)
    asyncio.run(reconcile.sweep_pending())
    esc = [i for i in sent if i.get("to")]
    assert len(esc) == 1 and esc[0]["to"] == "ou_root" and esc[0]["open_id"] == "ou_old" and esc[0]["app"] == "app-a"
    assert "30 小时" in esc[0]["note"]
    rec = approval_store.get("ou_old", "app-a")
    assert rec["escalated_to"] == "ou_root" and rec["escalated_at"]
    sent.clear()
    asyncio.run(reconcile.sweep_pending())
    assert not [i for i in sent if i.get("to")]                     # 只升级一次

def test_sweep_does_not_escalate_fresh_unnotified_or_decided(monkeypatch, tmp_path):
    sent = _escalation_env(monkeypatch, tmp_path)
    _aged_pending("ou_fresh", "app-a", hours=2)
    _aged_pending("ou_silent", "app-a", hours=30, notified=False)   # 卡片都没送到审批人:先补发,不升级
    _aged_pending("ou_done", "app-a", hours=30)
    approval_store.mark_approved("ou_done", "app-a", "ou_boss")
    asyncio.run(reconcile.sweep_pending())
    assert not [i for i in sent if i.get("to")]
    assert [i["open_id"] for i in sent] == ["ou_silent"]            # 补发照常,发给该应用审批人

def test_sweep_skips_escalation_when_target_is_already_the_approver_or_disabled(monkeypatch, tmp_path):
    sent = _escalation_env(monkeypatch, tmp_path)
    monkeypatch.setenv("APPROVER_APP_A", "ou_root")                  # 审批人就是兜底人,没有更上一级
    _aged_pending("ou_old", "app-a", hours=30)
    asyncio.run(reconcile.sweep_pending())
    assert not [i for i in sent if i.get("to")]
    monkeypatch.setenv("APPROVER_APP_A", "ou_boss")
    monkeypatch.setenv("APPROVAL_ESCALATE_HOURS", "0")               # 0 = 关闭升级
    asyncio.run(reconcile.sweep_pending())
    assert not [i for i in sent if i.get("to")]

def test_sweep_escalation_honours_custom_target_and_hours(monkeypatch, tmp_path):
    sent = _escalation_env(monkeypatch, tmp_path)
    monkeypatch.setenv("APPROVAL_ESCALATE_TO", "ou_cto")
    monkeypatch.setenv("APPROVAL_ESCALATE_HOURS", "4")
    _aged_pending("ou_old", "app-a", hours=5)
    asyncio.run(reconcile.sweep_pending())
    assert [i["to"] for i in sent if i.get("to")] == ["ou_cto"]

def test_sweep_escalation_send_failure_is_retried_next_time(monkeypatch, tmp_path):
    _escalation_env(monkeypatch, tmp_path, fail=True)
    _aged_pending("ou_old", "app-a", hours=30)
    asyncio.run(reconcile.sweep_pending())
    assert "escalated_to" not in approval_store.get("ou_old", "app-a")


def test_sweep_escalation_clock_starts_when_approver_first_got_the_card(monkeypatch, tmp_path):
    """申请 30 小时前提交,但卡片 2 小时前才第一次送达(之前推送一直失败)→ 还不该升级。"""
    sent = _escalation_env(monkeypatch, tmp_path)
    _aged_pending("ou_late", "app-a", hours=30, notified=False)
    two_h_ago = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    approval_store._set("ou_late", "app-a", notified_at=two_h_ago, first_notified_at=two_h_ago)
    asyncio.run(reconcile.sweep_pending())
    assert not [i for i in sent if i.get("to")]


def test_reconcile_runs_account_sweep_after_pending_sweep(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    from even_auth_gov import account_lifecycle
    order = []
    async def fake_pending(): order.append("pending")
    async def fake_accounts(client): order.append("accounts")
    monkeypatch.setattr(reconcile, "sweep_pending", fake_pending)
    monkeypatch.setattr(account_lifecycle, "sweep", fake_accounts)
    asyncio.run(reconcile.run(client=None))
    assert order[:2] == ["pending", "accounts"]
