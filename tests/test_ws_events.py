import asyncio
from types import SimpleNamespace
from even_auth_gov import ws_events

def _status(**kw):
    b = dict(is_frozen=False, is_resigned=False, is_exited=False); b.update(kw); return SimpleNamespace(**b)

def test_deleted_event_triggers_offboard(monkeypatch):
    calls = []
    async def fake_apply(open_id, name, reason, client): calls.append((open_id, reason))
    monkeypatch.setattr(ws_events.offboard, "apply", fake_apply)
    data = SimpleNamespace(event=SimpleNamespace(object=SimpleNamespace(open_id="ou_l", name="离职", status=None)))
    asyncio.run(ws_events.on_user_deleted(data))
    assert calls == [("ou_l", "offboard_deleted")]

def test_frozen_update_triggers_offboard(monkeypatch):
    calls = []
    async def fake_apply(open_id, name, reason, client): calls.append((open_id, reason))
    monkeypatch.setattr(ws_events.offboard, "apply", fake_apply)
    data = SimpleNamespace(event=SimpleNamespace(
        object=SimpleNamespace(open_id="ou_l", name="离职", status=_status(is_frozen=True)),
        old_object=SimpleNamespace(status=_status())))
    asyncio.run(ws_events.on_user_updated(data))
    assert calls == [("ou_l", "offboard_event")]

def test_resigned_update_triggers_offboard(monkeypatch):
    calls = []
    async def fake_apply(open_id, name, reason, client): calls.append((open_id, reason))
    monkeypatch.setattr(ws_events.offboard, "apply", fake_apply)
    data = SimpleNamespace(event=SimpleNamespace(
        object=SimpleNamespace(open_id="ou_l", name="离职", status=_status(is_resigned=True)),
        old_object=SimpleNamespace(status=_status())))
    asyncio.run(ws_events.on_user_updated(data))
    assert calls == [("ou_l", "offboard_event")]

def test_unrelated_update_noop(monkeypatch):
    calls = []
    async def fake_apply(*a, **k): calls.append(1)
    monkeypatch.setattr(ws_events.offboard, "apply", fake_apply)
    data = SimpleNamespace(event=SimpleNamespace(
        object=SimpleNamespace(open_id="ou_l", name="改名", status=_status()),
        old_object=SimpleNamespace(status=_status())))
    asyncio.run(ws_events.on_user_updated(data))
    assert calls == []

def test_update_no_old_object_falls_back_and_offboards(monkeypatch):
    calls = []
    async def fake_apply(open_id, name, reason, client): calls.append((open_id, reason))
    monkeypatch.setattr(ws_events.offboard, "apply", fake_apply)
    data = SimpleNamespace(event=SimpleNamespace(
        object=SimpleNamespace(open_id="ou_l", name="离职", status=_status(is_frozen=True))))
    asyncio.run(ws_events.on_user_updated(data))
    assert calls == [("ou_l", "offboard_event")]

def test_deleted_unknown_shape_no_raise(monkeypatch):
    async def fake_apply(*a, **k): pass
    monkeypatch.setattr(ws_events.offboard, "apply", fake_apply)
    data = SimpleNamespace(event=SimpleNamespace(object=SimpleNamespace(open_id="", name="", status=None)))
    asyncio.run(ws_events.on_user_deleted(data))  # empty open_id → offboard.apply guards it; must not raise
