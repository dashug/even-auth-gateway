# 接入说明 —— 应用如何接入 even-auth-gateway

你的应用作为**标准 OIDC 客户端**信任本网关，**不碰任何飞书密钥**。

**最快上手：抄 [`examples/demo_app.py`](../examples/demo_app.py)（整份 70 行，含 4 段核心逻辑，改 6 个环境变量即可跑）。** 下面是它的逐段说明 + 一次真实跑通的实录。

`<CASDOOR>` = 网关 Casdoor 地址（本地 `http://127.0.0.1:8000`，生产如 `https://auth.evenrealities.com`）。

---

## 第一步：在 Casdoor 注册你的应用（管理员，一次性）

1. **Applications → 新建你的应用**，拿到 `clientId` / `clientSecret`，Redirect URLs 填 `https://<你的应用>/callback`（逐字一致）。
2. **Groups → 建准入组** `approved-<你的应用短名>`（例：`approved-demo-app`）。
3.（要角色权限才做）**Roles → 建角色** `<应用短名>-<role>`（例：`demo-app-viewer`），把用户放进去。
4. **⚠️ 安全必做：裁 id_token 字段**——Casdoor 默认 `tokenFormat=JWT` 会把整个用户对象塞进 id_token（含 `passwordSalt` 等，实测泄露）。**关键：光设 tokenFields 不够，必须同时把 `tokenFormat` 改成 `JWT-Custom`**，白名单才生效。API 做法：
   ```
   POST <CASDOOR>/api/update-application?id=admin/<你的应用>
   {..., "tokenFormat":"JWT-Custom",
         "tokenFields":["Owner","Name","Id","Type","DisplayName","Avatar","Email","Groups","Roles"]}
   ```
   验证：重取 token，`passwordSalt` 应消失，claim 数从 80+ 降到 ~20，`groups/roles/email/sub` 仍在。

> 注册后你只需 4 个环境变量：`CASDOOR_ENDPOINT`、`CASDOOR_CLIENT_ID`、`CASDOOR_CLIENT_SECRET`、`CASDOOR_ORG`（飞书用户所在 org）。

## 第二步：实现两个路由（抄 demo_app.py）

### ① `GET /login` —— 重定向到 Casdoor（带签名 state 防 CSRF）
```python
state = f"{nonce}.{hmac_sign(nonce)}"
redirect_to = f"{CASDOOR}/login/oauth/authorize?client_id={CLIENT_ID}&response_type=code" \
              f"&redirect_uri={REDIRECT_URI}&scope=openid profile email&state={state}"
# 同时把 state 写进 httponly cookie
```

### ② `GET /callback` —— 验 state → 换 token → 查准入组
```python
# a. 验 state == cookie 里的 state(hmac.compare_digest)
# b. 换 token:
POST {CASDOOR}/api/login/oauth/access_token
     grant_type=authorization_code & client_id & client_secret & code & redirect_uri
  → {"id_token": "<jwt>", ...}
# c. 解 id_token 的 payload,查你的准入组:
approved = f"{ORG}/approved-{APP}" in claims["groups"]
# d. approved → 发你自己的会话进后台;否则 → 展示"待审批"页
```

## 跑通实录（本地真实抓取，非示意）

**① 打开 demo 的登录入口 → 真实跳转（含真 client_id、回调、签名 state）：**
```
$ curl -D - http://127.0.0.1:9100/login
location: http://127.0.0.1:8000/login/oauth/authorize?client_id=001f662a9b2bf1d5511e
          &response_type=code&redirect_uri=http%3A%2F%2F127.0.0.1%3A9100%2Fcallback
          &scope=openid+profile+email&state=<签名串>
set-cookie: oidc_state=<签名串>; HttpOnly; Max-Age=300; SameSite=lax
```

**② 回调换到 token 后，你的应用实际拿到的 claim（真实解出）：**
```json
{
  "sub":    "33b18096-21a2-48fe-9253-1667cf677324",   // 用户唯一键(飞书用户此处=飞书 open_id)
  "name":   "alice", "displayName": "Alice2",
  "email":  "alice@e.com",
  "groups": ["even-test/approved-demo-app", "even-test/approved-operators"],
  "roles":  ["cshub-operator", "demo-app-viewer"]
}
```

**③ 准入判定（demo 的逻辑跑在真实 claim 上）：**
```
'even-test/approved-demo-app' in groups  →  ✅ 放行
```
> 这条 claim 同时印证了**每应用独立**：alice 对 `demo-app` 和 `cshub` 各有各的准入组（`approved-demo-app` / `approved-operators`）和角色（`demo-app-viewer` / `cshub-operator`）。你的应用只认自己那一份。

## Claim 契约（你的应用怎么读）

| claim | 含义 | 你怎么用 |
|---|---|---|
| `sub` | 用户唯一键（飞书用户 = 飞书 open_id） | 作为你库里的用户外键，**别用 `name`（随机串）** |
| `groups` | `["<org>/<组名>"]` | 查 `"<org>/approved-<你的应用>"` 在不在 → 准入 |
| `roles` | `["<角色名>", ...]`（裁剪后为名字列表） | 挑前缀 `<你的应用>-` 的 → 映射到你应用内部权限。**角色能干什么由你定义**，网关只递名字 |

## OIDC 端点（据网关 discovery 实测，一次拉齐）

`GET <CASDOOR>/.well-known/openid-configuration` 返回全部；常用：

| 用途 | 端点 |
|---|---|
| 授权（浏览器重定向） | `<CASDOOR>/login/oauth/authorize` |
| 换 token（后台调用） | `<CASDOOR>/api/login/oauth/access_token` |
| 验签公钥（JWKS） | `<CASDOOR>/.well-known/jwks` |
| 用户信息 | `<CASDOOR>/api/userinfo` |

## 会话与离职撤销

- 你签发的会话 **token 用短 TTL**（建议 5 分钟）+ 刷新回 Casdoor 复验，或每请求校验。
- 离职时治理服务在 Casdoor 把用户置 `isForbidden=true` → 其会话立即失效 + 你应用下次刷新被拒 → **数分钟内自动登出**。你**无需实现 back-channel logout**。

## 待审批的通知（现状 vs 规划，说清楚不含糊）

- **现在就能用**：用户认证成功但不在你的准入组 → 你的 `/callback` 展示"申请已提交，等待审批"页（demo 里就这么做）。审批动作 = 管理员/治理服务把该用户加进 `approved-<你的应用>` 组（可用治理服务的 `casdoor_admin.add_to_group`）。
- **规划中（尚未实现）**：`POST <网关>/request-access {app, open_id, ...}` —— 应用主动上报待审批、自动推飞书审批卡片给该应用的审批人。见 [`docs/design.md`](design.md) 的「多应用授权模型」。**当前 demo 不依赖它**（它还没建），先靠管理员加组批准。

## 接入检查清单

- [ ] Casdoor 注册应用（client_id/secret + 回调 URL 逐字一致）
- [ ] 建 `approved-<应用>` 组（+ 可选角色）
- [ ] **裁 id_token 敏感字段**
- [ ] 抄 demo_app.py 的 `/login` + `/callback`，改 4 个环境变量
- [ ] 会话短 TTL / 每请求复验（保证离职即时断权）
- [ ]（生产）JWKS 验 id_token 签名（RS256）；回调 HTTPS
