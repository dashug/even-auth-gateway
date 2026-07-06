# even-auth-gateway

统一飞书(Feishu/Lark)登录网关 / 身份中心。把飞书扫码登录 + 入职审批 + 离职自动停用收敛成一份，供多个内部应用共享。

**独立于业务应用**（如 even-cs-hub）——各应用只作为标准 OIDC 客户端信任本网关。

## 组成

- **Casdoor**（第三方 IdP，`deploy/casdoor/`）：承接飞书原生登录 + OIDC/SAML/会话/多应用/管理后台。身份真理源。
- **even-auth-gov**（`even_auth_gov/`，本仓核心）：治理控制器。收 Casdoor webhook → 推飞书审批卡片 → 批准调 Casdoor 加组；离职（飞书事件 + 每日对账）→ 调 Casdoor 禁用。**自带飞书集成**（`feishu.py`），不依赖任何业务应用。

## 模块

| 文件 | 职责 |
|---|---|
| `casdoor_admin.py` | Casdoor Admin API（按飞书 open_id 加组/移组/禁用/反查），已对活实例验证 |
| `approval_store.py` | 审批工作流状态机（pending/approved/denied/disabled） |
| `decision.py` | 卡片审批决策（审批人校验 + 幂等 + 批准→加组） |
| `webhook.py` | Casdoor webhook 接收 + 验签 + 推审批卡片 |
| `offboard.py` | 离职引擎 → Casdoor 禁用 + 移组 |
| `ws_events.py` | 飞书 contact 事件（deleted/updated）→ 离职 |
| `reconcile.py` | 每日对账兜底（fail-open） |
| `migrate_emails.py` | 邮箱白名单 → Casdoor 组 迁移 |
| `app.py` | FastAPI 组装（webhook 路由 + 卡片回调） |
| `feishu.py` | 网关自带飞书集成（client/发消息/卡片构建） |
| `settings.py` | 环境变量配置（审批人 open_id、Casdoor 端点） |

## 关键设计

- 认证/授权分离：Casdoor 答"你是谁"（飞书 open_id = OIDC `sub`）；准入 = `approved-operators` 组成员（org 限定 `<org>/approved-operators`，进 id_token `groups` claim）。
- 离职撤销：短 token + 中心拦截（禁用即挡登录 + 应用刷新回 Casdoor 复验）。
- 详见 `docs/design.md`、`docs/implementation-plan.md`、`docs/casdoor-findings.md`（Phase 0 对活实例的实测结论）。

## 环境变量

```
FEISHU_APP_ID / FEISHU_APP_SECRET     # 飞书应用(用于发卡片、查 contact 状态、离职事件)
APPROVER_FEISHU_ID                    # 审批人 open_id
CASDOOR_ENDPOINT                      # Casdoor 地址
CASDOOR_CLIENT_ID / CASDOOR_CLIENT_SECRET  # 调 Casdoor Admin API 的凭据
CASDOOR_ORG                           # 飞书用户所在 org
CASDOOR_WEBHOOK_SECRET                # webhook 共享 token(Casdoor webhook 自定义头里配同值)
CASDOOR_WEBHOOK_HEADER                # webhook 携带 token 的头名(默认 X-Webhook-Token,须与 Casdoor 后台一致)
APPROVAL_STORE_FILE                   # 审批状态文件路径
```

## 开发

```
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
TESTING=1 .venv/bin/python -m pytest tests/ -q
```

Casdoor 本地起：`docker compose -f deploy/casdoor/docker-compose.yml up -d`（见 `deploy/casdoor/README.md`）。

## 待办（生产上线前）

- [x] **Casdoor 应用 token 字段裁剪** —— 设 `tokenFormat=JWT-Custom` + `tokenFields` 白名单（默认 JWT 无视白名单）。已对活实例验证 `passwordSalt` 等敏感字段消失、claim 85→20、groups/roles 保留。见 `docs/integration-guide.md` 第一步.4。
- [x] **飞书 WS 长连接接线** —— `feishu_ws.py` 建 WS 连接注册 `contact.user.deleted_v3/updated_v3` + 卡片回调，`app.py` lifespan 启动（`CHANNEL_CLIENTS_ENABLED` 开关，TESTING 跳过）。含 uvloop 死锁规避。
- [x] **每日对账调度接线** —— `scheduler.py` 用 APScheduler 按 `SSO_RECONCILE_CRON`（默认 `0 8 * * *`）跑 `reconcile.run`，`app.py` lifespan 启动（`SCHEDULER_ENABLED` 开关）。
- [x] **webhook 验签对齐 Casdoor 真实行为** —— Casdoor **不做 body HMAC 签名**,认证靠 webhook 上配置的**自定义头传共享 token**（见 `docs/casdoor-findings.md`）。`_verify` 改为常量时间比对 `CASDOOR_WEBHOOK_HEADER`（默认 `X-Webhook-Token`）的值 == `CASDOOR_WEBHOOK_SECRET`（fail-closed）；body 按 Casdoor Record 解析（`extendedUser`/`object` 取用户对象）。活验:正确 token+真实 Record→200、错/无 token→401、pending 正确建出。
