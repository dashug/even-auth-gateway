# 接入说明 — 应用如何接入 even-auth-gateway

面向要接入统一飞书登录网关的**应用开发者**。你的应用只需作为**标准 OIDC 客户端**信任本网关，不碰任何飞书密钥。

> 所有端点/claim 形状均已对活 Casdoor 实测（见 `docs/casdoor-findings.md`）。文档里 `<CASDOOR>` 指网关的 Casdoor 地址（如 `https://auth.evenrealities.com`），`<APP>` 指你的应用短名（如 `cs-hub`）。

## 1. 概览：你要做什么

```
用户浏览器 ──(1)重定向──▶ Casdoor 授权页 ──飞书扫码/免登──▶
   ▲                                                    │
   │(4)带 cs_session 进你的后台                          │(2)回调带 code
   │                                                    ▼
你的应用 ◀──(3)后台用 code 换 id_token(含 sub/groups/roles)── Casdoor
```

你实现两个路由 + 一段 claim 判定即可：
- `GET /auth/login`：重定向到 Casdoor 授权端点（带 state 防 CSRF）。
- `GET /auth/callback`：验 state → 用 code 换 id_token → 解 claim → **查你应用的准入组/角色** → 放行或"待审批"。

## 2. 接入前置：在 Casdoor 注册你的应用（一次性，管理员操作）

1. Casdoor 后台 → **Applications** → 新建 `<APP>`：
   - 记下 **Client ID / Client Secret**。
   - **Redirect URLs** 填你的回调：`https://<your-app>/auth/callback`（逐字一致，含协议/端口/路径）。
   - 勾选返回 `openid profile email` scope，并确保 token 里含 groups/roles。
2. **Groups** → 新建你的准入组 `approved-<APP>`（如 `approved-cs-hub`）。
3.（可选，要角色权限时）**Roles** → 新建 `<APP>-<role>`（如 `cs-hub-operator`、`cs-hub-admin`）。
4. **⚠️ 安全必做**：在应用的 token 字段配置里**裁剪 id_token**，只保留 `sub/name/email/groups/roles`——Casdoor 默认会把整个用户对象（含 password/totpSecret 等）塞进 id_token。

## 3. OIDC 端点清单（据网关 discovery 实测）

拉一次 discovery 即可全部拿到：`GET <CASDOOR>/.well-known/openid-configuration`

| 用途 | 端点 |
|---|---|
| 授权（浏览器重定向） | `GET <CASDOOR>/login/oauth/authorize` |
| 换 token（后台调用） | `POST <CASDOOR>/api/login/oauth/access_token` |
| 用户信息 | `GET <CASDOOR>/api/userinfo` |
| 验签公钥（JWKS） | `GET <CASDOOR>/.well-known/jwks` |
| 登出 | `<CASDOOR>/api/logout` |
| id_token 签名算法 | RS256/RS512/ES256 等（可用 JWKS 验签） |

## 4. 接入四步

**① 发起登录** — 重定向浏览器到：
```
<CASDOOR>/login/oauth/authorize?client_id=<CLIENT_ID>&response_type=code
  &redirect_uri=<你的回调 urlencoded>&scope=openid%20profile%20email&state=<签名的防CSRF随机串>
```
同时把 state 写进一个 httponly cookie（回调时比对，防登录 CSRF）。

**② 回调换 token** — 在 `/auth/callback`：先校验 `query.state == cookie.state`（用 `hmac.compare_digest`），再：
```
POST <CASDOOR>/api/login/oauth/access_token
Content-Type: application/x-www-form-urlencoded
grant_type=authorization_code&client_id=<CLIENT_ID>&client_secret=<CLIENT_SECRET>
  &code=<code>&redirect_uri=<你的回调>
→ 200 {"id_token":"<jwt>", "access_token":"...", ...}
```

**③ 解 id_token + 准入判定**（见 §5 claim 契约）。

**④ 放行 / 待审批**：在你应用的准入组里 → 发你自己的会话 cookie 放进后台；不在 → 展示"待审批"页并触发审批（§6）。

## 5. Claim 契约（实测样例）

id_token payload（裁剪后）关键字段：

```json
{
  "sub": "ou_cc56d4ccc6b8549b854109f60736e036",   // = 飞书 open_id,你应用的唯一用户键
  "name": "e7476ba1", "displayName": "陶童童",
  "email": "tongtong.tao@evenrealities.com",
  "groups": ["even-test/approved-cs-hub"],          // 准入:值是 <org>/<组名>
  "roles":  [{"owner":"even-test","name":"cs-hub-operator"}]  // 角色:对象列表
}
```

- **准入判定**：`"<org>/approved-<APP>" in claims["groups"]` → 有则放行，无则待审批。
- **角色判定**：从 `claims["roles"]` 里挑 `name` 前缀是 `<APP>-` 的，映射到你应用内部权限。**角色的含义（能干什么）由你应用定义**，网关只递角色名。
- **用户唯一键**：用 `sub`（飞书 open_id），不要用 `name`（随机串）。

## 6. 待审批与触发审批

用户认证成功但不在你的准入组 → 展示"申请已提交，等待审批"页，并**上报待审批请求**给治理服务（否则审批人不知道有人在等）：

```
POST <GATEWAY_GOV>/request-access
Content-Type: application/json
{"app": "<APP>", "open_id": "<sub>", "name": "<displayName>", "email": "<email>"}
```

治理服务据此建 (open_id, app) 待审批记录 + 推飞书审批卡片给**该应用的审批人**。审批人点【批准】→ 治理服务把用户加入 `approved-<APP>` 组（或分配角色）→ 用户重登即放行。

> 注：`/request-access` 是"每应用审批"设计的接口（见 `docs/design.md`）。单应用试点期用的是 Casdoor `signup` webhook 全局触发；多应用起改用本接口，因为 signup 只在用户**首次注册**发一次，驱动不了"对第 N 个应用的申请"。

## 7. 会话与撤销

- 你应用签发的会话 token **用短 TTL**（建议 5 分钟 access + 刷新回 Casdoor 复验），或每请求校验。
- 离职/禁用时治理服务把用户在 Casdoor 置 `isForbidden=true` → 其 Casdoor 会话立即失效 + 你应用下次刷新回 Casdoor 被拒 → **数分钟内全线登出**。你**无需实现 back-channel logout**（除非要秒级）。

## 8. 治理服务 HTTP 接口（网关侧，你会用到/需知道的）

| 端点 | 方向 | 用途 |
|---|---|---|
| `POST /request-access` | 你的应用 → 网关 | 上报"用户对本应用待审批"，触发审批卡片（多应用审批） |
| `POST /casdoor/webhook` | Casdoor → 网关 | Casdoor 事件（新用户等）通知，网关内部用，你不用调 |
| `POST /feishu/card` | 飞书 → 网关 | 审批卡片按钮回调，网关内部用，你不用调 |

你的应用**只需对接 `/request-access`**（待审批时上报）+ 标准 OIDC 端点（§3）。审批卡片的收发是网关的事，与你应用解耦。

## 9. 参考实现

`even-cs-hub` 的 `admin/oidc_client.py` + `main.py` 的 `/auth/oidc/login`、`/auth/oidc/callback` 就是一份可直接抄的最小 OIDC 客户端（state 签名、code 换 token、`is_approved` 组 claim 判定）。核心不到 100 行。

## 10. 接入检查清单

- [ ] Casdoor 注册应用，拿到 client_id/secret，配回调 URL（逐字一致）
- [ ] 建 `approved-<APP>` 组（+ 可选角色）
- [ ] **裁剪 id_token 敏感字段**（token 配置）
- [ ] 实现 `/auth/login`（重定向 + state cookie）
- [ ] 实现 `/auth/callback`（验 state → 换 token → 查准入组 → 放行/待审批）
- [ ] 待审批时 `POST /request-access` 上报
- [ ] 会话短 TTL / 每请求复验（保证离职即时断权）
- [ ]（生产）用 JWKS 验 id_token 签名；回调走 HTTPS；`/request-access` 内网限定 + 共享密钥

## 11. 安全须知

- **验签**：生产用 `<CASDOOR>/.well-known/jwks` 验 id_token 签名（RS256）。试点期若 token 走后台直取（不经浏览器）可暂缓，但生产必做。
- **裁 token**：务必裁掉 id_token 里的敏感字段（见 §2.4）。
- **回调逐字匹配 + HTTPS**：redirect_uri 生产必须公网 HTTPS 且与 Casdoor 注册值逐字一致。
