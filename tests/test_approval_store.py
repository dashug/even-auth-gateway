def _store(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "approvals.json"))
    from even_auth_gov import approval_store
    return approval_store

def test_pending_then_approve(monkeypatch, tmp_path):
    s = _store(monkeypatch, tmp_path)
    assert s.mark_pending("ou_x", "app-a", {"name": "张三", "email": "z@e.com"}) is True   # 新建=True(需发卡片)
    assert s.mark_pending("ou_x", "app-a", {"name": "张三"}) is False                      # 重复pending=False
    assert s.get("ou_x", "app-a")["status"] == "pending"
    s.mark_approved("ou_x", "app-a", "ou_boss")
    assert s.get("ou_x", "app-a")["status"] == "approved" and s.get("ou_x", "app-a")["approved_by"] == "ou_boss"

def test_deny_and_disable(monkeypatch, tmp_path):
    s = _store(monkeypatch, tmp_path)
    s.mark_pending("ou_y", "app-a", {"name": "李四"})
    s.mark_denied("ou_y", "app-a", "ou_boss")
    assert s.get("ou_y", "app-a")["status"] == "denied" and s.get("ou_y", "app-a")["denied_by"] == "ou_boss"
    s.mark_all_disabled("ou_y", "offboard_event")
    assert s.get("ou_y", "app-a")["status"] == "disabled" and s.get("ou_y", "app-a")["disabled_reason"] == "offboard_event"

def test_get_unknown_returns_none(monkeypatch, tmp_path):
    s = _store(monkeypatch, tmp_path)
    assert s.get("ou_ghost", "app-a") is None

def test_get_returns_copy(monkeypatch, tmp_path):
    s = _store(monkeypatch, tmp_path)
    s.mark_pending("ou_z", "app-a", {"name": "W"})
    rec = s.get("ou_z", "app-a"); rec["status"] = "hacked"
    assert s.get("ou_z", "app-a")["status"] == "pending"  # mutating the returned dict must not corrupt the store

def test_disabled_user_signup_replay_not_reset_to_pending(monkeypatch, tmp_path):
    # #15 安全:离职(disabled)用户的 signup 重放不得把他重置回 pending(否则可再被批准重新开通)
    s = _store(monkeypatch, tmp_path)
    s.mark_pending("ou_d", "app-a", {"name": "D"}); s.mark_approved("ou_d", "app-a", "b"); s.mark_all_disabled("ou_d", "exit")
    assert s.mark_pending("ou_d", "app-a", {"name": "D"}) is False   # 不重置、不发卡
    assert s.get("ou_d", "app-a")["status"] == "disabled"

def test_approved_user_signup_replay_keeps_approved(monkeypatch, tmp_path):
    # #15:已批准用户的 signup 重放不得丢掉 approved 状态
    s = _store(monkeypatch, tmp_path)
    s.mark_pending("ou_a", "app-a", {"name": "A"}); s.mark_approved("ou_a", "app-a", "b")
    assert s.mark_pending("ou_a", "app-a", {"name": "A"}) is False
    assert s.get("ou_a", "app-a")["status"] == "approved"

def test_denied_user_may_reapply(monkeypatch, tmp_path):
    # #15:被拒用户允许重新申请(建新 pending + 发卡)
    s = _store(monkeypatch, tmp_path)
    s.mark_pending("ou_r", "app-a", {"name": "R"}); s.mark_denied("ou_r", "app-a", "b")
    assert s.mark_pending("ou_r", "app-a", {"name": "R"}) is True
    assert s.get("ou_r", "app-a")["status"] == "pending"

def test_mark_notified_records_timestamp(monkeypatch, tmp_path):
    # #12:发卡成功留痕
    s = _store(monkeypatch, tmp_path)
    s.mark_pending("ou_n", "app-a", {"name": "N"})
    assert s.get("ou_n", "app-a")["notified_at"] == ""
    s.mark_notified("ou_n", "app-a")
    assert s.get("ou_n", "app-a")["notified_at"]   # 非空


# ── APP-AWARE (design-review #2/#16): per-(open_id, app) independence ──────

def test_pending_and_approved_independent_across_apps(monkeypatch, tmp_path):
    """同一用户对 app-a 待批、对 app-b 已批,互不影响 —— 这是本次改造的核心保证。"""
    s = _store(monkeypatch, tmp_path)
    s.mark_pending("ou_multi", "app-a", {"name": "多应用用户"})
    s.mark_pending("ou_multi", "app-b", {"name": "多应用用户"})
    s.mark_approved("ou_multi", "app-b", "ou_boss")
    assert s.get("ou_multi", "app-a")["status"] == "pending"
    assert s.get("ou_multi", "app-b")["status"] == "approved"

def test_mark_pending_guard_is_per_app(monkeypatch, tmp_path):
    """approved 状态的守卫只挡该 app 自己的记录;对另一个 app 首次申请仍要建 pending + 发卡。"""
    s = _store(monkeypatch, tmp_path)
    s.mark_pending("ou_multi", "app-a", {"name": "X"}); s.mark_approved("ou_multi", "app-a", "b")
    assert s.mark_pending("ou_multi", "app-a", {"name": "X"}) is False   # app-a 已批,不重置
    assert s.mark_pending("ou_multi", "app-b", {"name": "X"}) is True    # app-b 从未申请过,正常建 pending

def test_records_for_open_id_returns_all_apps(monkeypatch, tmp_path):
    s = _store(monkeypatch, tmp_path)
    s.mark_pending("ou_multi", "app-a", {"name": "X"}); s.mark_approved("ou_multi", "app-a", "b")
    s.mark_pending("ou_multi", "app-b", {"name": "X"}); s.mark_approved("ou_multi", "app-b", "b")
    s.mark_pending("ou_other", "app-a", {"name": "Y"})
    recs = s.records_for_open_id("ou_multi")
    apps = {r["app"] for r in recs}
    assert apps == {"app-a", "app-b"}
    assert all(r["open_id"] == "ou_multi" for r in recs)

def test_mark_all_disabled_sweeps_every_app_for_user(monkeypatch, tmp_path):
    s = _store(monkeypatch, tmp_path)
    s.mark_pending("ou_multi", "app-a", {"name": "X"}); s.mark_approved("ou_multi", "app-a", "b")
    s.mark_pending("ou_multi", "app-b", {"name": "X"}); s.mark_approved("ou_multi", "app-b", "b")
    s.mark_all_disabled("ou_multi", "offboard_event")
    assert s.get("ou_multi", "app-a")["status"] == "disabled"
    assert s.get("ou_multi", "app-b")["status"] == "disabled"

def test_mark_disable_failed_all_sweeps_every_app_for_user(monkeypatch, tmp_path):
    s = _store(monkeypatch, tmp_path)
    s.mark_pending("ou_multi", "app-a", {"name": "X"}); s.mark_approved("ou_multi", "app-a", "b")
    s.mark_pending("ou_multi", "app-b", {"name": "X"}); s.mark_approved("ou_multi", "app-b", "b")
    s.mark_disable_failed_all("ou_multi", "casdoor down")
    assert s.get("ou_multi", "app-a")["status"] == "disable_failed"
    assert s.get("ou_multi", "app-b")["status"] == "disable_failed"

def test_all_records_returns_raw_map_with_open_id_and_app(monkeypatch, tmp_path):
    s = _store(monkeypatch, tmp_path)
    s.mark_pending("ou_multi", "app-a", {"name": "X"})
    s.mark_pending("ou_multi", "app-b", {"name": "X"})
    recs = s.all_records()
    assert len(recs) == 2
    for rec in recs.values():
        assert rec["open_id"] == "ou_multi"
        assert rec["app"] in ("app-a", "app-b")
    # mutating the returned map must not corrupt the store (defensive copy)
    for rec in recs.values():
        rec["status"] = "hacked"
    assert s.get("ou_multi", "app-a")["status"] == "pending"


def test_mark_escalated_records_target_and_reapply_clears_it(monkeypatch, tmp_path):
    s = _store(monkeypatch, tmp_path)
    s.mark_pending("ou_x", "app-a", {"name": "张三"})
    s.mark_escalated("ou_x", "app-a", "ou_esc")
    rec = s.get("ou_x", "app-a")
    assert rec["escalated_to"] == "ou_esc" and rec["escalated_at"] and rec["status"] == "pending"
    s.mark_denied("ou_x", "app-a", "ou_esc")
    s.mark_pending("ou_x", "app-a", {"name": "张三"})          # 被拒后重新申请:新一轮,升级记录清零
    assert "escalated_to" not in s.get("ou_x", "app-a")


def test_first_notified_at_is_kept_across_reminders(monkeypatch, tmp_path):
    """催办会刷新 notified_at;超时升级要从审批人第一次收到卡片算起,所以另记一次性的 first_notified_at。"""
    s = _store(monkeypatch, tmp_path)
    s.mark_pending("ou_x", "app-a", {"name": "张三"})
    s.mark_notified("ou_x", "app-a")
    first = s.get("ou_x", "app-a")["first_notified_at"]
    s._set("ou_x", "app-a", notified_at="2000-01-01T00:00:00+00:00")
    s.mark_notified("ou_x", "app-a")
    rec = s.get("ou_x", "app-a")
    assert rec["first_notified_at"] == first and rec["notified_at"] != "2000-01-01T00:00:00+00:00"


def test_mark_denied_records_reason_only_when_given(monkeypatch, tmp_path):
    s = _store(monkeypatch, tmp_path)
    s.mark_pending("x_1", "app-a", {"name": "张三"})
    s.mark_denied("x_1", "app-a", "system", reason="ineligible:unregistered")
    rec = s.get("x_1", "app-a")
    assert rec["status"] == "denied" and rec["denied_by"] == "system" and rec["denied_reason"] == "ineligible:unregistered"
    s.mark_pending("ou_b", "app-a", {"name": "b"})
    s.mark_denied("ou_b", "app-a", "ou_boss")
    assert "denied_reason" not in s.get("ou_b", "app-a")


def test_reopen_after_renewal_only_flips_lifecycle_disables(monkeypatch, tmp_path):
    s = _store(monkeypatch, tmp_path)
    for app in ("app-a", "app-b", "app-c"):
        s.mark_pending("x_1", app, {"name": "x"})
        s.mark_approved("x_1", app, "ou_boss")
    s.mark_all_disabled("x_1", "account_expired")
    s._set("x_1", "app-c", disabled_reason="offboard_event")   # 别的原因停用的不动
    assert s.reopen_after_renewal("x_1") == 2
    assert s.get("x_1", "app-a")["status"] == "expired"
    assert s.get("x_1", "app-c")["status"] == "disabled"
    assert s.mark_pending("x_1", "app-a", {"name": "x"}) is True
    assert s.mark_pending("x_1", "app-c", {"name": "x"}) is False
