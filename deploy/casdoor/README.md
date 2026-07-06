# Phase 0 Runbook — Casdoor 网关地基验证

**目标**：在你有 Docker + 飞书凭据的机器上，验证 5 个载荷性假设，把实测结果填进
`docs/superpowers/specs/casdoor-findings.md`。这份 findings 是 Phase 1 代码里所有
Casdoor 端点/claim 占位符的**唯一事实源**——填完我据此一次性校准。

前置：Docker Desktop 已启动；一个飞书自建应用的 App ID / App Secret（就用 CS Hub 那个也行）。

---

## Step 1 — 起 Casdoor（Task 0.1）

```bash
docker compose -f deploy/casdoor/docker-compose.yml up -d
sleep 25
curl -s -o /dev/null -w "casdoor: HTTP %{http_code}\n" http://127.0.0.1:8000/
```

期望 `HTTP 200`。浏览器开 `http://127.0.0.1:8000`，默认管理员 **admin / 123**（登录后改密码）。

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
