# 接入说明 —— 应用如何接入 even-auth-gateway

你的应用作为**标准 OIDC 客户端**信任本网关，**不碰任何飞书密钥**。

**最快上手：抄 [`examples/demo_app.py`](../examples/demo_app.py)（整份 70 行，含 4 段核心逻辑，改 6 个环境变量即可跑）。** 下面是它的逐段说明 + 一次真实跑通的实录。

`<CASDOOR>` = 网关 Casdoor 地址（本地 `http://127.0.0.1:8000`，生产如 `https://auth.evenrealities.com`）。

---

## 第一步：在 Casdoor 注册你的应用（管理员，一次性）

1. **Applications → 新建你的应用**，拿到 `clientId` / `clientSecret`，Redirect URLs 填 `https://<你的应用>/callback`（逐字一致）。
2. **Groups → 建准入组** `approved-<你的应用短名>`（例：`approved-demo-app`）。
3.（要角色权限才做）**Roles → 建角色** `<应用短名>-<role>`（例：`demo-app-viewer`），**显示名填审批人看得懂的中文**（如"查看者"）。建好后审批卡片自动按角色出"批准为「查看者」"这类按钮，审批人点哪个就分配哪个角色（该应用只保留这一个，其它 `<应用短名>-*` 角色会被撤掉）；被拒或离职时角色由网关撤回。**一旦建了角色，就不能"只批准不给角色"**：网关会拒绝不带角色的批准，你的应用也要拒绝没有角色的用户（见检查清单）；想要"普通成员"这一档，就建一个对应的角色（如 `demo-app-member`）。不建角色的应用，卡片只有"批准 / 拒绝"。
4. **⚠️ 安全必做：裁 id_token 字段**——Casdoor 默认 `tokenFormat=JWT` 会把整个用户对象塞进 id_token（含 `passwordSalt` 等，实测泄露）。**关键：光设 tokenFields 不够，必须同时把 `tokenFormat` 改成 `JWT-Custom`**，白名单才生效。API 做法：
   ```
   POST <CASDOOR>/api/update-application?id=admin/<你的应用>
   {..., "tokenFormat":"JWT-Custom",
         "tokenFields":["Owner","Name","Id","Type","DisplayName","Avatar","Email","Groups","Roles"]}
   ```
   验证：重取 token，`passwordSalt` 应消失，claim 数从 80+ 降到 ~20，`groups/roles/email/sub` 仍在。
5. **把应用登记到网关**：治理服务的 `APP_CLIENT_IDS` 里加一项 `<应用短名>:<clientId>`（应用短名就是准入组 `approved-<应用短名>` 里那段）。网关靠它把 id_token 的 `aud` 认成你的应用；没登记的应用调 `/request-access` 会收到 403。

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
| `roles` | 角色列表。**元素形状随 Casdoor 版本不同**：较早版本是名字字符串；2026-11 实测 `latest` 是对象 `{"owner","name","displayName",...}` | 两种都兼容（是对象就取 `name`），挑前缀 `<你的应用>-` 的 → 映射到你应用内部权限。**角色能干什么由你定义**，网关只递名字 |

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

## 待审批：把 id_token 交给网关，自动推审批卡片

用户认证成功但不在你的准入组 → 你的 `/callback` 展示"申请已提交，等待审批"页，**同时**把刚换到的 id_token 交给治理服务（demo 的 `_request_access` 就是这几行）：

```
POST <治理服务>/request-access
Content-Type: application/json

{"id_token": "<回调里换到的 id_token>"}
```

- **你的应用不持有任何网关密钥。** 网关用 Casdoor 公钥验签，申请人（`sub` / `displayName` / `email`）和应用（`aud` = 你的 clientId → `APP_CLIENT_IDS`）**全部取自 token**，body 里别的字段一律忽略——所以没法替别人申请，也没法冒充别的应用。
- **只收 id_token**（`tokenType=id-token`）。access_token / refresh_token 会被 401 拒绝；refresh_token 别往外发。
- 重复调用是安全的：已在审批中 / 已处理过的不会重复推卡片。

| 返回 | 含义 |
|---|---|
| `200 {"status":"ok","app":...}` | 已建申请，审批卡片已推给该应用的审批人 |
| `200 {"status":"exists",...}` | 已在审批中或已处理过，不重复推卡 |
| `200 {"status":"approved",...}` | 该用户在 Casdoor 里已有准入组（如迁移来的老用户），让用户重新登录即可 |
| `400` | 缺 `id_token` |
| `401` | token 无效 / 过期 / 签名不对 / 不是 id_token |
| `403` | 你的应用没登记到网关 `APP_CLIENT_IDS`，或该账号已停用（离职的人拿着还没过期的旧 token 也申请不了） |
| `503` | 网关暂时拿不到 Casdoor 公钥或账号状态，稍后重试 |

审批人批准后，申请人会收到飞书通知"已获批准（角色：X），重新登录即可进入"。重新走一次登录，新 token 里就带上准入组和审批时选的角色。

> 第一个接入的应用（`DEFAULT_APP`，默认 cs-hub）由 Casdoor 注册 webhook 自动建申请，可以不调。

## 非飞书账号（外部合作方 / 服务账号）

你的应用**不用改**。外部合作方、没飞书的员工用邮箱 + 密码在 Casdoor 登录，拿到的 token 和员工一样：`sub` 是他们的账号 id（`x_` 开头，不是飞书 open_id），准入看 `groups`、角色看 `roles`，判定逻辑完全相同。这些账号由网关管着负责人和到期日，过期或负责人离职会被停用，和员工离职一样对你透明。

- **服务账号**（脚本、系统集成调你的接口）：用 password grant 换 token——`POST <casdoor>/api/login/oauth/access_token`，`grant_type=password&client_id=...&client_secret=...&username=<服务账号邮箱>&password=...&scope=openid`。需要管理员在 Casdoor 里给你的应用允许 `password` 授权类型。token 里的身份就是这个服务账号，照常判准入和角色。
- 目前 token 里**看不出**是不是外部人员；如果你的应用需要对外部人员隐藏某些数据，先联系网关管理员（`evauth_kind` 进 token 不在当前范围）。

## 接入检查清单

- [ ] Casdoor 注册应用（client_id/secret + 回调 URL 逐字一致）
- [ ] 建 `approved-<应用>` 组（+ 可选角色，显示名填中文）
- [ ] 分了角色的应用：**没有本应用角色的用户一律拒绝**（demo 设 `REQUIRE_ROLE=1`）。网关审批时已强制选角色，这是兜底——迁移或后台手工授权的人可能只有准入组
- [ ] 管理员把 `<应用短名>:<clientId>` 加进网关 `APP_CLIENT_IDS`
- [ ] **裁 id_token 敏感字段**
- [ ] 抄 demo_app.py 的 `/login` + `/callback`，改 4 个环境变量
- [ ] 未准入时把 id_token `POST` 给网关 `/request-access`（demo 设 `GATEWAY_URL` 即开启）
- [ ] 会话短 TTL / 每请求复验（保证离职即时断权）
- [ ]（生产）JWKS 验 id_token 签名（RS256）；回调 HTTPS
- [ ] 用 `sub` 当用户外键，不要假设它以 `ou_` 开头（非飞书账号是 `x_` 开头）
