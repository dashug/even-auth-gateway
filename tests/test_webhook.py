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


def test_card_send_failure_keeps_pending_unnotified_and_returns_ok(monkeypatch, tmp_path):
    """#12:推卡片失败(抛异常)不该让 webhook 500;pending 照建、notified_at 留空等对账补发。"""
    _env(monkeypatch, tmp_path)
    async def failing_send(info): raise RuntimeError("feishu down")
    monkeypatch.setattr(webhook, "send_approval_card", failing_send)
    body = json.dumps({"action": "signup", "object": json.dumps({"id": "ou_n", "name": "新人"})}).encode()
    r = asyncio.run(webhook.handle(body, token="shh"))
    assert r["status"] == "ok"
    rec = approval_store.get("ou_n", "app-a")
    assert rec["status"] == "pending" and rec["notified_at"] == ""


def test_card_shows_display_name_not_random_casdoor_name(monkeypatch, tmp_path):
    """飞书用户在 Casdoor 的 name 是随机串(casdoor-findings §1),审批人要看的是 displayName。"""
    _env(monkeypatch, tmp_path)
    sent = []
    async def fake_send(info): sent.append(info)
    monkeypatch.setattr(webhook, "send_approval_card", fake_send)
    body = json.dumps({"action": "signup", "extendedUser": {
        "id": "ou_n", "name": "a8f3k2x9", "displayName": "张三", "email": "z@e.com"}}).encode()
    asyncio.run(webhook.handle(body, token="shh"))
    assert sent[0]["name"] == "张三"
    assert approval_store.get("ou_n", "app-a")["name"] == "张三"


def test_notify_pending_passes_recipient_and_note_through(monkeypatch, tmp_path):
    """重发/升级时指定收卡人(to)和卡片说明(note);不指定时不带这两个键,由 _send_card 按应用找审批人。"""
    import asyncio
    from even_auth_gov import webhook, approval_store
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    approval_store.mark_pending("ou_a", "app-a", {"name": "A"})
    seen = []
    async def fake_send(info): seen.append(info)
    monkeypatch.setattr(webhook, "send_approval_card", fake_send)
    assert asyncio.run(webhook.notify_pending("ou_a", "app-a", "A", "", to="ou_esc", note="升级")) is True
    assert asyncio.run(webhook.notify_pending("ou_a", "app-a", "A")) is True
    assert seen[0]["to"] == "ou_esc" and seen[0]["note"] == "升级"
    assert "to" not in seen[1] and "note" not in seen[1]


# ── 里程碑 4a:非飞书账号闸门 ──

from datetime import timedelta
import httpx
from even_auth_gov import accounts


def _casdoor_with(monkeypatch, user, status="ok"):
    def handler(req):
        return httpx.Response(200, json={"status": status, "data": user})
    monkeypatch.setattr("even_auth_gov.casdoor_admin.make_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://casdoor"))


def _managed_props():
    return {"evauth_kind": "partner", "evauth_sponsor": "ou_boss",
            "evauth_expires": (accounts.today() + timedelta(days=30)).isoformat(),
            "evauth_confirmed_at": "2026-11-01T00:00:00+00:00"}


def _signup(uid):
    return json.dumps({"action": "signup", "extendedUser": {"id": uid, "name": "n", "displayName": "王总(CEO)"}}).encode()


def test_self_signup_of_unregistered_account_gets_no_card(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _casdoor_with(monkeypatch, {"id": "uuid-1", "owner": "even-test", "name": "n", "properties": {}})
    sent = []
    async def fake_send(info): sent.append(info)
    monkeypatch.setattr(webhook, "send_approval_card", fake_send)
    r = asyncio.run(webhook.handle(_signup("uuid-1"), token="shh"))
    assert r["status"] == "ignored" and sent == []
    assert approval_store.get("uuid-1", "app-a") is None


def test_unconfirmed_managed_account_creation_gets_no_card(monkeypatch, tmp_path):
    """命令行建号可能触发 add-user webhook:账号停用中、负责人没确认 → 不发卡片。"""
    _env(monkeypatch, tmp_path)
    props = {k: v for k, v in _managed_props().items() if k != "evauth_confirmed_at"}
    _casdoor_with(monkeypatch, {"id": "x_1", "owner": "even-test", "name": "x_1", "isForbidden": True, "properties": props})
    sent = []
    async def fake_send(info): sent.append(info)
    monkeypatch.setattr(webhook, "send_approval_card", fake_send)
    assert asyncio.run(webhook.handle(_signup("x_1"), token="shh"))["status"] == "ignored" and sent == []


def test_eligible_managed_account_goes_to_approval(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _casdoor_with(monkeypatch, {"id": "x_1", "owner": "even-test", "name": "x_1", "properties": _managed_props()})
    sent = []
    async def fake_send(info): sent.append(info)
    monkeypatch.setattr(webhook, "send_approval_card", fake_send)
    assert asyncio.run(webhook.handle(_signup("x_1"), token="shh"))["status"] == "ok"
    assert approval_store.get("x_1", "app-a")["status"] == "pending" and len(sent) == 1


def test_gate_fails_closed_when_casdoor_unreadable(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _casdoor_with(monkeypatch, None, status="error")
    assert asyncio.run(webhook.handle(_signup("x_1"), token="shh"))["status"] == "ignored"
    assert approval_store.get("x_1", "app-a") is None


def test_feishu_signup_does_not_query_casdoor(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    def boom():
        raise AssertionError("飞书账号不该查 Casdoor")
    monkeypatch.setattr("even_auth_gov.casdoor_admin.make_client", boom)
    async def fake_send(info): pass
    monkeypatch.setattr(webhook, "send_approval_card", fake_send)
    assert asyncio.run(webhook.handle(_signup("ou_new"), token="shh"))["status"] == "ok"
