"""最小接入示例 —— 一个应用如何接入 even-auth-gateway(Casdoor OIDC)。
整份就这些代码。跑法在文件末尾。接入你自己的应用照抄这四段即可。

依赖:fastapi, httpx  (pip install fastapi uvicorn httpx)
"""
from __future__ import annotations
import base64, hashlib, hmac, json, os, secrets, time
from urllib.parse import urlencode
import httpx
from fastapi import FastAPI, Request
from starlette.responses import RedirectResponse, HTMLResponse

# ── 接入你只改这 6 个环境变量 ─────────────────────────────
CASDOOR     = os.environ["CASDOOR_ENDPOINT"]        # 网关 Casdoor 地址
CLIENT_ID   = os.environ["CASDOOR_CLIENT_ID"]        # 在 Casdoor 注册应用拿到
CLIENT_SECRET = os.environ["CASDOOR_CLIENT_SECRET"]
ORG         = os.environ["CASDOOR_ORG"]              # 飞书用户所在 org
APP         = os.environ.get("APP_NAME", "demo-app") # 你的应用短名
PUBLIC_URL  = os.environ.get("PUBLIC_URL", "http://127.0.0.1:9100")
SECRET      = os.environ.get("SESSION_SECRET", "demo-secret")

APPROVED_GROUP = f"{ORG}/approved-{APP}"             # 你的准入组
REDIRECT_URI   = f"{PUBLIC_URL}/callback"

app = FastAPI()

# ── ① 发起登录:重定向到 Casdoor,带签名 state ───────────────
def _sign(v: str) -> str:
    return hmac.new(SECRET.encode(), v.encode(), hashlib.sha256).hexdigest()

@app.get("/login")
async def login():
    nonce = secrets.token_hex(8)
    state = f"{nonce}.{_sign(nonce)}"
    url = f"{CASDOOR}/login/oauth/authorize?" + urlencode({
        "client_id": CLIENT_ID, "response_type": "code", "redirect_uri": REDIRECT_URI,
        "scope": "openid profile email", "state": state})
    r = RedirectResponse(url)
    r.set_cookie("oidc_state", state, httponly=True, max_age=300)
    return r

# ── ② 回调:验 state → 换 token → 解 claim → 查准入组 ─────────
@app.get("/callback")
async def callback(request: Request):
    state = request.query_params.get("state", "")
    if state != request.cookies.get("oidc_state", "") or "." not in state \
       or not hmac.compare_digest(_sign(state.split(".")[0]), state.split(".")[1]):
        return HTMLResponse("state 校验失败", 400)
    async with httpx.AsyncClient() as c:
        resp = await c.post(f"{CASDOOR}/api/login/oauth/access_token", data={
            "grant_type": "authorization_code", "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET, "code": request.query_params.get("code", ""),
            "redirect_uri": REDIRECT_URI})
    tok = resp.json()
    if "id_token" not in tok:
        return HTMLResponse(f"换 token 失败: {tok}", 400)
    claims = _decode(tok["id_token"])
    # ── ③ 准入判定 ──
    if APPROVED_GROUP not in (claims.get("groups") or []):
        return HTMLResponse(f"<h3>申请已提交,等待管理员批准</h3>用户: {claims.get('displayName')}<br>"
                            f"缺组: {APPROVED_GROUP}<br>当前 groups: {claims.get('groups')}")
    # 已准入 → 发你自己的会话(这里简化成直接展示身份)
    roles = [r["name"] for r in (claims.get("roles") or []) if r.get("name","").startswith(f"{APP}-")]
    return HTMLResponse(f"<h3>✅ 登录成功</h3>open_id(sub): {claims.get('sub')}<br>"
                        f"姓名: {claims.get('displayName')}<br>邮箱: {claims.get('email')}<br>"
                        f"本应用角色: {roles or '(默认)'}")

# ── ④ 解 id_token(取 payload;生产应加 JWKS 验签) ────────────
def _decode(id_token: str) -> dict:
    p = id_token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))

@app.get("/")
async def home():
    return HTMLResponse(f"""
<!doctype html><meta charset=utf-8>
<title>Demo App · 接入网关示例</title>
<div style="min-height:90vh;display:flex;flex-direction:column;align-items:center;justify-content:center;
     font-family:-apple-system,'PingFang SC',sans-serif;background:#f5f6f8">
  <div style="background:#fff;padding:44px 40px;border-radius:16px;box-shadow:0 8px 30px rgba(0,0,0,.08);
       text-align:center;max-width:360px">
    <div style="font-size:20px;font-weight:700;margin-bottom:6px">Demo App</div>
    <div style="color:#888;font-size:13px;margin-bottom:28px">接入 even-auth-gateway 的最小示例（应用: {APP}）</div>
    <a href="/login" style="display:block;background:#3370ff;color:#fff;text-decoration:none;
       padding:13px 0;border-radius:10px;font-size:15px;font-weight:600">⚡ 用飞书登录</a>
    <div style="color:#aaa;font-size:12px;margin-top:16px">点击 → 跳网关(Casdoor) → 飞书扫码/免登 → 回本应用</div>
  </div>
</div>""")

# 跑法:
#   export CASDOOR_ENDPOINT=http://127.0.0.1:8000 CASDOOR_CLIENT_ID=... CASDOOR_CLIENT_SECRET=... CASDOOR_ORG=even-test
#   uvicorn demo_app:app --port 9100
# 然后开 http://127.0.0.1:9100/ 点登录。
