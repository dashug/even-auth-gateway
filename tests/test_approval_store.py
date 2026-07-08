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

def test_disabled_user_signup_replay_not_reset_to_pending(monkeypatch, tmp_path):
    # #15 安全:离职(disabled)用户的 signup 重放不得把他重置回 pending(否则可再被批准重新开通)
    s = _store(monkeypatch, tmp_path)
    s.mark_pending("ou_d", {"name": "D"}); s.mark_approved("ou_d", "b"); s.mark_disabled("ou_d", "exit")
    assert s.mark_pending("ou_d", {"name": "D"}) is False   # 不重置、不发卡
    assert s.get("ou_d")["status"] == "disabled"

def test_approved_user_signup_replay_keeps_approved(monkeypatch, tmp_path):
    # #15:已批准用户的 signup 重放不得丢掉 approved 状态
    s = _store(monkeypatch, tmp_path)
    s.mark_pending("ou_a", {"name": "A"}); s.mark_approved("ou_a", "b")
    assert s.mark_pending("ou_a", {"name": "A"}) is False
    assert s.get("ou_a")["status"] == "approved"

def test_denied_user_may_reapply(monkeypatch, tmp_path):
    # #15:被拒用户允许重新申请(建新 pending + 发卡)
    s = _store(monkeypatch, tmp_path)
    s.mark_pending("ou_r", {"name": "R"}); s.mark_denied("ou_r", "b")
    assert s.mark_pending("ou_r", {"name": "R"}) is True
    assert s.get("ou_r")["status"] == "pending"

def test_mark_notified_records_timestamp(monkeypatch, tmp_path):
    # #12:发卡成功留痕
    s = _store(monkeypatch, tmp_path)
    s.mark_pending("ou_n", {"name": "N"})
    assert s.get("ou_n")["notified_at"] == ""
    s.mark_notified("ou_n")
    assert s.get("ou_n")["notified_at"]   # 非空
