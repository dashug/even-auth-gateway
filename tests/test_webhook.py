import asyncio, hmac, hashlib, json
from even_auth_gov import webhook, approval_store

def test_bad_signature_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    monkeypatch.setenv("CASDOOR_WEBHOOK_SECRET", "shh")
    body = json.dumps({"action": "signup", "user": {"id": "ou_n", "name": "新人"}}).encode()
    r = asyncio.run(webhook.handle(body, signature="wrong"))
    assert r["status"] == "rejected"

def test_no_secret_configured_rejects(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    monkeypatch.delenv("CASDOOR_WEBHOOK_SECRET", raising=False)
    body = json.dumps({"action": "signup", "user": {"id": "ou_n"}}).encode()
    sig = hmac.new(b"", body, hashlib.sha256).hexdigest()
    r = asyncio.run(webhook.handle(body, signature=sig))
    assert r["status"] == "rejected"

def test_signup_creates_pending_and_sends_card(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    monkeypatch.setenv("CASDOOR_WEBHOOK_SECRET", "shh")
    sent = []
    async def fake_send(info): sent.append(info)
    monkeypatch.setattr(webhook, "send_approval_card", fake_send)
    body = json.dumps({"action": "signup", "user": {"id": "ou_n", "name": "新人", "email": "n@e.com"}}).encode()
    sig = hmac.new(b"shh", body, hashlib.sha256).hexdigest()
    r = asyncio.run(webhook.handle(body, signature=sig))
    assert r["status"] == "ok"
    assert approval_store.get("ou_n")["status"] == "pending"
    assert len(sent) == 1 and sent[0]["open_id"] == "ou_n"

def test_repeat_signup_no_duplicate_card(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    monkeypatch.setenv("CASDOOR_WEBHOOK_SECRET", "shh")
    sent = []
    async def fake_send(info): sent.append(info)
    monkeypatch.setattr(webhook, "send_approval_card", fake_send)
    body = json.dumps({"action": "signup", "user": {"id": "ou_n", "name": "新人"}}).encode()
    sig = hmac.new(b"shh", body, hashlib.sha256).hexdigest()
    for _ in range(2):
        asyncio.run(webhook.handle(body, signature=sig))
    assert len(sent) == 1  # second signup is already pending → no duplicate card

def test_non_user_event_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    monkeypatch.setenv("CASDOOR_WEBHOOK_SECRET", "shh")
    body = json.dumps({"action": "login", "user": {"id": "ou_n"}}).encode()
    sig = hmac.new(b"shh", body, hashlib.sha256).hexdigest()
    r = asyncio.run(webhook.handle(body, signature=sig))
    assert r["status"] == "ignored"

def test_missing_open_id_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    monkeypatch.setenv("CASDOOR_WEBHOOK_SECRET", "shh")
    body = json.dumps({"action": "signup", "user": {"name": "no id"}}).encode()
    sig = hmac.new(b"shh", body, hashlib.sha256).hexdigest()
    r = asyncio.run(webhook.handle(body, signature=sig))
    assert r["status"] == "ignored"
