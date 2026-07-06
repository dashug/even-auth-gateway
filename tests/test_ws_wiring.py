import asyncio

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
