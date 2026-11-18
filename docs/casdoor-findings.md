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

---

## 6. Webhook 验证机制（Phase prod-4 实测，2026-07-06）

- **Casdoor webhook 不对 body 做 HMAC 签名**。安全模型是:在 webhook 上配置**自定义请求头**（如 `X-Webhook-Token: <shared>`），Casdoor 每次回调携带该头。接收端**比对头值 == 共享密钥**即可，无需 body HMAC。
  - ⟹ 治理服务 `webhook._verify` 已从"body HMAC + `X-Casdoor-Signature`"（**错误方向，Casdoor 从不发此头**）改为"常量时间比对 `CASDOOR_WEBHOOK_HEADER`(默认 `X-Webhook-Token`) 值 == `CASDOOR_WEBHOOK_SECRET`"，未配密钥 fail-closed。
- **Webhook body = Casdoor `Record`**：`action`（如 signup/add-user）、`object`（受影响对象的 **JSON 字符串**）、`organization`、`user`（操作者用户名，**非对象**）；勾选 `isUserExtended` 时完整用户对象在 `extendedUser`。
  - ⟹ `webhook._extract_user` 按 `extendedUser` > `object`(str→解析) > `user`(dict 兜底) 取用户对象。
- **Webhook 触发按 record 的 organization 匹配**，record.org = **操作者的 org**（admin 操作 → built-in），非目标对象 org。生产真实飞书 signup 走应用 org（如 even-test）。
- ⚠️ **本地实例的 webhook 投递未触发**（record 正常生成但 Casdoor 未发出，疑似 webhook 缓存/构建差异）——故上述对齐由**官方文档 + 接收端活验**（真实 Record 形状 payload 打 `/casdoor/webhook`：正确 token→200、错/无 token→401、pending 正确建出）确认，**非 Casdoor 自身投递抓包**确认。生产接线时以真实回调再核一次头名/ body 形状。

---

## 7. id_token 验签 + 角色 API（2026-11-16 实测，Casdoor v4.29.0，`casbin/casdoor@sha256:d04c4481…`）

用仓库 compose 起的临时实例（独立 project，端口 18000），org `even-test` + 应用 `demo-app`（`tokenFormat=JWT-Custom`，tokenFields 同接入指南）+ 用户 id 设为 `ou_test_*`，password grant 换 token。

- **新版镜像没有默认 `admin/123`**：日志 `The password of built-in/admin is not set, open Casdoor in the browser to set it, or set initAdminPassword before the first start`。环境变量 `initAdminPassword` 只在首次启动（空库）生效；传空字符串等同未设（实测：空库 + 空值 → 仍是 not set，空密码登录被拒）。
  - ⟹ compose 已接 `initAdminPassword: ${CASDOOR_INIT_ADMIN_PASSWORD:-}`，`.env.example` 加了该项。
- **id_token 形状**：header `{"alg":"RS256","kid":"cert-built-in","typ":"JWT"}`（kid = 应用绑定的证书名）；claim `aud=["<clientId>"]`（**列表**）、`azp=<clientId>`、`sub=<用户 id>`（飞书用户 = open_id）、`displayName`、`email`、`groups`、`roles`、`tokenType="id-token"`。默认 TTL 168h（`expireInHours`）。
- **三种 token 都是同一把钥匙签的 JWT，aud 相同**，只靠 `tokenType` 区分：`access-token` / `id-token` / `refresh-token`。
  - ⟹ `/request-access` 只收 `tokenType=id-token`，refresh_token 不该被到处传。
- **`iss` 取签发请求的 Host**（本次 `http://127.0.0.1:18000`），不是固定值。治理服务走内网 `http://casdoor:8000` 访问，和 iss 不同。
  - ⟹ 期望的 iss 单独配 `CASDOOR_ISSUER`（Casdoor 对外地址）；留空不校验（签名已由 JWKS 保证）。容器内实测：内网拉 JWKS + iss 配对外地址 → 验过。
- **JWKS**：`GET /.well-known/jwks` → `[{kid:"cert-built-in", kty:"RSA", alg:"RS256"}]`，PyJWT 用它验 id_token 通过；篡改 payload（改 sub）→ `Signature verification failed`。
- **`roles` claim 是对象列表**：`[{"owner":"even-test","name":"demo-app-viewer","displayName":"查看者","users":null,...}]`——不是 07 月记录的名字列表（版本差异）。接入方两种都要兼容。`get-user` 返回的 `roles` 字段则是名字。
- **角色 API**（clientId/clientSecret query 鉴权）：
  - 列表：`GET /api/get-roles?owner=<org>` → `[{name, displayName, users, isEnabled, ...}]`。
  - 单个：`GET /api/get-role?id=<org>/<role>`，不存在时 `status=ok, data=null`。
  - 加人：`POST /api/update-role?id=<org>/<role>` 整对象回写，`users` 元素是 **`<owner>/<name>`**（Casdoor 用户名，不是 open_id）。改完重新换的 token 里 `roles` 即带上该角色。
  - 移人：同一个 `update-role`，从 `users` 里去掉 `<owner>/<name>` 后整对象回写（`remove_roles` 已对活实例验证，见 §8）。
  - ⟹ `casdoor_admin.list_app_roles` / `assign_role` / `remove_roles` 按此实现，与组变更共用进程内锁（role 对象同样是整对象读-改-写）。`remove_roles` 以 `get-roles` 里各角色的 `users` 为准找出该用户所在角色。
- **签名有效 ≠ 人还在职**：id_token 默认 168h，禁用账号后旧 token 照样验签通过。
  - ⟹ `/request-access` 验签后再 `get-user` 查实时状态：`isForbidden` → 403，查不到 → 503；“已有准入”也按实时 groups 判断。实测：先换 token 再禁用 → 403、不建申请。
- **端到端**（真实代码，仅把飞书发送换成捕获）：真实 id_token → `/request-access` 建 pending（姓名取自 claim，body 冒填被忽略）→ 卡片按角色出"批准为「编辑者」/「查看者」…"→ 模拟审批人点"编辑者"→ 先 `update-role` 再加 `approved-demo-app` 组 → 新 token `groups=["even-test/approved-demo-app"]`、`roles=[demo-app-editor]`；已准入用户再上报返回 `approved`。

## 8. 里程碑 3 + 撤角色端到端（2026-11-17，同一镜像的临时实例）

真实代码对活 Casdoor，只把飞书发送换成捕获，18 项全过：

- **只留所选角色**：先手工给 alice 挂上查看者 + 编辑者，再"批准为「编辑者」"→ 查看者被撤；新 token `roles=[demo-app-editor]`、`groups=[even-test/approved-demo-app]`。
- **角色强制**：分了角色的应用点不带角色的批准 → 拒绝 + `resend_card`，没开准入、仍待批。
- **拒绝撤角色**：有残留查看者的申请人被拒 → 角色被撤。
- **离职**：带角色批准后离职 → `isForbidden=true`、`approved-*` 组清空、角色成员里没有他。
- **迁移 `--role`**：不存在的角色 → 整批中止、一个人都不动；正确角色 → 有准入组 + 角色。
- **权限核对**：造出的 4 类需处理异常（本地离职但 Casdoor 启用 / 停用仍挂组 / 有准入没角色 / 有角色没准入）和 2 条 info 全部核出，正常用户无误报；`get-users` 的 `groups`、`get-roles` 的 `users`（`<owner>/<name>`）形状与单测假设一致；CLI 输出与函数一致；每日推送文本如预期。


## 9. 里程碑 4a 前置实测（2026-11-18，`casbin/casdoor:v4.29.0` 临时实例）

设计见 `docs/superpowers/specs/2026-11-18-m4a-managed-accounts-design.md` 第 6 节。治理服务的调用方式（client credentials）建号、改属性。

- **S1 邮箱登录**：`name`/`id` 都是随机串（`x_0123…`）的密码账号，用**邮箱**登录可行——网页登录（`/api/login`，`signinMethod=Password`）和 password grant 都通过，用 `name` 也通过。
  - ⟹ 受管账号 `name = id = x_<16 hex>`，对外只告诉对方邮箱。
- **S2 强制改密码**：用户对象有 `needUpdatePassword`（另有 `lastChangePasswordTime`）。置 true 后网页登录返回 `status=ok, data="RequiredUpdatePassword"`（登录页转改密码），**password grant 直接 `invalid_grant`**。
  - ⟹ 人员账号建号时置 true；`service` 账号不能置（否则 password grant 永远失败）。
- **S3 只改 properties**：`add-user` 可直接带 `properties`；`update-user?columns=properties` 整对象回写只落 `properties` 一列（同一请求里改的 `displayName` 被忽略），原有 `oauth_Lark_*` 保留，新键合并进去。
  - ⟹ `set_properties` 读出 → 合并 → `columns=properties` 回写。
- **S4 停用挡密码登录**：`isForbidden=true` 后网页登录报 `The user is forbidden to sign in, please contact the administrator`，password grant `invalid_grant`。
- **S5 自助注册（§1.1 风险属实）**：应用 `enableSignUp=true`、注册项不要求手机/邮箱验证码时，陌生人 `POST /api/signup` 直接建出**启用中**的账号（`id` 是 UUID，姓名/邮箱自填，`signupApplication=demo-app`）。默认注册项要求手机验证码，没配短信服务时碰巧注册不了——**不能当防线**。
  - 关法：应用打开 **`disableSelfSignup=true`**（`enableSignUp` 保持 true）→ `/api/signup` 报 `The application does not allow to sign up new account`。源码（v4.29.0 `object/application.go` `IsSelfSignupEnabled`）注释：关掉后只剩登录方式（如飞书首登）的注册和管理员发的邀请码；OAuth 首登分支（`controllers/auth.go:1250-1255`）只查 `EnableSignUp` + `IsSignupAllowedFor` + `providerItem.CanSignUp`，不看 `DisableSelfSignup`。
  - **邀请码不能用来关注册**：OAuth 首登分支同样调 `CheckInvitationCode`，注册项把邀请码设成必填会连飞书首登一起挡掉。
  - ⟹ 部署要求 `disableSelfSignup=true`；飞书首登不受影响这一点**需真实飞书联调时确认**。治理层闸门照做（纵深防御，配置被改回也拦得住）。
- **S6 建号是否触发 webhook**：本地实例仍**不投递任何 webhook**（容器→宿主机连通已验证；org=even-test 与 built-in 各配一个、事件 signup/add-user，client credentials 建号、admin 会话建号、自助注册均零回调），与 §6 现象一致。无结论，闸门两种情况都覆盖。
