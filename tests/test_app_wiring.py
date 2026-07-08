from fastapi.testclient import TestClient
import json

def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    monkeypatch.setenv("CASDOOR_WEBHOOK_SECRET", "shh")
    monkeypatch.setenv("TESTING", "1")
    from even_auth_gov.app import build_app
    return build_app()

def _app_live(monkeypatch, tmp_path):
    """Like _app() but with TESTING unset so the wired _send_card actually runs its
    full body (build the card + call feishu.send_card) — needed to observe card
    content / call counts end-to-end for /request-access. WS + scheduler stay off
    via their own env switches so no background threads spin up."""
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    monkeypatch.setenv("CASDOOR_WEBHOOK_SECRET", "shh")
    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.setenv("CHANNEL_CLIENTS_ENABLED", "false")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    from even_auth_gov.app import build_app
    return build_app()

def test_webhook_route_rejects_bad_token(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as c:
        r = c.post("/casdoor/webhook", content=b"{}", headers={"X-Webhook-Token": "bad"})
        assert r.status_code == 401

def test_webhook_route_accepts_good_token(monkeypatch, tmp_path):
    # Casdoor 认证靠自定义头传共享 token(默认 header 名 X-Webhook-Token),非 body 签名
    app = _app(monkeypatch, tmp_path)
    body = json.dumps({"action": "signup", "object": json.dumps({"id": "ou_n", "name": "N"})}).encode()
    with TestClient(app) as c:
        r = c.post("/casdoor/webhook", content=body, headers={"X-Webhook-Token": "shh"})
        assert r.status_code == 200

def test_webhook_route_custom_header_name(monkeypatch, tmp_path):
    # header 名可配:CASDOOR_WEBHOOK_HEADER
    monkeypatch.setenv("CASDOOR_WEBHOOK_HEADER", "Authorization")
    app = _app(monkeypatch, tmp_path)
    body = json.dumps({"action": "signup", "object": json.dumps({"id": "ou_m", "name": "M"})}).encode()
    with TestClient(app) as c:
        r = c.post("/casdoor/webhook", content=body, headers={"Authorization": "shh"})
        assert r.status_code == 200

def test_no_unauthenticated_http_card_route(monkeypatch, tmp_path):
    """安全回归:卡片回调只走 WS(SDK 验签)。禁止存在明文 HTTP /feishu/card 路由——
    否则任何人可 curl 伪造 operator.open_id 自助批准。见 casdoor-findings / 设计评审 #1。"""
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as c:
        payload = {"operator": {"open_id": "ou_boss"},
                   "action": {"value": {"action": "sso_approve", "sso_open_id": "ou_a"}}}
        r = c.post("/feishu/card", json=payload)
        assert r.status_code == 404  # 路由已移除,伪造审批无门可入


# ── /request-access (design-review #2/#16): app #2+ onboarding ─────────────

def test_request_access_wrong_token_rejected(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as c:
        r = c.post("/request-access", json={"app": "app-b", "open_id": "ou_x", "name": "X", "email": ""},
                   headers={"X-Webhook-Token": "bad"})
        assert r.status_code == 401

def test_request_access_no_token_rejected(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as c:
        r = c.post("/request-access", json={"app": "app-b", "open_id": "ou_x", "name": "X", "email": ""})
        assert r.status_code == 401

def test_request_access_missing_fields_rejected(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as c:
        r = c.post("/request-access", json={"app": "app-b"}, headers={"X-Webhook-Token": "shh"})
        assert r.status_code == 400

def test_request_access_valid_token_creates_pending(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)  # TESTING=1 → card send no-ops, we only check store side effects
    with TestClient(app) as c:
        r = c.post("/request-access", json={"app": "app-b", "open_id": "ou_x", "name": "X", "email": "x@e.com"},
                   headers={"X-Webhook-Token": "shh"})
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    from even_auth_gov import approval_store
    rec = approval_store.get("ou_x", "app-b")
    assert rec is not None and rec["status"] == "pending"

def test_request_access_duplicate_returns_exists_and_does_not_reset(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as c:
        r1 = c.post("/request-access", json={"app": "app-b", "open_id": "ou_x", "name": "X", "email": ""},
                    headers={"X-Webhook-Token": "shh"})
        r2 = c.post("/request-access", json={"app": "app-b", "open_id": "ou_x", "name": "X", "email": ""},
                    headers={"X-Webhook-Token": "shh"})
    assert r1.json() == {"status": "ok"}
    assert r2.json() == {"status": "exists"}

def test_request_access_pending_for_one_app_does_not_affect_another(monkeypatch, tmp_path):
    """同一用户先对 app-a 申请、再对 app-b 申请 —— 两条 pending 记录独立存在。"""
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as c:
        r1 = c.post("/request-access", json={"app": "app-a", "open_id": "ou_x", "name": "X", "email": ""},
                    headers={"X-Webhook-Token": "shh"})
        r2 = c.post("/request-access", json={"app": "app-b", "open_id": "ou_x", "name": "X", "email": ""},
                    headers={"X-Webhook-Token": "shh"})
    assert r1.json() == {"status": "ok"}
    assert r2.json() == {"status": "ok"}   # 不同 app,不是重复
    from even_auth_gov import approval_store
    assert approval_store.get("ou_x", "app-a")["status"] == "pending"
    assert approval_store.get("ou_x", "app-b")["status"] == "pending"

def test_request_access_sends_card_carrying_app_to_its_own_approver(monkeypatch, tmp_path):
    """端到端:card 送达该 app 专属审批人(settings.approver_for),按钮 value 携带 app。"""
    monkeypatch.setenv("APPROVER_APP_B", "ou_app_b_boss")
    app = _app_live(monkeypatch, tmp_path)
    sent = []
    async def fake_send_card(receive_id, card, receive_id_type="open_id"):
        sent.append((receive_id, card)); return True
    monkeypatch.setattr("even_auth_gov.feishu.send_card", fake_send_card)
    with TestClient(app) as c:
        r = c.post("/request-access", json={"app": "app-b", "open_id": "ou_x", "name": "X", "email": "x@e.com"},
                   headers={"X-Webhook-Token": "shh"})
    assert r.status_code == 200
    assert len(sent) == 1
    receive_id, card = sent[0]
    assert receive_id == "ou_app_b_boss"   # per-app approver, not the global default
    action_buttons = card["elements"][-1]["actions"]
    assert all(btn["value"]["app"] == "app-b" for btn in action_buttons)
    assert all(btn["value"]["sso_open_id"] == "ou_x" for btn in action_buttons)

def test_request_access_duplicate_does_not_resend_card(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVER_APP_B", "ou_app_b_boss")
    app = _app_live(monkeypatch, tmp_path)
    sent = []
    async def fake_send_card(receive_id, card, receive_id_type="open_id"):
        sent.append(receive_id); return True
    monkeypatch.setattr("even_auth_gov.feishu.send_card", fake_send_card)
    with TestClient(app) as c:
        c.post("/request-access", json={"app": "app-b", "open_id": "ou_x", "name": "X", "email": ""},
               headers={"X-Webhook-Token": "shh"})
        c.post("/request-access", json={"app": "app-b", "open_id": "ou_x", "name": "X", "email": ""},
               headers={"X-Webhook-Token": "shh"})
    assert len(sent) == 1   # 第二次(已 pending)不重发卡片
