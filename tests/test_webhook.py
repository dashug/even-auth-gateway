import asyncio, json
from even_auth_gov import webhook, approval_store


def _env(monkeypatch, tmp_path, default_app="app-a"):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    monkeypatch.setenv("CASDOOR_WEBHOOK_SECRET", "shh")
    monkeypatch.setenv("DEFAULT_APP", default_app)


def test_bad_token_rejected(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
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
    """Casdoor Record 的 object 是 JSON 字符串形态的用户对象。
    Casdoor signup webhook 不带 app 信息,落到 settings.default_app()(#2/#16)。"""
    _env(monkeypatch, tmp_path)
    sent = []
    async def fake_send(info): sent.append(info)
    monkeypatch.setattr(webhook, "send_approval_card", fake_send)
    body = json.dumps({
        "action": "signup",
        "object": json.dumps({"id": "ou_n", "name": "新人", "email": "n@e.com"}),
    }).encode()
    r = asyncio.run(webhook.handle(body, token="shh"))
    assert r["status"] == "ok"
    assert approval_store.get("ou_n", "app-a")["status"] == "pending"
    assert len(sent) == 1 and sent[0]["open_id"] == "ou_n" and sent[0]["email"] == "n@e.com"
    assert sent[0]["app"] == "app-a"   # 卡片携带 app 身份


def test_add_user_extended_user_dict(monkeypatch, tmp_path):
    """webhook 勾选 isUserExtended 时,完整用户对象在 extendedUser。"""
    _env(monkeypatch, tmp_path)
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
    assert approval_store.get("ou_ext", "app-a")["status"] == "pending"
    assert sent[0]["open_id"] == "ou_ext"
    assert sent[0]["app"] == "app-a"


def test_repeat_signup_no_duplicate_card(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    sent = []
    async def fake_send(info): sent.append(info)
    monkeypatch.setattr(webhook, "send_approval_card", fake_send)
    body = json.dumps({"action": "signup", "object": json.dumps({"id": "ou_n", "name": "新人"})}).encode()
    for _ in range(2):
        asyncio.run(webhook.handle(body, token="shh"))
    assert len(sent) == 1  # 第二次已 pending → 不重复发卡


def test_non_user_event_ignored(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    body = json.dumps({"action": "login", "object": json.dumps({"id": "ou_n"})}).encode()
    r = asyncio.run(webhook.handle(body, token="shh"))
    assert r["status"] == "ignored"


def test_missing_open_id_ignored(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    body = json.dumps({"action": "signup", "object": json.dumps({"name": "no id"})}).encode()
    r = asyncio.run(webhook.handle(body, token="shh"))
    assert r["status"] == "ignored"


def test_signup_uses_settings_default_app_when_env_unset(monkeypatch, tmp_path):
    """未设 DEFAULT_APP 时,回退到 settings.default_app() 的内建默认值(cs-hub),
    保证现有 cs-hub 试点在升级后不需要任何配置变更。"""
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    monkeypatch.setenv("CASDOOR_WEBHOOK_SECRET", "shh")
    monkeypatch.delenv("DEFAULT_APP", raising=False)
    sent = []
    async def fake_send(info): sent.append(info)
    monkeypatch.setattr(webhook, "send_approval_card", fake_send)
    body = json.dumps({"action": "signup", "object": json.dumps({"id": "ou_legacy", "name": "老用户"})}).encode()
    r = asyncio.run(webhook.handle(body, token="shh"))
    assert r["status"] == "ok"
    assert approval_store.get("ou_legacy", "cs-hub")["status"] == "pending"
    assert sent[0]["app"] == "cs-hub"
