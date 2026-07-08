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

    async def fake_handle(action, operator_id, owner, sso_open_id, app, client):
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

    async def fake_handle(action, operator_id, owner, sso_open_id, app, client):
        captured["app"] = app
        return {"status": "ok"}

    monkeypatch.setattr(decision, "handle", fake_handle)
    monkeypatch.setenv("DEFAULT_APP", "cs-hub")
    monkeypatch.delenv("APPROVER_CS_HUB", raising=False)

    data = _card_data({"action": "sso_approve", "sso_open_id": "ou_x"})  # no "app" key — pre-upgrade card
    asyncio.run(feishu_ws._process_card(data))

    assert captured["app"] == "cs-hub"
