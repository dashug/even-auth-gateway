from fastapi.testclient import TestClient
import json
import httpx
from tests._jwt import APP_CLIENT_IDS, CID_C, OTHER_KEY, jwks, token


def _fake_casdoor(monkeypatch, roles=None, jwks_status=200, roles_ok=True, user_groups=None,
                  forbidden=False, user_ok=True, users=None):
    """把 casdoor_admin.make_client 换成内存假 Casdoor:提供 JWKS、get-user(账号实时状态)与 get-roles。"""
    from even_auth_gov import id_token
    id_token.clear_cache()
    seen = {"paths": []}
    def handler(req):
        seen["paths"].append(req.url.path)
        if req.url.path == "/.well-known/jwks":
            return httpx.Response(jwks_status, json=jwks() if jwks_status == 200 else {})
        if req.url.path == "/api/get-user":
            if not user_ok:
                return httpx.Response(200, json={"status": "error", "msg": "boom", "data": None})
            uid = req.url.params.get("userId")
            data = {"owner": "even-test", "name": "randname", "id": uid,
                    "groups": user_groups or [], "isForbidden": forbidden}
            data.update((users or {}).get(uid, {}))   # users:按 id 覆盖字段(如受管账号的 properties)
            return httpx.Response(200, json={"status": "ok", "data": data})
        if req.url.path == "/api/get-roles":
            if not roles_ok:
                return httpx.Response(200, json={"status": "error", "msg": "boom", "data": None})
            return httpx.Response(200, json={"status": "ok", "data": roles or []})
        return httpx.Response(404)
    monkeypatch.setattr("even_auth_gov.casdoor_admin.make_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://casdoor"))
    return seen

def _app(monkeypatch, tmp_path, **casdoor):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    monkeypatch.setenv("CASDOOR_WEBHOOK_SECRET", "shh")
    monkeypatch.setenv("APP_CLIENT_IDS", APP_CLIENT_IDS)
    monkeypatch.setenv("TESTING", "1")
    _fake_casdoor(monkeypatch, **casdoor)
    from even_auth_gov.app import build_app
    return build_app()

def _app_live(monkeypatch, tmp_path, **casdoor):
    """Like _app() but with TESTING unset so the wired _send_card actually runs its
    full body (build the card + call feishu.send_card) — needed to observe card
    content / call counts end-to-end for /request-access. WS + scheduler stay off
    via their own env switches so no background threads spin up."""
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    monkeypatch.setenv("CASDOOR_WEBHOOK_SECRET", "shh")
    monkeypatch.setenv("APP_CLIENT_IDS", APP_CLIENT_IDS)
    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.setenv("CHANNEL_CLIENTS_ENABLED", "false")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    _fake_casdoor(monkeypatch, **casdoor)
    from even_auth_gov.app import build_app
    return build_app()

def _ra(c, tok=None, **extra):
    """POST /request-access,默认带 alice 登录 app-b 的 id_token。"""
    return c.post("/request-access", json={"id_token": tok if tok is not None else token(), **extra})

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


# ── /request-access:应用 #2+ 上报待批。鉴权 = 用户刚登录拿到的 Casdoor id_token ─────
# 身份(sub/displayName/email)与应用(aud→APP_CLIENT_IDS)都取自 Casdoor 签名过的 claim,
# 调用方在 body 里填的任何身份字段一律不信(防冒名骗批准)。

def test_request_access_without_id_token_rejected(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as c:
        r = c.post("/request-access", json={})
    assert r.status_code == 400

def test_request_access_shared_webhook_secret_no_longer_accepted(monkeypatch, tmp_path):
    """旧契约(共享密钥 + body 里自报 open_id/name)已废弃:拿到 webhook 密钥也不能再上报。"""
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as c:
        r = c.post("/request-access", json={"app": "app-b", "open_id": "ou_x", "name": "X", "email": ""},
                   headers={"X-Webhook-Token": "shh"})
    assert r.status_code == 400
    from even_auth_gov import approval_store
    assert approval_store.get("ou_x", "app-b") is None

def test_request_access_invalid_token_rejected(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as c:
        assert _ra(c, "garbage").status_code == 401
        assert _ra(c, token(key=OTHER_KEY)).status_code == 401

def test_request_access_unregistered_app_forbidden(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as c:
        r = _ra(c, token(aud=["cid-unknown"], azp="cid-unknown"))
    assert r.status_code == 403

def test_request_access_jwks_unavailable_is_503(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, jwks_status=502)
    with TestClient(app) as c:
        r = _ra(c)
    assert r.status_code == 503

def test_request_access_valid_token_creates_pending_from_signed_claims(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)  # TESTING=1 → card send no-ops, we only check store side effects
    with TestClient(app) as c:
        r = _ra(c)
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "app": "app-b"}
    from even_auth_gov import approval_store
    rec = approval_store.get("ou_alice", "app-b")
    assert rec["status"] == "pending"
    assert (rec["name"], rec["email"]) == ("Alice", "alice@e.com")

def test_request_access_ignores_caller_supplied_identity(monkeypatch, tmp_path):
    """冒名回归:body 里自报的 app/open_id/name 全部忽略,只认 token。"""
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as c:
        r = _ra(c, app="finance", open_id="ou_boss", name="张总", email="boss@e.com")
    assert r.json() == {"status": "ok", "app": "app-b"}
    from even_auth_gov import approval_store
    assert approval_store.get("ou_boss", "finance") is None
    assert approval_store.get("ou_alice", "app-b")["name"] == "Alice"

def test_request_access_duplicate_returns_exists_and_does_not_reset(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as c:
        r1, r2 = _ra(c), _ra(c)
    assert r1.json() == {"status": "ok", "app": "app-b"}
    assert r2.json() == {"status": "exists", "app": "app-b"}

def test_request_access_already_approved_in_casdoor_creates_nothing(monkeypatch, tmp_path):
    """Casdoor 里该用户已在准入组(如迁移进来的老用户)→ 不建 pending、不发卡。以 Casdoor 实时组为准。"""
    monkeypatch.setenv("CASDOOR_ORG", "even-test")
    app = _app(monkeypatch, tmp_path, user_groups=["even-test/approved-app-b"])
    with TestClient(app) as c:
        r = _ra(c)
    assert r.json() == {"status": "approved", "app": "app-b"}
    from even_auth_gov import approval_store
    assert approval_store.get("ou_alice", "app-b") is None

def test_request_access_rejects_disabled_account_with_still_valid_token(monkeypatch, tmp_path):
    """id_token 默认 168h 有效:离职被禁用的人拿着旧 token 也不能再给审批人推卡片。"""
    app = _app(monkeypatch, tmp_path, forbidden=True)
    with TestClient(app) as c:
        r = _ra(c)
    assert r.status_code == 403
    from even_auth_gov import approval_store
    assert approval_store.get("ou_alice", "app-b") is None

def test_request_access_account_lookup_failure_is_503(monkeypatch, tmp_path):
    """确认不了账号状态就不建申请,让接入方稍后重试。"""
    app = _app(monkeypatch, tmp_path, user_ok=False)
    with TestClient(app) as c:
        r = _ra(c)
    assert r.status_code == 503
    from even_auth_gov import approval_store
    assert approval_store.get("ou_alice", "app-b") is None

def test_request_access_pending_for_one_app_does_not_affect_another(monkeypatch, tmp_path):
    """同一用户先对 app-b 申请、再对 app-c 申请 —— 两条 pending 记录独立存在。"""
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as c:
        r1 = _ra(c)
        r2 = _ra(c, token(aud=[CID_C], azp=CID_C))
    assert r1.json() == {"status": "ok", "app": "app-b"}
    assert r2.json() == {"status": "ok", "app": "app-c"}   # 不同 app,不是重复
    from even_auth_gov import approval_store
    assert approval_store.get("ou_alice", "app-b")["status"] == "pending"
    assert approval_store.get("ou_alice", "app-c")["status"] == "pending"

def test_request_access_sends_card_carrying_app_to_its_own_approver(monkeypatch, tmp_path):
    """端到端:card 送达该 app 专属审批人(settings.approver_for),按钮 value 携带 app,姓名取自 token。"""
    monkeypatch.setenv("APPROVER_APP_B", "ou_app_b_boss")
    app = _app_live(monkeypatch, tmp_path)
    sent = []
    async def fake_send_card(receive_id, card, receive_id_type="open_id"):
        sent.append((receive_id, card)); return True
    monkeypatch.setattr("even_auth_gov.feishu.send_card", fake_send_card)
    with TestClient(app) as c:
        r = _ra(c, name="张总")
    assert r.status_code == 200
    assert len(sent) == 1
    receive_id, card = sent[0]
    assert receive_id == "ou_app_b_boss"   # per-app approver, not the global default
    action_buttons = card["elements"][-1]["actions"]
    assert all(btn["value"]["app"] == "app-b" for btn in action_buttons)
    assert all(btn["value"]["sso_open_id"] == "ou_alice" for btn in action_buttons)
    assert "Alice" in card["elements"][0]["content"] and "张总" not in card["elements"][0]["content"]

def test_request_access_duplicate_does_not_resend_card(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVER_APP_B", "ou_app_b_boss")
    app = _app_live(monkeypatch, tmp_path)
    sent = []
    async def fake_send_card(receive_id, card, receive_id_type="open_id"):
        sent.append(receive_id); return True
    monkeypatch.setattr("even_auth_gov.feishu.send_card", fake_send_card)
    with TestClient(app) as c:
        _ra(c); _ra(c)
    assert len(sent) == 1   # 第二次(已 pending)不重发卡片


def test_card_offers_the_apps_casdoor_roles(monkeypatch, tmp_path):
    """审批即赋角:卡片上的角色按钮来自 Casdoor 里该应用的 `<app>-<role>`,网关不另配清单。"""
    monkeypatch.setenv("APPROVER_APP_B", "ou_app_b_boss")
    roles = [{"name": "app-b-viewer", "displayName": "查看者", "isEnabled": True},
             {"name": "app-b-editor", "displayName": "编辑者", "isEnabled": True},
             {"name": "app-c-admin", "displayName": "别的应用", "isEnabled": True}]
    app = _app_live(monkeypatch, tmp_path, roles=roles)
    sent = []
    async def fake_send_card(receive_id, card, receive_id_type="open_id"):
        sent.append(card); return True
    monkeypatch.setattr("even_auth_gov.feishu.send_card", fake_send_card)
    with TestClient(app) as c:
        _ra(c)
    buttons = sent[0]["elements"][-1]["actions"]
    assert [b["text"]["content"] for b in buttons] == ["批准为「编辑者」", "批准为「查看者」", "拒绝"]
    assert [b["value"].get("role") for b in buttons] == ["app-b-editor", "app-b-viewer", None]

def test_card_falls_back_to_plain_approve_when_roles_unreadable(monkeypatch, tmp_path):
    """读不到角色清单不该卡住审批:照常发卡(只开准入、不给角色 = 权限上偏保守),并记已通知。"""
    monkeypatch.setenv("APPROVER_APP_B", "ou_app_b_boss")
    app = _app_live(monkeypatch, tmp_path, roles_ok=False)
    sent = []
    async def fake_send_card(receive_id, card, receive_id_type="open_id"):
        sent.append(card); return True
    monkeypatch.setattr("even_auth_gov.feishu.send_card", fake_send_card)
    with TestClient(app) as c:
        _ra(c)
    buttons = sent[0]["elements"][-1]["actions"]
    assert [b["text"]["content"] for b in buttons] == ["批准 ✅", "拒绝"]
    from even_auth_gov import approval_store
    assert approval_store.get("ou_alice", "app-b")["notified_at"]


# ── #12 回归:卡片没真正送达时不得记 notified_at(否则 reconcile 不补发) ─────

def test_request_access_card_send_failure_leaves_unnotified(monkeypatch, tmp_path):
    """send_card 返回 False(飞书 API 失败,不抛异常)→ pending 仍建,但 notified_at 必须为空,
    让每日对账补发;接口本身仍 200(pending 已落库,补发是网关自己的事)。"""
    monkeypatch.setenv("APPROVER_APP_B", "ou_app_b_boss")
    app = _app_live(monkeypatch, tmp_path)
    async def failing_send_card(receive_id, card, receive_id_type="open_id"): return False
    monkeypatch.setattr("even_auth_gov.feishu.send_card", failing_send_card)
    with TestClient(app) as c:
        r = _ra(c)
    assert r.status_code == 200
    from even_auth_gov import approval_store
    rec = approval_store.get("ou_alice", "app-b")
    assert rec["status"] == "pending"
    assert rec["notified_at"] == ""

def test_request_access_without_approver_configured_leaves_unnotified(monkeypatch, tmp_path):
    """审批人没配(或还是占位 ou_xxx)→ 卡片发不出去,不能假装已通知。"""
    monkeypatch.delenv("APPROVER_APP_B", raising=False)
    monkeypatch.setenv("APPROVER_FEISHU_ID", "ou_xxx")
    app = _app_live(monkeypatch, tmp_path)
    sent = []
    async def fake_send_card(receive_id, card, receive_id_type="open_id"):
        sent.append(receive_id); return True
    monkeypatch.setattr("even_auth_gov.feishu.send_card", fake_send_card)
    with TestClient(app) as c:
        r = _ra(c)
    assert r.status_code == 200
    assert sent == []
    from even_auth_gov import approval_store
    assert approval_store.get("ou_alice", "app-b")["notified_at"] == ""


def test_healthz_reports_ok_and_background_components(monkeypatch, tmp_path):
    """给容器 healthcheck / 运维探活用;TESTING 下 WS 与调度器都不启动。"""
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as c:
        r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "feishu_ws": False, "scheduler": False}


# ── 里程碑 4a:非飞书账号闸门 ──

from datetime import timedelta


def _managed_props(days=30, **over):
    from even_auth_gov import accounts
    p = {"evauth_kind": "partner", "evauth_sponsor": "ou_boss",
         "evauth_expires": (accounts.today() + timedelta(days=days)).isoformat(),
         "evauth_confirmed_at": "2026-11-01T00:00:00+00:00"}
    p.update(over)
    return p


def test_request_access_rejects_unregistered_non_feishu_account(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as c:
        r = _ra(c, token(sub="uuid-stranger"))
    assert r.status_code == 403 and "未登记" in r.json()["error"]
    from even_auth_gov import approval_store
    assert approval_store.get("uuid-stranger", "app-b") is None


def test_request_access_rejects_expired_managed_account(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, users={"x_ext": {"properties": _managed_props(days=-1)}})
    with TestClient(app) as c:
        assert _ra(c, token(sub="x_ext")).status_code == 403


def test_request_access_accepts_eligible_managed_account(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, users={"x_ext": {"properties": _managed_props()}})
    with TestClient(app) as c:
        r = _ra(c, token(sub="x_ext"))
    assert r.status_code == 200 and r.json() == {"status": "ok", "app": "app-b"}
