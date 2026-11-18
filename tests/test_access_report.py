"""权限视图 + 离职撤权核对(F-5.8.3)。以 Casdoor 实际状态为准,合上审批库留痕。"""
import asyncio, csv, io, json
from types import SimpleNamespace

import httpx
import pytest

from even_auth_gov import access_report as ar, approval_store

ORG = "even-test"


def _user(oid, name, groups=(), forbidden=False, display=None, email=""):
    return {"owner": ORG, "name": name, "id": oid, "displayName": display or name, "email": email,
            "isForbidden": forbidden, "groups": [f"{ORG}/{g}" for g in groups]}


def _role(name, display, members=(), enabled=True):
    return {"owner": ORG, "name": name, "displayName": display, "isEnabled": enabled,
            "users": [f"{ORG}/{m}" for m in members]}


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("CASDOOR_ORG", ORG)
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    monkeypatch.setenv("APP_CLIENT_IDS", "app-a:cid-a,cs:cid-cs,cs-hub:cid-hub")
    monkeypatch.setenv("DEFAULT_APP", "cs-hub")
    return tmp_path


def _approved(oid, app, by="ou_boss", role=""):
    approval_store.mark_pending(oid, app, {"name": oid})
    approval_store.mark_approved(oid, app, by, role=role)


def _snap(users, roles):
    return {"users": users, "roles": roles, "records": approval_store.all_records()}


def _kinds(anomalies):
    return sorted((a["kind"], a["open_id"], a["app"]) for a in anomalies)


# ── 视图:人 × 应用 ──

def test_rows_join_casdoor_groups_roles_and_approval_trail(env):
    _approved("ou_alice", "app-a", role="app-a-editor")
    users = [_user("ou_alice", "u1", groups=["approved-app-a"], display="Alice", email="a@e.com")]
    roles = [_role("app-a-editor", "编辑者", members=["u1"]), _role("app-a-viewer", "查看者")]
    rows, _ = ar.build(_snap(users, roles))
    assert rows == [{
        "name": "Alice", "open_id": "ou_alice", "email": "a@e.com", "app": "app-a", "account": "enabled",
        "kind": "飞书员工", "sponsor": "", "expires": "", "has_access": "yes", "roles": "编辑者", "approval_status": "approved", "approved_by": "ou_boss",
        "approved_at": rows[0]["approved_at"], "source": "approval",
    }]
    assert rows[0]["approved_at"]


def test_role_is_attributed_to_the_longest_matching_app(env):
    users = [_user("ou_x", "u1", groups=["approved-cs", "approved-cs-hub"])]
    roles = [_role("cs-admin", "管理员", members=["u1"]), _role("cs-hub-operator", "运营", members=["u1"])]
    rows, _ = ar.build(_snap(users, roles))
    by_app = {r["app"]: r["roles"] for r in rows}
    assert by_app == {"cs": "管理员", "cs-hub": "运营"}


def test_access_granted_outside_approval_flow_is_marked_as_such(env):
    users = [_user("ou_mig", "u1", groups=["approved-cs-hub"])]
    rows, anomalies = ar.build(_snap(users, []))
    assert rows[0]["source"] == "casdoor-only" and rows[0]["approval_status"] == ""
    assert ("access_without_approval", "ou_mig", "cs-hub") in _kinds(anomalies)


# ── 核对:需要处理的异常 ──

def test_offboarded_locally_but_casdoor_account_enabled_is_high(env):
    """离职流程记过 disabled,但 Casdoor 账号又是启用的(被人手工重新启用)—— 最危险。"""
    _approved("ou_back", "app-a")
    approval_store.mark_all_disabled("ou_back", "offboard_event")
    users = [_user("ou_back", "u1")]
    _, anomalies = ar.build(_snap(users, []))
    hit = [a for a in anomalies if a["kind"] == "offboarded_but_enabled"]
    assert len(hit) == 1 and hit[0]["severity"] == "high" and hit[0]["open_id"] == "ou_back"


def test_disable_failed_and_still_enabled_is_high_but_resolved_if_casdoor_disabled(env):
    _approved("ou_f", "app-a")
    approval_store.mark_disable_failed_all("ou_f", "casdoor down")
    _, anomalies = ar.build(_snap([_user("ou_f", "u1")], []))
    assert [a["severity"] for a in anomalies if a["kind"] == "offboarded_but_enabled"] == ["high"]
    _, anomalies = ar.build(_snap([_user("ou_f", "u1", forbidden=True)], []))
    assert not [a for a in anomalies if a["kind"] == "offboarded_but_enabled"]


def test_disabled_account_still_holding_groups_or_roles_is_medium(env):
    users = [_user("ou_d", "u1", groups=["approved-app-a"], forbidden=True)]
    roles = [_role("app-a-viewer", "查看者", members=["u1"])]
    rows, anomalies = ar.build(_snap(users, roles))
    assert rows[0]["account"] == "disabled"
    hit = [a for a in anomalies if a["kind"] == "disabled_with_access"]
    assert len(hit) == 1 and hit[0]["severity"] == "medium" and hit[0]["app"] == "app-a"


def test_role_without_access_and_access_without_role(env):
    """角色强制:分了角色的应用,有准入就得有角色;反过来有角色没准入是残余。"""
    _approved("ou_norole", "app-a")
    users = [_user("ou_norole", "u1", groups=["approved-app-a"]),
             _user("ou_residue", "u2")]
    roles = [_role("app-a-viewer", "查看者", members=["u2"])]
    _, anomalies = ar.build(_snap(users, roles))
    kinds = _kinds(anomalies)
    assert ("access_without_role", "ou_norole", "app-a") in kinds
    assert ("role_without_access", "ou_residue", "app-a") in kinds
    sev = {a["kind"]: a["severity"] for a in anomalies}
    assert sev["access_without_role"] == "medium" and sev["role_without_access"] == "medium"


def test_access_without_role_only_for_apps_that_use_roles(env):
    _approved("ou_plain", "cs-hub")
    users = [_user("ou_plain", "u1", groups=["approved-cs-hub"])]
    roles = [_role("cs-hub-old", "停用的角色", enabled=False)]      # 只有停用角色 = 没分角色
    _, anomalies = ar.build(_snap(users, roles))
    assert anomalies == []


def test_approved_record_without_casdoor_group_and_missing_user_are_info(env):
    _approved("ou_drift", "app-a")
    _approved("ou_gone", "app-a")
    _, anomalies = ar.build(_snap([_user("ou_drift", "u1")], []))
    kinds = {(a["kind"], a["open_id"]): a["severity"] for a in anomalies}
    assert kinds[("approved_but_no_access", "ou_drift")] == "info"
    assert kinds[("record_user_missing", "ou_gone")] == "info"


def test_healthy_state_has_no_anomalies(env):
    _approved("ou_ok", "app-a", role="app-a-viewer")
    users = [_user("ou_ok", "u1", groups=["approved-app-a"])]
    roles = [_role("app-a-viewer", "查看者", members=["u1"])]
    assert ar.build(_snap(users, roles))[1] == []


def test_anomalies_sorted_most_severe_first(env):
    _approved("ou_back", "app-a")
    approval_store.mark_all_disabled("ou_back", "offboard_event")
    users = [_user("ou_back", "u1"), _user("ou_mig", "u2", groups=["approved-app-a"]),
             _user("ou_d", "u3", groups=["approved-app-a"], forbidden=True)]
    _, anomalies = ar.build(_snap(users, []))
    assert [a["severity"] for a in anomalies] == ["high", "medium", "info"]


# ── 可选:逐个查飞书在职状态(--check-feishu)──

def test_check_feishu_flags_departed_and_unknown_but_enabled_accounts(env, monkeypatch):
    from even_auth_gov import reconcile
    async def fake_status(open_id):
        if open_id == "ou_left":
            return SimpleNamespace(is_frozen=False, is_resigned=True, is_exited=False)
        if open_id == "ou_deleted":
            raise reconcile.FeishuUserNotFound(open_id)
        if open_id == "ou_noscope":
            raise RuntimeError("feishu get-user 41050 no user authority")
        return SimpleNamespace(is_frozen=False, is_resigned=False, is_exited=False)
    monkeypatch.setattr(reconcile, "fetch_feishu_status", fake_status)
    users = [_user("ou_left", "u1"), _user("ou_deleted", "u2"), _user("ou_noscope", "u3"), _user("ou_ok", "u4"),
             _user("ou_off", "u5", forbidden=True), {**_user("pwd-uuid", "u6")}]
    found = asyncio.run(ar.check_feishu(users))
    sev = {(a["kind"], a["open_id"]): a["severity"] for a in found}
    assert sev == {("departed_but_enabled", "ou_left"): "high",
                   ("departed_but_enabled", "ou_deleted"): "high",
                   ("feishu_check_failed", "ou_noscope"): "medium"}   # 已停用和非飞书账号不查


# ── 数据来源:Casdoor get-users / get-roles ──

def test_snapshot_reads_casdoor_users_and_roles(env):
    def handler(req):
        if req.url.path == "/api/get-users":
            assert req.url.params["owner"] == ORG
            return httpx.Response(200, json={"status": "ok", "data": [_user("ou_a", "u1")]})
        if req.url.path == "/api/get-roles":
            return httpx.Response(200, json={"status": "ok", "data": [_role("app-a-viewer", "查看者")]})
        return httpx.Response(404)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://casdoor")
    snap = asyncio.run(ar.snapshot(client))
    assert [u["id"] for u in snap["users"]] == ["ou_a"] and [r["name"] for r in snap["roles"]] == ["app-a-viewer"]


def test_snapshot_fails_loudly_when_casdoor_unreadable(env):
    """核对报表读不到 Casdoor 就报错,不能输出一张"看起来没问题"的空表。"""
    def handler(req):
        return httpx.Response(200, json={"status": "error", "msg": "boom", "data": None})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://casdoor")
    with pytest.raises(RuntimeError):
        asyncio.run(ar.snapshot(client))


# ── CLI ──

def _fake_snapshot(monkeypatch, users, roles):
    async def fake(client):
        return _snap(users, roles)
    monkeypatch.setattr(ar, "snapshot", fake)


def test_main_writes_access_matrix_csv_filtered_by_app(env, monkeypatch, tmp_path):
    users = [_user("ou_a", "u1", groups=["approved-app-a", "approved-cs-hub"])]
    _fake_snapshot(monkeypatch, users, [])
    out = tmp_path / "access.csv"
    assert ar.main(["-o", str(out), "--app", "app-a"]) == 0
    rows = list(csv.DictReader(io.StringIO(out.read_text(encoding="utf-8-sig"))))
    assert [(r["open_id"], r["app"]) for r in rows] == [("ou_a", "app-a")]
    assert list(rows[0]) == ar.COLUMNS


def test_main_anomalies_mode_writes_anomaly_csv(env, monkeypatch, tmp_path):
    users = [_user("ou_mig", "u1", groups=["approved-app-a"])]
    _fake_snapshot(monkeypatch, users, [])
    out = tmp_path / "anomalies.csv"
    assert ar.main(["-o", str(out), "--anomalies"]) == 0
    rows = list(csv.DictReader(io.StringIO(out.read_text(encoding="utf-8-sig"))))
    assert list(rows[0]) == ar.ANOMALY_COLUMNS
    assert [(r["kind"], r["open_id"]) for r in rows] == [("access_without_approval", "ou_mig")]


# ── 每日推送:对账后把需处理(高/中)的异常推给全局审批人 ──

def _capture_texts(monkeypatch):
    sent = []
    async def fake_send_text(receive_id, text, receive_id_type="open_id"):
        sent.append((receive_id, text)); return True
    monkeypatch.setattr("even_auth_gov.feishu.send_text", fake_send_text)
    return sent


def test_alert_pushes_actionable_anomalies_summary(env, monkeypatch):
    monkeypatch.setenv("APPROVER_FEISHU_ID", "ou_root")
    _approved("ou_back", "app-a")
    approval_store.mark_all_disabled("ou_back", "offboard_event")
    users = [_user("ou_back", "u1", display="回来的人"), _user("ou_mig", "u2", groups=["approved-app-a"])]
    _fake_snapshot(monkeypatch, users, [])
    sent = _capture_texts(monkeypatch)
    assert asyncio.run(ar.alert(client=None)) == 1
    assert len(sent) == 1 and sent[0][0] == "ou_root"
    assert "回来的人" in sent[0][1] and "access_report --anomalies" in sent[0][1]
    assert "ou_mig" not in sent[0][1]                          # info 级不推


def test_alert_silent_when_nothing_actionable(env, monkeypatch):
    monkeypatch.setenv("APPROVER_FEISHU_ID", "ou_root")
    _fake_snapshot(monkeypatch, [_user("ou_mig", "u1", groups=["approved-app-a"])], [])
    sent = _capture_texts(monkeypatch)
    assert asyncio.run(ar.alert(client=None)) == 0
    assert sent == []


def test_alert_caps_listed_items(env, monkeypatch):
    monkeypatch.setenv("APPROVER_FEISHU_ID", "ou_root")
    users = [_user(f"ou_d{i}", f"u{i}", groups=["approved-app-a"], forbidden=True) for i in range(15)]
    _fake_snapshot(monkeypatch, users, [])
    sent = _capture_texts(monkeypatch)
    assert asyncio.run(ar.alert(client=None)) == 15
    text = sent[0][1]
    assert "15" in text and text.count("ou_d") == ar.ALERT_MAX_ITEMS


# ── 里程碑 4a:非飞书账号 ──

from datetime import date

D = date(2026, 11, 18)


def _managed(oid, groups=(), forbidden=False, **props):
    p = {"evauth_kind": "partner", "evauth_sponsor": "ou_boss", "evauth_expires": "2026-12-31",
         "evauth_confirmed_at": "2026-11-01T00:00:00+00:00"}
    p.update(props)
    return {**_user(oid, oid, groups=groups, forbidden=forbidden),
            "properties": {k: v for k, v in p.items() if v is not None}}


def _account_kinds(users, roles=()):
    _, anomalies = ar.build(_snap(users, list(roles)), today=D)
    return {(a["kind"], a["open_id"]): a["severity"] for a in anomalies
            if a["kind"] not in ("access_without_approval",)}


def test_managed_and_unregistered_account_anomalies(env):
    boss = _user("ou_boss", "boss")
    users = [
        boss,
        _user("uuid-stranger", "s1", groups=["approved-app-a"]),            # 未登记 + 有准入
        _user("uuid-idle", "s2"),                                            # 未登记,没准入
        _user("uuid-off", "s3", forbidden=True),                             # 未登记但已停用:不报
        _managed("x_expired", evauth_expires="2026-11-17"),
        _managed("x_rejected", evauth_rejected_at="2026-11-02T00:00:00+00:00"),
        _managed("x_bad", evauth_kind="vendor"),
        _managed("x_bad_off", forbidden=True, evauth_kind="vendor"),
        _managed("x_orphan", evauth_sponsor="ou_ghost"),
        _managed("x_soon", evauth_expires="2026-11-25"),
        _managed("x_wait", forbidden=True, evauth_confirmed_at=None),
        _managed("x_fine", evauth_expires="2027-01-31"),
    ]
    assert _account_kinds(users) == {
        ("unregistered_with_access", "uuid-stranger"): "high",
        ("unregistered_account", "uuid-idle"): "medium",
        ("lapsed_but_enabled", "x_expired"): "high",
        ("lapsed_but_enabled", "x_rejected"): "high",
        ("lapsed_but_enabled", "x_bad"): "high",
        ("invalid_account_metadata", "x_bad_off"): "medium",
        ("sponsor_gone_but_enabled", "x_orphan"): "high",
        ("expiring_soon", "x_soon"): "info",
        ("awaiting_sponsor", "x_wait"): "info",
    }


def test_sponsor_disabled_counts_as_gone(env):
    users = [_user("ou_boss", "boss", forbidden=True), _managed("x_a")]
    assert _account_kinds(users) == {("sponsor_gone_but_enabled", "x_a"): "high"}


def test_view_rows_carry_kind_sponsor_and_expiry(env):
    users = [_user("ou_boss", "boss", display="李四"), _managed("x_a", groups=["approved-app-a"]),
             _user("ou_emp", "emp", groups=["approved-app-a"])]
    rows, _ = ar.build(_snap(users, []), today=D)
    by_id = {r["open_id"]: r for r in rows}
    assert (by_id["x_a"]["kind"], by_id["x_a"]["sponsor"], by_id["x_a"]["expires"]) == ("外部合作方", "李四", "2026-12-31")
    assert (by_id["ou_emp"]["kind"], by_id["ou_emp"]["sponsor"], by_id["ou_emp"]["expires"]) == ("飞书员工", "", "")
