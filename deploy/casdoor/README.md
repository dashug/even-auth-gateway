# Phase 0 Runbook — Casdoor 网关地基验证

**目标**：在你有 Docker + 飞书凭据的机器上，验证 5 个载荷性假设，把实测结果填进
`docs/casdoor-findings.md`。这份 findings 是 Phase 1 代码里所有
Casdoor 端点/claim 占位符的**唯一事实源**——填完我据此一次性校准。

前置：Docker Desktop 已启动；一个飞书自建应用的 App ID / App Secret（就用 CS Hub 那个也行）。

---

## Step 1 — 起 Casdoor（Task 0.1）

```bash
docker compose -f deploy/casdoor/docker-compose.yml up -d
sleep 25
curl -s -o /dev/null -w "casdoor: HTTP %{http_code}\n" http://127.0.0.1:8000/
```

期望 `HTTP 200`。浏览器开 `http://127.0.0.1:8000`，用内置管理员 `admin` 登录：**新版镜像没有默认密码**——起之前在 `.env` 设 `CASDOOR_INIT_ADMIN_PASSWORD`（只在首次启动、空库时生效），没设就得开浏览器自己设；老镜像仍是 `admin / 123`，登录后立刻改。

> 若 Casdoor 起不来：`docker compose -f deploy/casdoor/docker-compose.yml logs casdoor | tail -50`。
> 最常见是 postgres 连接串格式与所装 Casdoor 版本不符——按日志提示调 `dataSourceName`，
> 或参考 https://casdoor.org/docs/basic/server-installation 的 Docker 章节。

findings 记：Casdoor 版本、DB=postgres、访问方式。

## Step 2 — 飞书原生 provider 端到端扫码（Task 0.2）

1. UI → **Providers** → Add：Category=`OAuth`，Type=`Lark`，Client ID=飞书 App ID，Client secret=飞书 App Secret。
2. 飞书开放平台 → 你的应用 → 安全设置 → 重定向 URL 加 `http://127.0.0.1:8000/callback`。
3. UI → **Applications** → `app-built-in` → 把刚建的 Lark provider 加进去 → 保存。
4. 打开 Casdoor 登录页，点飞书图标，**真机扫码**。

findings 记：✅/❌ 扫码登录；登录后 Casdoor 用户对象里飞书身份落在哪些字段（`name`/`email`/`id`/`externalId`…）——迁移脚本和组匹配要用。

## Step 3 — 组成员进 id_token claim（Task 0.3，最关键）

1. UI → **Groups** → 新建 `approved-operators`。把测试用户加进去。
2. UI → **Applications** → 新建 `cs-hub`：记下 Client ID / Secret；Redirect URLs 填
   `http://127.0.0.1:9000/auth/callback`；确保勾选返回 groups/roles 的 scope。
3. 手动走一次授权码流解 id_token：

```bash
# (a) 浏览器打开授权 URL(替换 CLIENT_ID)，扫码后从回调地址取 code：
#   http://127.0.0.1:8000/login/oauth/authorize?client_id=CLIENT_ID&response_type=code&redirect_uri=http://127.0.0.1:9000/auth/callback&scope=openid%20profile%20email&state=x
# (b) 用 code 换 token：
curl -s -X POST http://127.0.0.1:8000/api/login/oauth/access_token \
  -d grant_type=authorization_code -d client_id=CLIENT_ID -d client_secret=CLIENT_SECRET \
  -d code=PASTE_CODE -d redirect_uri=http://127.0.0.1:9000/auth/callback | python3 -m json.tool
# (c) 把返回的 id_token 中间段 base64 解出来看 payload：
python3 -c "import sys,base64,json; t=input('id_token: ').split('.')[1]; print(json.dumps(json.loads(base64.urlsafe_b64decode(t+'=='*3)),indent=2,ensure_ascii=False))"
```

findings 记（**决定成败**）：组成员在 id_token 里的**确切 claim 名与结构**（例 `"groups":["approved-operators"]`）。
若解不出组 claim → 记 ❌ + fallback（改用 Casdoor 每应用角色，或治理服务侧 `/api/get-user` 查组）。

## Step 4 — Admin API + 禁用即杀会话（Task 0.4.1-0.4.2）

实测这三个动作的**确切端点 + 请求体 + 鉴权方式**（Casdoor 版本相关，别猜）：

```bash
# 候选：加组 / 移组 / 禁用。用管理员 token 或应用 clientId/clientSecret 鉴权。
# 具体端点查 http://127.0.0.1:8000/swagger 或 Casdoor API 文档，实测记录。
```

- 加入 `approved-operators`（候选 `POST /api/update-user` 带 groups 字段 或 `/api/add-user-to-group`）
- 从组移除
- 禁用用户（候选 `POST /api/update-user` 置 `isForbidden=true`）
- **禁用即杀会话**：让测试用户登录建会话 → 调禁用 → 用其会话访问受保护页/刷新，确认失效。

findings 记：每个动作的 method/path/body/auth；禁用后会话失效 ✅/❌ 及延迟。

## Step 5 — new-user webhook + 验签（Task 0.4.3）

1. 起个临时 echo 收 webhook：`while true; do nc -l 127.0.0.1 9999; done`（或 RequestBin）。
2. Casdoor UI 配 webhook：URL 指向它，事件选 `signup`/`add-user`。
3. 手动建一个新用户触发。

findings 记：webhook body 结构；**是否带签名头（HMAC?）及验签方式**。若无内置签名 → 记
「共享密钥 header + 内网限定」作替代（Phase 1 `webhook.py` 默认 HMAC-SHA256，按实测调整）。

---

## 门控

5 项（飞书登录 / 组 claim / Admin API / 禁用杀会话 / webhook）都在 findings 记为可行
（或有可接受 fallback）后，回来找我：我据 findings 校准 Phase 1 占位端点 + 做 Phase 2（CS Hub 改 OIDC 客户端）。
任一硬失败 → 回设计 spec 调整对应环节，别硬往下盖。

---

# 生产部署（上线前清单）

配置全走同目录 `.env`（`cp .env.example .env` 后改），compose 自动读取。

1. **强 DB 密码**：`.env` 里设 `CASDOOR_DB_PASSWORD` 为强随机值（默认值只配 spike 用）。
2. **内置 admin 密码**：它是 IdP 的超管。新版镜像首次启动前在 `.env` 设强随机的 `CASDOOR_INIT_ADMIN_PASSWORD`（不设则谁先打开页面谁来设）；老镜像默认 `admin/123`，首次登录立刻改。
3. **钉镜像 digest**：`CASDOOR_IMAGE=casbin/casdoor@sha256:...`，别用 `:latest`（可复现 + 防上游意外变更）。
4. **nginx 前置 TLS**：casdoor 只 `127.0.0.1:8000` 暴露，由 nginx 反代到 auth 子域做 HTTPS；生产可把 db 的 `ports` 整段删掉（只走 compose 内网）。
5. **治理服务密钥入密管**：`gov.env` 里的 `FEISHU_APP_SECRET / CASDOOR_CLIENT_SECRET / CASDOOR_WEBHOOK_SECRET` 不写死、不提交（已在 `.gitignore`）。`CASDOOR_WEBHOOK_SECRET` 只配在 Casdoor webhook 上，**不发给接入应用**。
6. **id_token 裁字段**：每个接入应用设 `tokenFormat=JWT-Custom` + `tokenFields` 白名单（见 `docs/integration-guide.md` 第一步.4，`passwordSalt` 等敏感字段必裁）。

## 治理服务 even-auth-gov（compose profile `gov`）

镜像由仓库根 `Dockerfile` 构建（非 root、单 worker、自带 `/healthz` 健康检查），和 Casdoor 同一个 compose 项目：

```bash
cp deploy/casdoor/gov.env.example deploy/casdoor/gov.env     # 填飞书 / Casdoor / 审批人 / 共享 token
docker compose -f deploy/casdoor/docker-compose.yml --profile gov up -d --build
curl -s http://127.0.0.1:8100/healthz        # {"status":"ok","feishu_ws":true,"scheduler":true}
```

- **Casdoor webhook**：后台 Webhooks → URL `http://even-auth-gov:8080/casdoor/webhook`（compose 内网），事件勾 `signup`，自定义头 `X-Webhook-Token: <CASDOOR_WEBHOOK_SECRET>`，勾 `isUserExtended`。
- **接入应用调 `/request-access`**：应用把用户刚登录拿到的 id_token 交给网关，网关验 Casdoor 签名——应用不持有任何网关密钥。每接一个应用，在 `gov.env` 的 `APP_CLIENT_IDS` 加 `<应用短名>:<clientId>`；`CASDOOR_ISSUER` 填 Casdoor 对外地址（token 里的 `iss`，和内网 `http://casdoor:8000` 不同）。经 nginx 把 `8100` 反代给内网应用（与 Casdoor 同样只做 TLS 前置，不对公网开放）。契约见 `docs/integration-guide.md`「待审批」。
- **审批时分配角色**：在 Casdoor 给应用建 `<应用短名>-<role>` 角色（显示名填中文），审批卡片自动按角色出按钮，无需改网关配置。
- **飞书应用必须与 Casdoor Lark provider 同一个**：open_id 按飞书应用区分，否则审批卡片/在职查询拿到的 open_id 对不上 Casdoor 的 `sub`。
- **运维命令**（在容器里跑，读写的是同一份审批库）：
  ```bash
  # 审计导出(F-5.8.2)
  docker compose -f deploy/casdoor/docker-compose.yml --profile gov exec -T even-auth-gov \
    python -m even_auth_gov.audit_export > audit.csv
  # CS Hub 老操作员迁移(先 --dry-run 看匹配,再真跑)
  docker compose -f deploy/casdoor/docker-compose.yml --profile gov cp admin_auth.json even-auth-gov:/tmp/
  docker compose -f deploy/casdoor/docker-compose.yml --profile gov exec even-auth-gov \
    python -m even_auth_gov.migrate_emails /tmp/admin_auth.json --dry-run
  ```

## 备份 / 恢复（务必配）

**Casdoor 的 postgres 卷是唯一身份真理源——丢了它 = 丢全部用户/组/角色/应用配置。**

备份（cron 每日 + backups/ 同步异地）：
```
deploy/casdoor/backup.sh
# cron: 0 3 * * *  /path/to/even-auth-gateway/deploy/casdoor/backup.sh >> /var/log/casdoor-backup.log 2>&1
```

恢复（从某份备份重建库）：
```
gunzip -c deploy/casdoor/backups/casdoor-<ts>.sql.gz \
  | docker compose -f deploy/casdoor/docker-compose.yml exec -T casdoor-db psql -U casdoor casdoor
```

治理服务在跑时，`backup.sh` 会顺带备份审批库（`backups/approvals-<ts>.json.gz`，审批/拒绝/停用的审计留痕）。恢复：
```
gunzip -c deploy/casdoor/backups/approvals-<ts>.json.gz \
  | docker compose -f deploy/casdoor/docker-compose.yml --profile gov exec -T even-auth-gov sh -c 'cat > /data/approvals.json'
```

## 单实例约束（重要）

`even-auth-gov` 治理服务目前设计为**单实例**运行，原因：
- 飞书 WS 长连接（离职事件 + 卡片回调）单连接即可，多实例会重复处理；
- 审批状态是 JSON 文件 + 进程内/文件锁，Casdoor 组/角色变更是进程内串行锁——**跨实例不互斥**。

所以：**跑 1 个副本**（要高可用用主备/重启拉起，不要多活）。将来要多活，需把审批状态挪到共享存储（如 postgres）并给 Casdoor 组变更上分布式锁——那是另一轮改造，不在当前范围。
