import asyncio
from types import SimpleNamespace

import lark_oapi

from even_auth_gov import feishu_ws


class _FakeBuilder:
    def __init__(self, calls):
        self._calls = calls

    def register_p2_contact_user_deleted_v3(self, fn):
        self._calls.append(("deleted", fn))
        return self

    def register_p2_contact_user_updated_v3(self, fn):
        self._calls.append(("updated", fn))
        return self

    def register_p2_card_action_trigger(self, fn):
        self._calls.append(("card", fn))
        return self

    def build(self):
        return "fake-handler"


def _make_fake_dispatcher(calls):
    class _FakeEventDispatcherHandler:
        @staticmethod
        def builder(encrypt_key, verification_token):
            return _FakeBuilder(calls)

    return _FakeEventDispatcherHandler


class _FakeWSClient:
    """Never actually connects — no network calls, fails fast so the WS
    thread's reconnect backoff loop is reached (and interruptible) quickly."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self._conn = None

    async def _connect(self):
        self._conn = None  # simulate a failed handshake, no I/O

    async def _ping_loop(self):
        while True:
            await asyncio.sleep(3600)

    async def _disconnect(self):
        self._conn = None


def _reset():
    feishu_ws.stop_ws()
    feishu_ws._main_loop = None


def test_start_ws_noop_without_credentials(monkeypatch):
    _reset()
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)

    loop = asyncio.new_event_loop()
    try:
        feishu_ws.start_ws(loop)  # should log + return, no thread spawned
    finally:
        loop.close()

    assert feishu_ws._ws_thread is None
    _reset()


def test_start_ws_registers_offboarding_and_card_handlers(monkeypatch):
    _reset()
    monkeypatch.setenv("FEISHU_APP_ID", "cli_dummy")
    monkeypatch.setenv("FEISHU_APP_SECRET", "dummy_secret")
    monkeypatch.setenv("FEISHU_ENCRYPT_KEY", "")
    monkeypatch.setenv("FEISHU_VERIFICATION_TOKEN", "")

    calls = []
    monkeypatch.setattr(lark_oapi, "EventDispatcherHandler", _make_fake_dispatcher(calls))
    monkeypatch.setattr(lark_oapi.ws, "Client", _FakeWSClient)

    loop = asyncio.new_event_loop()
    try:
        feishu_ws.start_ws(loop)
        registered = {name for name, _fn in calls}
        assert registered == {"deleted", "updated", "card"}
        # handlers are the module's own functions (not lambdas), so a
        # scheduled event actually reaches ws_events / decision.handle
        registered_fns = {name: fn for name, fn in calls}
        assert registered_fns["deleted"] is feishu_ws._on_deleted
        assert registered_fns["updated"] is feishu_ws._on_updated
        assert registered_fns["card"] is feishu_ws._on_card
    finally:
        feishu_ws.stop_ws()
        loop.close()
        _reset()


def test_start_ws_second_call_is_idempotent(monkeypatch):
    _reset()
    monkeypatch.setenv("FEISHU_APP_ID", "cli_dummy")
    monkeypatch.setenv("FEISHU_APP_SECRET", "dummy_secret")

    calls = []
    monkeypatch.setattr(lark_oapi, "EventDispatcherHandler", _make_fake_dispatcher(calls))
    monkeypatch.setattr(lark_oapi.ws, "Client", _FakeWSClient)

    loop = asyncio.new_event_loop()
    try:
        feishu_ws.start_ws(loop)
        first_thread = feishu_ws._ws_thread
        feishu_ws.start_ws(loop)  # already running -> should no-op, not spawn a 2nd thread
        assert feishu_ws._ws_thread is first_thread
    finally:
        feishu_ws.stop_ws()
        loop.close()
        _reset()


# ── APP-AWARE (design-review #2/#16): _process_card extracts app identity ──

def _card_data(value: dict, operator_open_id: str = "ou_boss"):
    return SimpleNamespace(event=SimpleNamespace(
        action=SimpleNamespace(value=value),
        operator=SimpleNamespace(open_id=operator_open_id),
    ))


def test_process_card_extracts_app_and_uses_its_approver(monkeypatch):
    from even_auth_gov import decision

    captured = {}

    async def fake_handle(action, operator_id, owner, sso_open_id, app, client, role=""):
        captured.update(action=action, operator_id=operator_id, owner=owner, sso_open_id=sso_open_id, app=app)
        return {"status": "ok"}

    monkeypatch.setattr(decision, "handle", fake_handle)
    monkeypatch.setenv("APPROVER_APP_B", "ou_app_b_boss")

    data = _card_data({"action": "sso_approve", "sso_open_id": "ou_x", "app": "app-b"}, operator_open_id="ou_app_b_boss")
    asyncio.run(feishu_ws._process_card(data))

    assert captured["app"] == "app-b"
    assert captured["sso_open_id"] == "ou_x"
    assert captured["operator_id"] == "ou_app_b_boss"
    assert captured["owner"] == "ou_app_b_boss"   # per-app approver, not necessarily the global default


def test_process_card_defaults_app_for_old_cards_without_app_field(monkeypatch):
    from even_auth_gov import decision

    captured = {}

    async def fake_handle(action, operator_id, owner, sso_open_id, app, client, role=""):
        captured["app"] = app
        return {"status": "ok"}

    monkeypatch.setattr(decision, "handle", fake_handle)
    monkeypatch.setenv("DEFAULT_APP", "cs-hub")
    monkeypatch.delenv("APPROVER_CS_HUB", raising=False)

    data = _card_data({"action": "sso_approve", "sso_open_id": "ou_x"})  # no "app" key — pre-upgrade card
    asyncio.run(feishu_ws._process_card(data))

    assert captured["app"] == "cs-hub"


# ── 审批结果回告:审批人收到处理结果(尤其失败),申请人收到批准/拒绝通知 ──

def _capture_texts(monkeypatch):
    sent = []
    async def fake_send_text(receive_id, text, receive_id_type="open_id"):
        sent.append((receive_id, text)); return True
    monkeypatch.setattr("even_auth_gov.feishu.send_text", fake_send_text)
    return sent


def test_process_card_approve_notifies_approver_and_applicant(monkeypatch):
    from even_auth_gov import decision

    async def fake_handle(action, operator_id, owner, sso_open_id, app, client, role=""):
        return {"status": "ok", "message": "已批准: X", "decision": "approved"}

    monkeypatch.setattr(decision, "handle", fake_handle)
    monkeypatch.setenv("APPROVER_APP_B", "ou_boss")
    sent = _capture_texts(monkeypatch)
    asyncio.run(feishu_ws._process_card(_card_data({"action": "sso_approve", "sso_open_id": "ou_x", "app": "app-b"})))

    to = {rid: text for rid, text in sent}
    assert "已批准" in to["ou_boss"] and "app-b" in to["ou_boss"]
    assert "app-b" in to["ou_x"] and "批准" in to["ou_x"]


def test_process_card_failure_alerts_approver_only(monkeypatch):
    """Casdoor 加组失败:审批人必须知道(否则以为已批),申请人不该收到任何结果。"""
    from even_auth_gov import decision

    async def fake_handle(action, operator_id, owner, sso_open_id, app, client, role=""):
        return {"status": "error", "message": "Casdoor 加组失败,请重试"}

    monkeypatch.setattr(decision, "handle", fake_handle)
    monkeypatch.setenv("APPROVER_APP_B", "ou_boss")
    sent = _capture_texts(monkeypatch)
    asyncio.run(feishu_ws._process_card(_card_data({"action": "sso_approve", "sso_open_id": "ou_x", "app": "app-b"})))

    assert [rid for rid, _ in sent] == ["ou_boss"]
    assert "加组失败" in sent[0][1]


def test_process_card_idempotent_click_does_not_renotify_applicant(monkeypatch):
    from even_auth_gov import decision

    async def fake_handle(action, operator_id, owner, sso_open_id, app, client, role=""):
        return {"status": "ok", "message": "已处理过: approved"}   # 无 decision 字段

    monkeypatch.setattr(decision, "handle", fake_handle)
    monkeypatch.setenv("APPROVER_APP_B", "ou_boss")
    sent = _capture_texts(monkeypatch)
    asyncio.run(feishu_ws._process_card(_card_data({"action": "sso_approve", "sso_open_id": "ou_x", "app": "app-b"})))

    assert [rid for rid, _ in sent] == ["ou_boss"]


# ── 审批即赋角:按钮 value 里的 role 透传给 decision,批准通知告诉申请人角色 ──

def test_process_card_passes_role_from_button_to_decision(monkeypatch):
    from even_auth_gov import decision

    captured = {}

    async def fake_handle(action, operator_id, owner, sso_open_id, app, client, role=""):
        captured["role"] = role
        return {"status": "ok"}

    monkeypatch.setattr(decision, "handle", fake_handle)
    monkeypatch.setenv("APPROVER_APP_B", "ou_boss")
    _capture_texts(monkeypatch)
    asyncio.run(feishu_ws._process_card(_card_data(
        {"action": "sso_approve", "sso_open_id": "ou_x", "app": "app-b", "role": "app-b-editor"})))
    assert captured["role"] == "app-b-editor"


def test_approved_with_role_tells_applicant_the_role(monkeypatch):
    from even_auth_gov import decision

    async def fake_handle(action, operator_id, owner, sso_open_id, app, client, role=""):
        return {"status": "ok", "message": "已批准 X(角色:编辑者)", "decision": "approved", "role": "编辑者"}

    monkeypatch.setattr(decision, "handle", fake_handle)
    monkeypatch.setenv("APPROVER_APP_B", "ou_boss")
    sent = _capture_texts(monkeypatch)
    asyncio.run(feishu_ws._process_card(_card_data(
        {"action": "sso_approve", "sso_open_id": "ou_x", "app": "app-b", "role": "app-b-editor"})))
    to = {rid: text for rid, text in sent}
    assert "编辑者" in to["ou_x"] and "app-b" in to["ou_x"]


# ── 角色强制:点了不带角色的旧卡片/降级卡片 → decision 要求重发,给点击人补一张带角色按钮的卡片 ──

def test_process_card_resends_role_card_to_clicker_when_decision_asks(monkeypatch, tmp_path):
    from even_auth_gov import decision, webhook, approval_store

    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    approval_store.mark_pending("ou_x", "app-b", {"name": "申请人", "email": "x@e.com"})

    async def fake_handle(action, operator_id, owner, sso_open_id, app, client, role=""):
        return {"status": "error", "message": "该应用已分角色,请在新卡片上选择角色", "resend_card": True}

    resent = []
    async def fake_notify(open_id, app, name="", email="", to="", note=""):
        resent.append((open_id, app, name, email, to)); return True

    monkeypatch.setattr(decision, "handle", fake_handle)
    monkeypatch.setattr(webhook, "notify_pending", fake_notify)
    monkeypatch.setenv("APPROVER_APP_B", "ou_boss")
    sent = _capture_texts(monkeypatch)
    asyncio.run(feishu_ws._process_card(_card_data({"action": "sso_approve", "sso_open_id": "ou_x", "app": "app-b"},
                                                    operator_open_id="ou_boss")))
    assert resent == [("ou_x", "app-b", "申请人", "x@e.com", "ou_boss")]
    assert [rid for rid, _ in sent] == ["ou_boss"]          # 申请人不收任何结果


def test_process_card_error_without_resend_flag_does_not_resend(monkeypatch, tmp_path):
    from even_auth_gov import decision, webhook

    async def fake_handle(action, operator_id, owner, sso_open_id, app, client, role=""):
        return {"status": "error", "message": "Casdoor 加组失败"}

    resent = []
    async def fake_notify(*a, **k): resent.append(a); return True
    monkeypatch.setattr(decision, "handle", fake_handle)
    monkeypatch.setattr(webhook, "notify_pending", fake_notify)
    monkeypatch.setenv("APPROVER_APP_B", "ou_boss")
    _capture_texts(monkeypatch)
    asyncio.run(feishu_ws._process_card(_card_data({"action": "sso_approve", "sso_open_id": "ou_x", "app": "app-b"})))
    assert resent == []
