def _store(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "approvals.json"))
    from even_auth_gov import approval_store
    return approval_store

def test_pending_then_approve(monkeypatch, tmp_path):
    s = _store(monkeypatch, tmp_path)
    assert s.mark_pending("ou_x", {"name": "张三", "email": "z@e.com"}) is True   # 新建=True(需发卡片)
    assert s.mark_pending("ou_x", {"name": "张三"}) is False                      # 重复pending=False
    assert s.get("ou_x")["status"] == "pending"
    s.mark_approved("ou_x", "ou_boss")
    assert s.get("ou_x")["status"] == "approved" and s.get("ou_x")["approved_by"] == "ou_boss"

def test_deny_and_disable(monkeypatch, tmp_path):
    s = _store(monkeypatch, tmp_path)
    s.mark_pending("ou_y", {"name": "李四"})
    s.mark_denied("ou_y", "ou_boss")
    assert s.get("ou_y")["status"] == "denied" and s.get("ou_y")["denied_by"] == "ou_boss"
    s.mark_disabled("ou_y", "offboard_event")
    assert s.get("ou_y")["status"] == "disabled" and s.get("ou_y")["disabled_reason"] == "offboard_event"

def test_get_unknown_returns_none(monkeypatch, tmp_path):
    s = _store(monkeypatch, tmp_path)
    assert s.get("ou_ghost") is None

def test_get_returns_copy(monkeypatch, tmp_path):
    s = _store(monkeypatch, tmp_path)
    s.mark_pending("ou_z", {"name": "W"})
    rec = s.get("ou_z"); rec["status"] = "hacked"
    assert s.get("ou_z")["status"] == "pending"  # mutating the returned dict must not corrupt the store
