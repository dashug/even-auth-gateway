# 设计：统一飞书登录网关 / 身份中心（子项目一 — 地基 + CS Hub 试点）

日期：2026-07-04
状态：已与需求方对齐，待实施
前置：选型调研（buy-on-Casdoor，见对话记录）+ 已完成的 CS Hub 飞书 SSO（`docs/superpowers/specs/2026-07-03-feishu-sso-design.md`）

## 背景与目标

CS Hub 已实现一套飞书 SSO（飞书 OAuth + 入职审批卡片 + 离职三层闭环），但这套逻辑焊死在单个应用里。公司有 6-15 个内部应用（千人级）都要接飞书登录，且将来要接入非飞书身份源。目标：把"飞书扫码登录 + 入职审批 + 离职自动停用"收敛成一份、供所有应用共享的**统一登录网关 / 身份中心（IdP）**。

**本 spec 只覆盖子项目一**：立起网关地基 + 把 CS Hub 作为首个 OIDC 客户端接入验证。后续每个应用接入各自成 spec。

### 已对齐的决策

1. **选型 = buy-on-Casdoor**：Casdoor 承接协议底座（飞书登录 + OIDC/SAML/会话/多应用/管理后台），保留自建飞书治理逻辑。依据：调研已验证 Casdoor 是五个开源 IdP 中唯一原生支持飞书的（内置 Lark provider `idp/lark.go` + Lark syncer，零自定义 OAuth 代码），协议全家桶（OAuth/OIDC/SAML/CAS/LDAP/SCIM/WebAuthn）在 Apache-2.0 仓库内、无功能墙。
2. **范围 = 子项目一（地基 + CS Hub 试点）**。
3. **身份真理源 = Casdoor 权威**：Casdoor 持有全部用户；CS Hub 的 `sso_store` 用户主库退役，CS Hub 变纯 OIDC 客户端；治理服务降级成"控制器"（收 webhook、推卡片、调 Casdoor API）。
4. **审批门 = 认证/授权分离 + 组门控**：任何飞书员工都能"认证"（登进 Casdoor），但"授权访问应用套件"取决于是否在 Casdoor 的 `approved-operators` 组。新用户=认证成功但不在组=应用展示"待审批"页拒绝。审批通过=治理服务调 Casdoor API 加组。此模型无首登竞态（准入挂在组 claim，不是"建号那一刻"），并天然满足认证/授权分离。依据：调研已验证 Casdoor webhook 是异步 fire-and-forget、拦不住首登，故不能靠 webhook 同步拦截。
5. **离职撤销 = 短 token + 中心拦截**：各应用短 TTL access token（建议 5 分钟）+ 刷新回 Casdoor 校验；禁用即失效。各应用零额外集成，不做 back-channel logout。
6. **Casdoor 后端数据库 = PostgreSQL（默认）**：若已有 MySQL 实例则改用 MySQL（Casdoor 两者皆支持，仅连接串差异）。Casdoor 不使用 SQLite。

## 架构拓扑

```
                          阿里云内网 · Docker · nginx
  飞书 ──OAuth/syncer──▶ ┌──────────────────────────────┐
                        │  Casdoor  (auth.evenrealities.com)  │  ← 身份真理源
                        │  · 飞书原生 provider(扫码登录)      │    OIDC/SAML 签发方
                        │  · 用户 + approved-operators 组     │    PostgreSQL 容器
                        └──────────────────────────────┘
                     new-user webhook │        ▲ user/group API(加组/禁用)
                                      ▼        │
                        ┌──────────────────────────────┐
                        │ 治理服务 even-auth-gov (FastAPI)   │  ← CS Hub 现有逻辑降级复用
                        │ · webhook 接收→推飞书审批卡片      │    审批状态库(瘦 sso_store)
                        │ · 卡片回调→调 Casdoor 加组         │    飞书 WS 事件 + 每日对账
                        │ · 离职→调 Casdoor 禁用            │
                        └──────────────────────────────┘
                                      │ (复用 channels/feishu.py, feishu_ws.py)
   CS Hub / 应用2 / 应用3… ──纯 OIDC 客户端──▶ 信任 Casdoor(校验 id_token + approved 组 claim)
```

三个部署单元：Casdoor（协议底座）、治理服务（自建逻辑新家）、各应用（纯 OIDC 客户端）。

## 组件

### Casdoor（部署 + 配置，不写代码）
- Docker 部署，nginx 反代到 `auth.evenrealities.com`，PostgreSQL 容器做后端。
- 配飞书原生 provider（Category=OAuth, Type=Lark；Client ID←App ID, Client secret←App Secret；回调 `<casdoor-domain>/callback`）。
- 建组 `approved-operators`（应用套件准入组）。
- 建 OIDC 应用 `cs-hub`（第一个客户端），配 redirect URI、组 claim 映射到 id_token。
- 飞书应用需补权限：`contact:user.base:readonly` + `contact:department.base:readonly`（syncer 用）。

### 治理服务 `even-auth-gov`（新服务，从 CS Hub 现有模块抽出）
- **`POST /casdoor/webhook`**：验签 Casdoor 事件（new-user/login）；未审批用户 → 建 pending 记录 → 复用 `build_sso_approval_card` 推飞书审批卡片给审批人。
- **飞书卡片回调**：复用 `handle_card_action` / `_handle_sso_decision`；动作从"改本地态"改为**调 Casdoor group API 把用户加入 `approved-operators`**（批准）或保持不在组/禁用（拒绝）。
- **离职引擎**：复用现有 `feishu_ws` 的 `contact.user.deleted_v3`/`updated_v3` 双事件 handler + 每日对账 job；动作改为**调 Casdoor user API 置 `isForbidden=true` + 移出组**。
- **瘦身版 `sso_store`**：只存审批工作流状态（open_id → pending/approved/denied + 审批人 + 时间 + 审计），不再是用户主库。Casdoor 是用户主库。
- **Casdoor 客户端封装**：新模块 `casdoor_admin.py`，封装 Casdoor Admin API（加组、移组、禁用用户、查用户），用 Casdoor client credentials 鉴权。
- 复用 `channels/feishu.py`（卡片/文本发送）、`channels/feishu_ws.py`（WS 事件）、飞书 contact API。

### CS Hub（首个 OIDC 客户端 / 试点）
- **删**：`admin/feishu_oauth.py`、`/auth/feishu/login`、`/auth/feishu/callback`、`sso_store` 的用户/OAuth 部分。
- **加**：标准 OIDC 客户端（authlib）：登录重定向到 Casdoor、回调校验 id_token、检查 `approved-operators` 组 claim。
- **改**：中间件的会话校验 → 从"feishu 会话查本地 is_active"改为"校验 OIDC 会话/token + 组 claim 存在"；缺组 → 展示"待审批"页。
- **保留**：密码应急入口（过渡期超管兜底，feature-flag 可关）。

## 数据流

### 1. 首次登录 + 审批
```
员工 CS Hub 点飞书登录 → 302 Casdoor → 飞书扫码授权 → Casdoor 建号(不在组)
  → 回 CS Hub 带 id_token(无 approved 组 claim) → CS Hub 展示"待审批"页
并行:Casdoor new-user webhook → 治理服务 → 建 pending → 飞书审批卡片给审批人
管理员点【批准】→ 治理服务调 Casdoor group API 加入 approved-operators
  → 员工重新登录 → id_token 含组 claim → CS Hub 放行
```
无首登竞态：准入判定挂在"组 claim 是否存在"，与建号时刻解耦。

### 2. 正常登录（已审批）
```
CS Hub → Casdoor(若已有 SSO 会话则免扫码 = 真 SSO) → id_token 带组 → 放行
父域 Casdoor 会话 → 第 2 个应用登录时免扫码(真 SSO 跨应用)
```

### 3. 离职停用
```
飞书 deleted_v3/updated_v3(frozen/resigned/exited) 事件 或 每日对账
  → 治理服务调 Casdoor user API 置 isForbidden=true + 移出组
  → Casdoor 自身 SSO 会话立即失效
  → 各应用短 token(≤5min)到期 → 刷新回 Casdoor 被拒 → 数分钟内全线登出
```

## 离职跨应用撤销（短 token + 中心拦截）

各应用 access token 短 TTL（建议 5 分钟）+ 刷新时回 Casdoor 校验。禁用用户后：①Casdoor SSO 会话立即失效；②各应用下次刷新（≤5 分钟）被拒。各应用零额外集成，只需配短 TTL，不实现 back-channel logout。若将来某高敏应用要秒级，再单独叠加 back-channel logout（范围外）。

## 认证 vs 授权分离

- **认证**（你是谁）：Casdoor（飞书）。任何飞书员工可认证。
- **授权访问应用套件**（准入）：`approved-operators` 组成员（组 claim）。
- **应用内细粒度授权**（能干啥）：各应用自理（本 spec 不涉及；将来可用 Casdoor 每应用角色扩展）。

## 多应用授权模型（接第 2 个应用前的设计基线）

> 子项目一是"单应用（CS Hub）、单准入组"，够验证网关闭环。接第 2 个应用起，准入与角色都要按应用切分。以下三层机制**均已对活 Casdoor 实测**（groups 与 roles 都能进 id_token claim）。

### 三层授权

| 层 | 管什么 | 机制 | id_token claim | 实测 |
|---|---|---|---|---|
| ① 认证 | 你是谁 | Casdoor + 飞书 | `sub` = 飞书 open_id | ✅ |
| ② 准入 | 能不能进应用 X | 每应用组 `approved-<app>` | `groups`（值 `<org>/<组名>`） | ✅ |
| ③ 角色权限 | 进去能干啥 | 每应用角色 `<app>-<role>` | `roles`（值 `{owner,name}` 列表） | ✅ |

### 核心原则：分配中心化，含义应用自理

- **"谁有什么准入/角色"（分配）= 中心化**在 Casdoor + 治理服务：一处审计、离职一键全线失效、一张表看清每人对每应用的准入与角色。
- **"某角色能干什么"（含义）= 应用自理**：应用把自己的角色映射到权限/UI，网关只把 `roles` 塞进 token，不理解角色语义。加应用零网关负担。

### 每应用审批（相对单应用要改三处）

1. **每应用一个准入组** `approved-<app>`；各应用 OIDC 客户端配自己的 `OIDC_APPROVED_GROUP`，只查自己那个组 claim。
2. **审批状态按 (open_id, app) 记**：`approval_store` 从 `open_id → status` 改为 `open_id → {app: status}`（记录"张三：CS Hub 已批、财务待批"）。
3. **触发模型改为"应用上报"**：Casdoor 的 `signup` webhook 只在**首次注册**发一次，驱动不了"对第 N 个应用的申请"。故用户首登某应用、缺该应用的组/角色 → 应用的待审批页 `POST 网关 /request-access?app=<app>` → 网关建 (用户, app) pending + 推卡片。
4. **每应用审批人**：审批卡片按 `app` 路由到不同审批人；`decision.handle` 的审批人校验扩成"每应用审批人"（配置 `app → approver_open_id`）。

### 审批与角色合一（推荐简化）

不必"准入组 + 角色"两套。**审批 = 直接分配一个角色**：

- 用户首登应用 X、`roles` 里无任何 `<X>-*` → 待审批。
- 审批人批准时**选角色**（"批张三进财务系统，角色=会计"）→ Casdoor 给 `finance-会计` 角色。
- 应用：`roles` 含任一 `<X>-*` → 准入；具体角色 → 权限。

即"**有该应用任一角色 = 准入，具体角色 = 权限**"，一个概念，审批卡片顺带定角色。仅当需要"已批准但未定角色"的中间态时，才让准入组与角色分开（两套并存）。

### 离职闭环不变

无论准入用组还是角色，离职时治理服务 `disable_user` 置 Casdoor `isForbidden=true` → 该用户所有组/角色随之失效，**所有应用一次性断权**。这正是中心化分配的最大红利。

## 错误处理

- webhook 验签失败 → 拒绝 + 日志。
- Casdoor API 调用失败（加组/禁用）→ 重试 + 告警；离职禁用失败必须重试到成功或人工介入（安全攸关，宁可重试不可漏禁）。
- 卡片发送失败 → pending 记录仍建，日志告警，用户重登看"待审批"。
- OIDC 回调 state/nonce 校验失败 → 回登录页带错误。
- Casdoor 不可用 → CS Hub 展示友好错误 + 密码应急入口仍可用（过渡期）。
- 离职事件与对账对硬删除延续既有 fail-open 策略（飞书 get-user 无可靠 not-found 码，见前置 spec）。

## 迁移与回滚（CS Hub 试点）

- **数据迁移**：现有 `data/admin_auth.json` 的 approved_emails → 迁移脚本按邮箱在 Casdoor 建/匹配用户并加入 `approved-operators` 组。
- **逻辑迁移**：审批卡片、离职三层闭环整段从 CS Hub 搬到 `even-auth-gov`。
- **灰度**：feature-flag `AUTH_MODE=oidc|legacy`。默认先 legacy，切 oidc 验证，稳定后删 legacy 飞书登录代码（密码入口保留更久）。
- **回滚**：出问题切回 `AUTH_MODE=legacy`，原飞书登录代码在灰度期不删。

## 测试

- **Casdoor 配置冒烟**：飞书 provider 可扫码；OIDC discovery 端点正常；组 claim 出现在 id_token。
- **治理服务**：webhook 验签；卡片回调调 Casdoor 加组（mock `casdoor_admin`）；批准/拒绝/幂等/非审批人拒绝；离职事件→调禁用；每日对账→调禁用；fail-open。
- **CS Hub OIDC 客户端**：登录回调；组 claim 缺失→待审批页；有组→放行；短 token 刷新回 Casdoor 校验。
- **端到端**：新用户全程（扫码→待审批→批准→放行）；离职全程（禁用→数分钟内应用登出）；真 SSO（第二个 mock 应用免登）。

## 改动 / 新建清单

| 项 | 说明 |
|---|---|
| Casdoor 部署 | docker-compose 加 Casdoor + PostgreSQL；nginx 反代 auth 子域 |
| `even-auth-gov`（新服务） | webhook 接收、卡片回调、离职引擎、瘦 sso_store、`casdoor_admin.py` |
| 迁移脚本 | approved_emails → Casdoor `approved-operators` 组 |
| CS Hub | 删 feishu_oauth/OAuth 路由；加 OIDC 客户端 + 组 claim 检查 + 待审批页；中间件改造；feature-flag |
| 从 CS Hub 迁出 | 审批卡片 + 离职三层闭环 → even-auth-gov |

## 实施前置验证（实施计划第一步必须先验，避免在假设上盖楼）

以下是本设计的载荷性假设，都属 Casdoor 标准能力但未在本项目实测，实施计划的第一批任务应先在一个 Casdoor 实例上逐一验通、再往下做：

1. **组成员能进 id_token claim**：Casdoor 能把用户的 `approved-operators` 组/角色成员关系作为一个 claim 放进 OIDC id_token（授权门整个挂在这上面）。
2. **禁用即杀会话**：Casdoor 置用户 `isForbidden=true` 后，其现有 SSO 会话立即失效、且后续 token 刷新被拒（离职撤销的地基）。
3. **new-user webhook 及其验签**：Casdoor 在新用户建号时发 webhook、且有可验证的签名/鉴权头（治理服务据此触发审批）。
4. **Admin API 可加组/移组/禁用**：Casdoor 提供以 client credentials 鉴权的用户/组管理 API（治理服务的动作面）。
5. **飞书原生 provider 端到端可扫码**：调研已证其存在（F1），仍需在实例上跑通一次真实扫码登录。

任一条不成立，需回到本设计调整对应环节（例如组 claim 不可得则改用 Casdoor 每应用角色或治理服务侧的 introspection 端点）。

## 范围外（后续各自成 spec）

- 应用 2..N 接入（每个 = 注册一个 Casdoor OIDC 客户端 + 配短 TTL + 组 claim 检查，小 spec）。
- 非飞书身份源（邮箱/密码、其他 OIDC/SAML IdP、外部合作方 SAML）。
- Casdoor 高可用 / 多副本（试点先单实例）。
- 应用内细粒度授权 / 每应用角色。
- 高敏应用的 back-channel logout 秒级撤销。
