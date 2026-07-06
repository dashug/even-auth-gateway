from fastapi.testclient import TestClient
import hmac, hashlib, json

def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    monkeypatch.setenv("CASDOOR_WEBHOOK_SECRET", "shh")
    monkeypatch.setenv("TESTING", "1")
    from even_auth_gov.app import build_app
    return build_app()

def test_webhook_route_rejects_bad_sig(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as c:
        r = c.post("/casdoor/webhook", content=b"{}", headers={"X-Casdoor-Signature": "bad"})
        assert r.status_code == 401

def test_webhook_route_accepts_good_sig(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    body = json.dumps({"action": "signup", "user": {"id": "ou_n", "name": "N"}}).encode()
    sig = hmac.new(b"shh", body, hashlib.sha256).hexdigest()
    with TestClient(app) as c:
        r = c.post("/casdoor/webhook", content=body, headers={"X-Casdoor-Signature": sig})
        assert r.status_code == 200

def test_card_callback_routes_to_decision(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    from even_auth_gov import approval_store, app as appmod
    approval_store.mark_pending("ou_a", {"name": "申请人"})
    # stub decision + config so the card callback is deterministic
    async def fake_decision(action, operator_id, owner, sso_open_id, client):
        return {"status": "ok", "message": f"Approved: {sso_open_id}"}
    monkeypatch.setattr(appmod.decision, "handle", fake_decision)
    monkeypatch.setattr(appmod, "_owner", lambda: "ou_boss")
    with TestClient(app) as c:
        payload = {"operator": {"open_id": "ou_boss"},
                   "action": {"value": {"action": "sso_approve", "sso_open_id": "ou_a"}}}
        r = c.post("/feishu/card", json=payload)
        assert r.status_code == 200
        assert r.json()["toast"]["type"] == "success"
