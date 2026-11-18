# even-auth-gateway

统一飞书(Feishu/Lark)登录网关 / 身份中心。把飞书扫码登录 + 入职审批 + 离职自动停用收敛成一份，供多个内部应用共享。

**独立于业务应用**（如 even-cs-hub）——各应用只作为标准 OIDC 客户端信任本网关。

## 组成

- **Casdoor**（第三方 IdP，`deploy/casdoor/`）：承接飞书原生登录 + OIDC/SAML/会话/多应用/管理后台。身份真理源。
- **even-auth-gov**（`even_auth_gov/`，本仓核心）：治理控制器。收 Casdoor webhook → 推飞书审批卡片 → 批准调 Casdoor 加组；离职（飞书事件 + 每日对账）→ 调 Casdoor 禁用。**自带飞书集成**（`feishu.py`），不依赖任何业务应用。

## 模块

| 文件 | 职责 |
|---|---|
| `casdoor_admin.py` | Casdoor Admin API（按飞书 open_id 加组/移组/禁用/反查 + 列应用角色/赋角色/撤角色），已对活实例验证 |
| `approval_store.py` | 审批工作流状态机,按 `(open_id, app)` 记（pending/approved/denied/disabled） |
| `decision.py` | 卡片审批决策（审批人/升级对象校验 + 幂等 + 批准→〔该应用只留所选角色〕→加对应 app 的组；拒绝→撤组+撤该应用角色；分了角色的应用不许无角色批准） |
| `webhook.py` | Casdoor webhook 接收 + 验签 + 推审批卡片（落到 `DEFAULT_APP`）；`notify_pending` 送达才记 `notified_at` |
| `offboard.py` | 离职引擎 → 按用户禁用 Casdoor + 清掉其在 Casdoor 上全部 `approved-*` 组与角色 |
| `ws_events.py` | 飞书 contact 事件（deleted/updated）→ 离职 |
| `feishu_ws.py` | 飞书 WS 长连接（离职事件 + 卡片回调）；审批结果回告审批人与申请人 |
| `reconcile.py` | 每日对账兜底（fail-open,按用户去重,跳过已禁用账号）；待批扫描：补发、催办、超时升级 |
| `scheduler.py` | APScheduler：每日对账 + 权限核对，每小时待批扫描 |
| `migrate_emails.py` | 邮箱白名单 → Casdoor 组 迁移（cs-hub 试点），CLI 支持 `--dry-run` |
| `audit_export.py` | 审批/停用审计导出 CSV（CLI，可按 app/状态过滤） |
| `access_report.py` | 权限视图“谁能访问什么” + 离职撤权核对（以 Casdoor 实际状态为准，CLI 导出 CSV；每日对账后推送异常） |
| `accounts.py` | 受管的非飞书账号：规则（类型/负责人/到期日）、审批闸门、管理员命令行 `python -m even_auth_gov.accounts` |
| `account_lifecycle.py` | 受管账号生命周期：建号/纳管/负责人确认、到期扫描与续期提醒、续期、负责人离职连带停用、换负责人、设密码 |
| `id_token.py` | `/request-access` 鉴权：验接入应用转交的 Casdoor id_token（JWKS 验签，`aud`→应用） |
| `app.py` | FastAPI 组装（webhook 路由 + `/request-access` + `/healthz`） |
| `feishu.py` | 网关自带飞书集成（client/发消息/卡片构建,卡片带 app 标识与按角色批准按钮） |
| `settings.py` | 环境变量配置（审批人 open_id、按应用覆盖、Casdoor 端点） |

## 关键设计

- 认证/授权分离：Casdoor 答"你是谁"（飞书 open_id = OIDC `sub`）；准入 = 按应用区分的 `approved-<app>` 组成员（org 限定 `<org>/approved-<app>`，进 id_token `groups` claim）。**APP-AWARE**（设计评审 #2/#16）：准入按 `(open_id, app)` 记，同一用户可对应用 A 待批、对应用 B 已批，互不影响；只有离职（禁用 Casdoor 账号）是按用户一次性生效，会清掉该用户在**所有**应用上的准入。
- 第一个接入的应用（默认 `cs-hub`，见 `DEFAULT_APP`）走 Casdoor signup webhook 自动建 pending；第二个及以后的应用用 `POST /request-access` 主动上报"某用户在等本应用批准"——body 是用户刚登录拿到的 **Casdoor id_token**，网关验签后从 claim 里取申请人、从 `aud` 认应用（`APP_CLIENT_IDS` 登记），调用方没法冒名或冒充别的应用，也不持有任何网关密钥。
- 审批即赋角（F-5.5.3）：应用在 Casdoor 建了 `<app>-<role>` 角色，卡片就按角色出"批准为「X」"按钮，先赋角色再加准入组；"批准为「X」"即该应用只留 X（清掉此前残留的其它角色）。**准入仍以 `approved-<app>` 组为准**，角色是附加项——不分角色的应用零改动。拒绝时撤该应用角色，离职时撤全部角色，不留"有角色没准入"的残余。
- 角色强制（里程碑 3）：**分了角色的应用不允许"有准入没角色"**。审批卡片读不到角色清单时会降级成普通"批准"按钮，真点下去网关会拒绝并给点击人重发一张带角色按钮的卡片；迁移老用户用 `migrate_emails --role`；接入应用也要拒绝没有角色的用户（接入说明检查清单）。
- 审批人可按应用覆盖（`APPROVER_<APP>`，未配落到全局 `APPROVER_FEISHU_ID`），审批卡片上明确标出申请接入的应用。
- 审批卡片**送达才算已通知**：飞书发送失败或审批人未配置 → `notified_at` 留空，每小时的待批扫描补发；已送达但超 `SSO_PENDING_RESEND_SECONDS`（默认 1 天）仍待批 → 再催办一次。
- 超时升级（里程碑 3）：审批人**第一次收到卡片**后超过 `APPROVAL_ESCALATE_HOURS`（默认 24）仍未处理 → 卡片另发给 `APPROVAL_ESCALATE_TO`（默认全局审批人），升级对象和原审批人谁先点算谁；每条申请只升级一次，审计导出带 `escalated_to/at`。审批人本身就是全局审批人的应用不升级。
- 权限视图与撤权核对（F-5.8.3）：`access_report` 以 Casdoor 实际的组/角色/账号状态为准，合上审批库留痕，导出"人 × 应用"一张表；核对清单按严重程度列出离职未停用、停用后仍挂权限、有角色没准入、有准入没角色、迁移/手工授权等。每日对账后自动核对，高/中级异常推给全局审批人。
- 非飞书账号（里程碑 4a）：外部合作方、没飞书的员工、服务账号用邮箱/密码登录，**每个都必须登记负责人（飞书员工）和到期日**（写在 Casdoor 用户 `properties` 的 `evauth_*` 键上）。只能由管理员命令行建号，负责人在飞书点「确认负责」后才启用。没登记、负责人没确认、已过期的非飞书账号**过不了审批闸门**（signup webhook / `/request-access` / 批准三处都查），堵住了"陌生人自助注册后骗批准"。
- 受管账号回收：到期前 14/3 天给负责人发续期卡片，过期按离职流程停用（停账号 + 清准入组和角色）；负责人离职时名下账号连带停用，每日对账也会查负责人的在职状态；登记信息异常按到期处理（宁可错停）。续期后账号重新启用，但要重新申请各应用。飞书员工的备用密码登录挂在本人账号上，离职停用自然覆盖。
- 审批结果回告：点完按钮后，审批人收到处理结果（含 Casdoor 加组失败提示，可再点重试），申请人收到批准/拒绝通知。
- 离职撤销：短 token + 中心拦截（禁用即挡登录 + 应用刷新回 Casdoor 复验）。
- 详见 `docs/design.md`、`docs/implementation-plan.md`、`docs/casdoor-findings.md`（Phase 0 对活实例的实测结论）。

## 环境变量

```
FEISHU_APP_ID / FEISHU_APP_SECRET     # 飞书应用(发卡片、查 contact 状态、离职事件);须与 Casdoor Lark provider 同一应用(open_id 按应用区分)
FEISHU_ENCRYPT_KEY / FEISHU_VERIFICATION_TOKEN  # 飞书事件加密/校验(WS 模式可留空)
APPROVER_FEISHU_ID                    # 全局审批人 open_id(未按应用覆盖时的兜底)
APPROVER_<APP>                        # 按应用覆盖审批人,如 APPROVER_CS_HUB(app 名转大写、'-'→'_')
DEFAULT_APP                           # Casdoor signup webhook 不带 app 信息时的默认应用名(默认 cs-hub)
CASDOOR_ENDPOINT                      # Casdoor 地址
CASDOOR_CLIENT_ID / CASDOOR_CLIENT_SECRET  # 调 Casdoor Admin API 的凭据
CASDOOR_ORG                           # 飞书用户所在 org
CASDOOR_WEBHOOK_SECRET                # webhook 共享 token(Casdoor webhook 自定义头里配同值;只给 Casdoor,不发给接入应用)
CASDOOR_WEBHOOK_HEADER                # webhook 携带 token 的头名(默认 X-Webhook-Token,须与 Casdoor 后台一致)
APP_CLIENT_IDS                        # 可调 /request-access 的应用登记:<应用短名>:<clientId>,逗号分隔
CASDOOR_ISSUER                        # id_token 的 iss 期望值 = Casdoor 对外地址(留空不校验 iss)
APPROVAL_STORE_FILE                   # 审批状态文件路径(默认 data/approvals.json;容器内 /data/approvals.json)
SSO_RECONCILE_CRON / SCHEDULER_TIMEZONE   # 对账时间(默认 0 8 * * *,Asia/Shanghai)
SSO_PENDING_RESEND_SECONDS            # 已送达但仍待批的催办间隔(默认 86400)
SSO_PENDING_SWEEP_CRON                # 待批扫描:补发/催办/超时升级(默认 30 * * * *,每小时)
APPROVAL_ESCALATE_HOURS               # 审批人收到卡片后多少小时未处理就升级(默认 24;0 关闭)
APPROVAL_ESCALATE_TO                  # 超时升级给谁(飞书 open_id;默认 APPROVER_FEISHU_ID)
ACCESS_REPORT_ALERT                   # 每日对账后推送权限核对异常(默认 true)
ACCOUNT_MAX_DAYS / ACCOUNT_RENEW_DAYS # 受管账号每次有效期上限 / 一次续期天数(默认 180 / 90)
ACCOUNT_REMIND_DAYS                   # 到期前第几天提醒负责人续期(默认 14,3)
ACCOUNT_EVENTS_FILE                   # 受管账号变更历史(默认和审批库同目录的 account_events.jsonl)
SSO_LOG_LEVEL                         # 日志级别(默认 INFO)
CHANNEL_CLIENTS_ENABLED / SCHEDULER_ENABLED  # 关掉飞书 WS / 对账调度(默认开)
```

完整模板见 `deploy/casdoor/gov.env.example`。

## 开发

```
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
TESTING=1 .venv/bin/python -m pytest tests/ -q
uvicorn --factory even_auth_gov.app:build_app --port 8100   # 本地起治理服务
```

Casdoor 本地起：`docker compose -f deploy/casdoor/docker-compose.yml up -d`（见 `deploy/casdoor/README.md`）。

## 部署与运维

- 治理服务镜像：仓库根 `Dockerfile`；compose 里是 `gov` profile：`docker compose -f deploy/casdoor/docker-compose.yml --profile gov up -d --build`（单实例，见部署 README「单实例约束」）。
- 探活：`GET /healthz` → `{"status":"ok","feishu_ws":bool,"scheduler":bool}`。
- 审计导出：`python -m even_auth_gov.audit_export -o audit.csv [--app X] [--status S]`；受管账号变更历史加 `--accounts`。
- 受管账号：`python -m even_auth_gov.accounts create --kind partner|staff|service --name N --email E --sponsor <负责人飞书邮箱> --expires YYYY-MM-DD`（初始密码只打印一次）；另有 `register`（纳管已有账号）、`resend-confirm`、`renew`、`transfer`、`set-password`（含飞书员工备用登录）、`list`。
- 权限视图：`python -m even_auth_gov.access_report -o access.csv [--app X]`；撤权核对：`--anomalies [--check-feishu]`（后者逐个查飞书在职状态，慢）。
- 老操作员迁移：`python -m even_auth_gov.migrate_emails admin_auth.json --dry-run`（确认后去掉 `--dry-run`；应用分了角色时加 `--role <角色>`）。
- 备份：`deploy/casdoor/backup.sh`（Casdoor 库 + 审批库 + 受管账号变更历史）。

## 待办（生产上线前）

- [x] **Casdoor 应用 token 字段裁剪** —— 设 `tokenFormat=JWT-Custom` + `tokenFields` 白名单（默认 JWT 无视白名单）。已对活实例验证 `passwordSalt` 等敏感字段消失、claim 85→20、groups/roles 保留。见 `docs/integration-guide.md` 第一步.4。
- [x] **飞书 WS 长连接接线** —— `feishu_ws.py` 建 WS 连接注册 `contact.user.deleted_v3/updated_v3` + 卡片回调，`app.py` lifespan 启动（`CHANNEL_CLIENTS_ENABLED` 开关，TESTING 跳过）。含 uvloop 死锁规避。
- [x] **每日对账调度接线** —— `scheduler.py` 用 APScheduler 按 `SSO_RECONCILE_CRON`（默认 `0 8 * * *`）跑 `reconcile.run`，`app.py` lifespan 启动（`SCHEDULER_ENABLED` 开关）。
- [x] **治理服务容器化** —— `Dockerfile` + compose `gov` profile + `/healthz` + 审批库纳入备份。
- [x] **审批结果回告 + 审计导出** —— F-5.3.4 / F-5.8.2 的最小实现。
- [x] **`/request-access` 改验 id_token** —— 去掉与 webhook 共用的共享密钥，防冒名骗批准；已对活 Casdoor 端到端验证（`docs/casdoor-findings.md` §7）。
- [x] **审批时选角色（F-5.5.3）** —— 角色清单取自 Casdoor `<app>-<role>`，按角色出批准按钮；审计导出带 `role` 列。批准时该应用只留所选角色，拒绝撤该应用角色，离职撤全部角色。
- [x] **里程碑 3 · 平台化治理** —— 权限视图 + 离职撤权核对（`access_report`，每日推送异常）；角色强制（分了角色的应用不许无角色批准、迁移 `--role`）；审批超时升级（`APPROVAL_ESCALATE_*`，每小时待批扫描）。
- [x] **里程碑 4a · 非飞书账号与受管外部账号** —— 邮箱/密码账号必须有负责人 + 到期日；邀请制建号 + 负责人确认；审批闸门（顺带修了自助注册骗批准）；到期提醒/续期/过期停用；负责人离职连带停用；核对报表报出未登记和过期仍启用的账号。已对活 Casdoor 端到端验证（`docs/casdoor-findings.md` §9）。
- [x] **webhook 验签对齐 Casdoor 真实行为** —— Casdoor **不做 body HMAC 签名**,认证靠 webhook 上配置的**自定义头传共享 token**（见 `docs/casdoor-findings.md`）。`_verify` 改为常量时间比对 `CASDOOR_WEBHOOK_HEADER`（默认 `X-Webhook-Token`）的值 == `CASDOOR_WEBHOOK_SECRET`（fail-closed）；body 按 Casdoor Record 解析（`extendedUser`/`object` 取用户对象）。活验:正确 token+真实 Record→200、错/无 token→401、pending 正确建出。

以下需要真实环境，代码侧做不了，上线前逐项过：

- [ ] **接真实飞书联调** —— 目前只有单测 + 飞书侧模拟的端到端。要确认：离职事件能收到并禁用；审批卡片在 PC / 手机上显示正常（含角色按钮、非飞书账号的身份行）、点击能回调到网关并按所选角色开通；审批结果通知送达审批人和申请人；受管账号的负责人确认卡片、续期提醒卡片显示正常、按钮回调生效。
- [ ] **Casdoor 关闭自助注册** —— 生产应用打开 `disableSelfSignup`（`enableSignUp` 保持开着），见部署 README「受管的非飞书账号」；真实飞书联调时确认飞书首次扫码仍能自动注册。
- [ ] **飞书开放平台配置** —— 机器人、长连接事件/回调、通讯录权限范围覆盖全员，见部署 README「治理服务」一节。
- [ ] **生产部署** —— 按 `deploy/casdoor/README.md`「生产部署（上线前清单）」：强密码、钉镜像 digest、nginx TLS、`gov.env`（含 `APP_CLIENT_IDS` / `CASDOOR_ISSUER`）、备份 cron。
- [ ] **恢复演练** —— `backup.sh` 只管备份；异地留存 + 实际恢复一次（PRD 风险项）还没做。
- [ ] **CS Hub 老操作员迁移** —— `migrate_emails --dry-run` 对生产 Casdoor 核对匹配结果，再真跑。
- [ ] **CS Hub 切到网关登录** —— 按接入说明改登录、关掉自建直连登录（F-5.1.3）、会话短 TTL（≤5 分钟断权的前提）。

PRD 后续里程碑（未开始，单独立项）：

- 里程碑 4b（P2）：账号自动供给/回收。外部 OIDC/SAML 身份提供方等有具体对接方时再加（治理层不用改，配 Casdoor 并把外部登录绑到已建账号）。

已确认的约束：

- **单实例运行** —— 多活需审批库挪 Postgres + Casdoor 组/角色变更上分布式锁，作为单独一轮改造，当前不做（见部署 README「单实例约束」）。
