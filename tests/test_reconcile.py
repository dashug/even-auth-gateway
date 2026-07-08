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
