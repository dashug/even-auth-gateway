import asyncio
from types import SimpleNamespace
from even_auth_gov import reconcile, approval_store

def _status(**kw):
    b = dict(is_frozen=False, is_resigned=False, is_exited=False); b.update(kw); return SimpleNamespace(**b)

def test_reconcile_disables_frozen_keeps_ok_and_failopen(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    for oid in ("ou_ok", "ou_frozen", "ou_err"):
        approval_store.mark_pending(oid, {"name": oid}); approval_store.mark_approved(oid, "ou_boss")
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
    approval_store.mark_pending("ou_pending", {"name": "p"})   # still pending, not approved
    approval_store.mark_pending("ou_ok", {"name": "o"}); approval_store.mark_approved("ou_ok", "ou_boss")
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
    approval_store.mark_pending("ou_gone", {"name": "g"}); approval_store.mark_approved("ou_gone", "b")
    approval_store.mark_pending("ou_down", {"name": "d"}); approval_store.mark_approved("ou_down", "b")
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

def test_reconcile_retries_disable_failed(monkeypatch, tmp_path):
    # #4 兜底:上次禁用失败的记录,每日对账优先重试(reason=reconcile-retry)。
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    approval_store.mark_pending("ou_df", {"name": "df"}); approval_store.mark_disable_failed("ou_df", "prev fail")
    approval_store.mark_pending("ou_ok", {"name": "o"}); approval_store.mark_approved("ou_ok", "b")
    applied = []
    async def fake_apply(open_id, name, reason, client): applied.append((open_id, reason))
    async def fake_status(open_id): return _status()
    monkeypatch.setattr(reconcile.offboard, "apply", fake_apply)
    monkeypatch.setattr(reconcile, "fetch_feishu_status", fake_status)
    asyncio.run(reconcile.run(client=None))
    assert ("ou_df", "reconcile-retry") in applied   # disable_failed 被优先重试
