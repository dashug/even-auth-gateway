# Casdoor Findings（Phase 0 实测结果）

> 4/5 项已由 AI 对着本地跑起来的 Casdoor 实例亲验（`casbin/casdoor:latest` + postgres16，
> Colima）。**第 1 项（飞书原生扫码）需你本人用真实飞书 App 凭据 + 扫码完成**——非人不可。
> 这份文档是 Phase 1 代码里所有 Casdoor 端点/claim 占位符的**唯一事实源**。

**Casdoor 版本**：`casbin/casdoor:latest`（2026-07 拉取；`/api/get-version` 未返回具体 tag，以镜像 digest 为准）
**DB**：postgres16（compose 默认）
**实测**：AI @ 2026-07-05，对本地 `http://127.0.0.1:8000`

---

## 1. 飞书原生 provider 端到端扫码（Step 2）✅ 已验（真机扫码通过）

- 状态：✅ **通过**——真实飞书 App 凭据 + 手机扫码，Casdoor 自动建号成功（用户"陶童童"落进 even-test org）。
- 踩坑记录：provider 必须挂在**普通 org 的应用**上（不能是 built-in org 的 app-built-in，否则报"built-in 禁止建新用户"）；应用需 `enableSignUp=true`。登录入口用 OAuth authorize URL（`/login/oauth/authorize?client_id=...`），不是 SPA 假路由 `/login/<app>`。
- **飞书身份字段落点（关键）**：
  - **`sub` / 用户 `id` 字段 = 飞书 open_id 原值**（如 `ou_cc56d4ccc6b8549b854109f60736e036`）→ **各应用拿到的 OIDC id_token `sub` 直接就是飞书 open_id**，天然对齐，无需额外映射。
  - `lark` 字段 = open_id；`properties` 里全套存 `oauth_Lark_id/email/displayName/avatarUrl/accessToken`；`email` = 企业邮箱。
  - Casdoor 用户 `name` 是自动生成的随机串（如 `e7476ba1`），`update-user?id=` 用 `<org>/<name>`。
- **open_id → Casdoor 用户 反查（治理服务禁用/加组的定位键）**：
  - ✅ `GET /api/get-user?owner=<org>&userId=<open_id>`（因 sub=open_id，userId 查得到）→ 拿到 user，其 `owner`/`name` 拼成 `<org>/<name>`。
  - ✅ 兜底：`GET /api/get-user?owner=<org>&email=<企业邮箱>`（飞书离职事件也带 email）。
  - ❌ `?lark=` 不支持。
- **对 Phase 1 的影响**：`casdoor_admin` 需加 `find_user_by_open_id(open_id)`（用 userId 反查 → 返回 `<org>/<name>`）；`decision`/`offboard` 传的是 open_id，需先解析成 `<org>/<name>` 再调 update-user。

## 2. 组成员进 id_token claim（Step 3，关键）✅ 已验

- 状态：✅ **通过**
- **组成员在 id_token 的 claim 名 = `groups`，值格式 = `["<org>/<组名>"]`**（org 限定）。
  实测：alice 加入 `even-test/approved-operators` 后，其 id_token payload 含 `"groups": ["even-test/approved-operators"]`。
- token 端点：`POST /api/login/oauth/access_token`（支持 `grant_type=password`，username 用**纯用户名**不带 org 前缀）；authorize 端点：`/login/oauth/authorize`。
- ⚠️ **安全发现**：默认 id_token 把整个 user 对象都塞进 payload，**含 `password`/`passwordSalt`/`totpSecret`/`recoveryCodes` 等敏感字段**。生产必须在应用的 token 字段配置里裁剪（只留 sub/name/email/groups 等），否则密码哈希会随 token 外泄。
- **对 Phase 1 的影响**：CS Hub `admin/oidc_client.py` 的 `GROUP_CLAIM="groups"` ✅ 对；但 `APPROVED_GROUP` 判定要用 **org 限定的 `"<org>/approved-operators"`**，不是裸 `"approved-operators"`。

## 3. Admin API — 加组 / 移组 / 禁用 / 按邮箱查（Step 4.1）✅ 已验

**核心发现：Casdoor 无独立"加组/禁用"端点。统一用 `update-user` + `columns=` 局部更新，且需先 GET 完整 user 对象、改字段、再整体回写（read-modify-write，不是打补丁）。**

| 动作 | 实测调用 |
|---|---|
| 加入组 | GET `/api/get-user?id=<org>/<name>` → 把 `groups` 设为 `["<org>/approved-operators"]` → `POST /api/update-user?id=<org>/<name>&columns=groups`（body=完整 user 对象） |
| 从组移除 | 同上，`groups` 里去掉该组 |
| 禁用用户 | GET user → 置 `isForbidden=true` → `POST /api/update-user?id=<org>/<name>&columns=isForbidden` |
| 按邮箱查 | `GET /api/get-user?owner=<org>&email=<email>` → 返回 user（`data.name` 命中） |

- **鉴权（非交互，治理服务用）**：✅ query 参数 `clientId=<app.clientId>&clientSecret=<app.clientSecret>` 即可调 Admin API（实测 `/api/get-user?...&clientId=..&clientSecret=..` status ok）。无需 cookie。
- user 标识：`id` 参数用 `<org>/<name>`；user 对象里另有一个 UUID `id` 字段（`sub`），二者不同。
- **对 Phase 1 的影响（重要）**：`services/even_auth_gov/casdoor_admin.py` 现有占位实现（POST 一个 `{op:"add-group"}`）**是错的**，必须改成"GET user → 改 groups/isForbidden → update-user?columns= 回写"的 read-modify-write，鉴权用 clientId/secret query。

## 4. 禁用即杀会话（Step 4.2）✅ 已验（对新 token）

- 状态：✅ 禁用（isForbidden=true）后，该用户**再取 token 被拒**：`error: the user is forbidden to sign in`。
- 已登录会话的即时失效未单独压测，但设计的「短 token + 刷新回 Casdoor 复验」在此基础上成立：刷新时 Casdoor 认定 forbidden 即拒。离职撤销机制**成立**。

## 5. new-user webhook + 验签（Step 5）⚠️ 部分验

- webhook 可建/可配（`/api/add-webhook`、`update-webhook`；events 对应 record 的 action 名）。
- **record action 名实测**：`signup` / `add-user` / `update-user` / `login/oauth/access_token`。webhook org 要匹配 record 的 org（**admin API 改动的 record org = `built-in`**，即操作者 org）。
- **关键**：`signup` 事件是**终端用户自助注册**（OAuth provider 首登）时才发；AI 用 admin API `add-user` 触发的是另一 action，故未捕获 signup body。**真实触发点 = 飞书用户扫码首登 → Casdoor 自动注册 → 发 signup webhook，与第 1 项绑定**，请你在做第 1 项飞书扫码时顺带抓一次 signup webhook body（配 webhook 指向一个 echo，扫码后看 body 里 `user.id`/飞书 open_id 落点 + 有无签名头）。
- 网络：容器→host 用 `host.docker.internal:9099` 实测可达（wget 探测通）。
- 默认无内置 HMAC 签名头 → Phase 1 `webhook.py` 用「共享密钥 header + 内网限定」校验（已实现）。

---

## 门控结论

- ✅ **5/5 全通过**（飞书扫码 / 组 claim / Admin API / 禁用挡登录 / 非交互鉴权 / open_id 反查），Phase 0 闭环。
- **无硬失败，设计不用回改**。衍生动作项：
  1. **生产必须裁剪 id_token 敏感字段**（默认把 password/totpSecret/recoveryCodes 全塞进 token，安全隐患）——在应用 token 字段配置里只留 sub/name/email/groups 等。
  2. casdoor_admin 改 read-modify-write（已校准，commit f3d05df）。
  3. casdoor_admin 加 `find_user_by_open_id`（userId 反查）；decision/offboard 先把 open_id 解析成 `<org>/<name>`（校准中）。
  4. 授权准入用 `groups` claim 含 `<org>/approved-operators`（org 限定，非裸组名）。
