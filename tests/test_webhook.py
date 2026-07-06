import asyncio, json
from even_auth_gov import webhook, approval_store


def test_bad_token_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    monkeypatch.setenv("CASDOOR_WEBHOOK_SECRET", "shh")
    body = json.dumps({"action": "signup", "object": json.dumps({"id": "ou_n", "name": "新人"})}).encode()
    r = asyncio.run(webhook.handle(body, token="wrong"))
    assert r["status"] == "rejected"


def test_no_secret_configured_rejects(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    monkeypatch.delenv("CASDOOR_WEBHOOK_SECRET", raising=False)
    body = json.dumps({"action": "signup", "object": json.dumps({"id": "ou_n"})}).encode()
    # 即使传空 token,也因未配密钥而 fail-closed
    r = asyncio.run(webhook.handle(body, token=""))
    assert r["status"] == "rejected"


def test_signup_object_string_creates_pending_and_sends_card(monkeypatch, tmp_path):
    """Casdoor Record 的 object 是 JSON 字符串形态的用户对象。"""
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    monkeypatch.setenv("CASDOOR_WEBHOOK_SECRET", "shh")
    sent = []
    async def fake_send(info): sent.append(info)
    monkeypatch.setattr(webhook, "send_approval_card", fake_send)
    body = json.dumps({
        "action": "signup",
        "object": json.dumps({"id": "ou_n", "name": "新人", "email": "n@e.com"}),
    }).encode()
    r = asyncio.run(webhook.handle(body, token="shh"))
    assert r["status"] == "ok"
    assert approval_store.get("ou_n")["status"] == "pending"
    assert len(sent) == 1 and sent[0]["open_id"] == "ou_n" and sent[0]["email"] == "n@e.com"


def test_add_user_extended_user_dict(monkeypatch, tmp_path):
    """webhook 勾选 isUserExtended 时,完整用户对象在 extendedUser。"""
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    monkeypatch.setenv("CASDOOR_WEBHOOK_SECRET", "shh")
    sent = []
    async def fake_send(info): sent.append(info)
    monkeypatch.setattr(webhook, "send_approval_card", fake_send)
    body = json.dumps({
        "action": "add-user",
        "extendedUser": {"id": "ou_ext", "name": "扩展人", "email": "e@e.com"},
        "object": "irrelevant",
    }).encode()
    r = asyncio.run(webhook.handle(body, token="shh"))
    assert r["status"] == "ok"
    assert approval_store.get("ou_ext")["status"] == "pending"
    assert sent[0]["open_id"] == "ou_ext"


def test_repeat_signup_no_duplicate_card(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    monkeypatch.setenv("CASDOOR_WEBHOOK_SECRET", "shh")
    sent = []
    async def fake_send(info): sent.append(info)
    monkeypatch.setattr(webhook, "send_approval_card", fake_send)
    body = json.dumps({"action": "signup", "object": json.dumps({"id": "ou_n", "name": "新人"})}).encode()
    for _ in range(2):
        asyncio.run(webhook.handle(body, token="shh"))
    assert len(sent) == 1  # 第二次已 pending → 不重复发卡


def test_non_user_event_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    monkeypatch.setenv("CASDOOR_WEBHOOK_SECRET", "shh")
    body = json.dumps({"action": "login", "object": json.dumps({"id": "ou_n"})}).encode()
    r = asyncio.run(webhook.handle(body, token="shh"))
    assert r["status"] == "ignored"


def test_missing_open_id_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    monkeypatch.setenv("CASDOOR_WEBHOOK_SECRET", "shh")
    body = json.dumps({"action": "signup", "object": json.dumps({"name": "no id"})}).encode()
    r = asyncio.run(webhook.handle(body, token="shh"))
    assert r["status"] == "ignored"
