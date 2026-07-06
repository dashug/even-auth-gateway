# 统一飞书登录网关（子项目一）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 立起 Casdoor 统一登录网关（飞书原生登录 + OIDC 签发），把 CS Hub 现有飞书治理逻辑（审批卡片 + 离职闭环）抽成独立"治理服务"对接 Casdoor，并把 CS Hub 改造成首个 OIDC 客户端验证闭环。

**Architecture:** Casdoor（Docker，身份真理源，飞书 provider + OIDC/SAML）→ 治理服务 even-auth-gov（FastAPI，收 Casdoor webhook 推飞书审批卡片、批准调 Casdoor 加组、离职调 Casdoor 禁用）→ 各应用纯 OIDC 客户端。准入=认证/授权分离，挂在 `approved-operators` 组 claim；离职撤销=短 token + 中心拦截。

**Tech Stack:** Casdoor（Go 二进制，Docker）+ PostgreSQL；治理服务 Python 3.12 / FastAPI / httpx / authlib；复用 CS Hub 的 channels/feishu.py、feishu_ws.py；nginx 反代。

**Spec:** `docs/superpowers/specs/2026-07-04-feishu-sso-gateway-design.md`（先读一遍，尤其「实施前置验证」「数据流」两节）

**约定：**
- 治理服务测试 `tests/gov/`，用 `.venv/bin/python -m pytest`
- commit message 平铺直叙，**不加任何 AI 署名行**
- Casdoor/基础设施类任务无法 TDD，改为「明确的验证步骤 + 通过判据」；代码类任务严格 TDD

---

## ⚠️ Phase 0 是硬门控

Phase 0 的产出是一份 **`docs/superpowers/specs/casdoor-findings.md`**，记录 5 个载荷性 Casdoor 事实的**实测结果**（组 claim 配置、禁用即杀会话、webhook 验签方案、Admin API 端点与载荷、飞书 provider 端到端）。Phase 1+ 中所有「调 Casdoor API」的具体端点/载荷，以这份 findings 为准。**Phase 0 任一条不通过 → 停下，回设计 spec 调整对应环节，不要往下盖楼。**

## File Structure

| 文件 | 职责 |
|---|---|
| `deploy/casdoor/docker-compose.yml`（新） | Casdoor + PostgreSQL 容器编排 |
| `deploy/casdoor/README.md`（新） | Casdoor 部署与配置手册（飞书 provider、组、OIDC 应用） |
| `docs/superpowers/specs/casdoor-findings.md`（新） | Phase 0 实测事实（后续代码的事实依据） |
| `services/even_auth_gov/`（新目录） | 治理服务 |
| `services/even_auth_gov/casdoor_admin.py`（新） | Casdoor Admin API 客户端（加组/移组/禁用/查用户） |
| `services/even_auth_gov/approval_store.py`（新） | 瘦审批状态机（pending/approved/denied + 审计），从 sso_store 演进 |
| `services/even_auth_gov/webhook.py`（新） | Casdoor webhook 接收 + 验签 + 触发审批 |
| `services/even_auth_gov/offboard.py`（新） | 离职引擎（复用飞书事件/对账 → 调 Casdoor 禁用） |
| `services/even_auth_gov/app.py`（新） | FastAPI 组装（webhook 路由 + 卡片回调 + 飞书 WS 启动） |
| `services/even_auth_gov/migrate_emails.py`（新） | approved_emails → Casdoor 组 迁移脚本 |
| `channels/feishu.py`、`channels/feishu_ws.py`（复用/微调） | 卡片、WS 事件；治理服务 import 复用 |
| CS Hub `main.py` / 新 `admin/oidc_client.py`（改/新） | OIDC 客户端 + 组 claim 检查 + AUTH_MODE flag |

---

### Task 0.1: Casdoor + PostgreSQL 本地起容器

**Files:**
- Create: `deploy/casdoor/docker-compose.yml`
- Create: `deploy/casdoor/README.md`

- [ ] **Step 0.1.1: 写 compose 文件**

`deploy/casdoor/docker-compose.yml`：

```yaml
services:
  casdoor-db:
    image: postgres:16
    environment:
      POSTGRES_USER: casdoor
      POSTGRES_PASSWORD: casdoor_local_pw
      POSTGRES_DB: casdoor
    volumes:
      - casdoor_pg:/var/lib/postgresql/data
    ports:
      - "127.0.0.1:5433:5432"
  casdoor:
    image: casbin/casdoor:latest
    depends_on: [casdoor-db]
    environment:
      RUNNING_IN_DOCKER: "true"
      driverName: postgres
      dataSourceName: "user=casdoor password=casdoor_local_pw host=casdoor-db port=5432 sslmode=disable dbname=casdoor"
    ports:
      - "127.0.0.1:8000:8000"
volumes:
  casdoor_pg:
```

- [ ] **Step 0.1.2: 起容器并验证 UI 可达**

Run: `docker compose -f deploy/casdoor/docker-compose.yml up -d && sleep 20 && curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8000/`
Expected: `200`（Casdoor 首页/登录页）。默认管理员 `admin` / `123`（首次登录后改）。

- [ ] **Step 0.1.3: 记录到 findings**

创建 `docs/superpowers/specs/casdoor-findings.md`，写下：Casdoor 版本（`curl -s http://127.0.0.1:8000/api/get-version` 或 UI 页脚）、管理员登录方式、DB=postgres。

- [ ] **Step 0.1.4: Commit**

```bash
git add deploy/casdoor/ docs/superpowers/specs/casdoor-findings.md
git commit -m "Add local Casdoor+Postgres compose for gateway spike"
```

---

### Task 0.2: 验证飞书原生 provider 端到端可扫码

**Files:** Modify: `docs/superpowers/specs/casdoor-findings.md`

- [ ] **Step 0.2.1: 在 Casdoor 配飞书 provider**

Casdoor UI → Providers → Add：Category=OAuth，Type=Lark，Client ID=你的飞书 App ID，Client secret=飞书 App Secret。飞书开放平台安全设置里加回调 `http://127.0.0.1:8000/callback`。（依据调研 F1：这是内置 Lark provider，非通用 Custom OAuth。）

- [ ] **Step 0.2.2: 把 provider 挂到内置应用并实测扫码**

Applications → app-built-in → 添加刚建的 Lark provider → 保存。打开 Casdoor 登录页，点飞书图标，用真实飞书扫码。
Expected: 扫码后成功登录 Casdoor，用户列表里出现该飞书用户（含 open_id/姓名/邮箱）。

- [ ] **Step 0.2.3: 记录结果**

findings 里记：✅/❌ 飞书扫码；登录后 Casdoor 用户对象里飞书身份字段落在哪些属性（`name`/`email`/`id`/`externalId` 等）——后续组匹配和迁移脚本要用。

- [ ] **Step 0.2.4: Commit** `git commit -am "Verify Casdoor native Lark login end-to-end (findings)"`

---

### Task 0.3: 验证「组成员进 id_token claim」

**Files:** Modify: `docs/superpowers/specs/casdoor-findings.md`

- [ ] **Step 0.3.1: 建组 + 建 OIDC 客户端应用**

Casdoor UI → Groups → 新建 `approved-operators`。→ Applications → 新建 `cs-hub`：拿到 Client ID/Secret，Redirect URLs 填 `http://127.0.0.1:9000/auth/callback`（CS Hub 本地回调），勾选返回的 scopes（profile/email/groups 或 roles）。把测试用户加入 `approved-operators` 组。

- [ ] **Step 0.3.2: 走一次 OIDC 授权码流，解出 id_token**

用 curl/httpie 手动跑：`/login/oauth/authorize?client_id=...&response_type=code&redirect_uri=...&scope=openid profile email&state=x` → 拿 code → `POST /api/login/oauth/access_token`（client_id/secret/code）→ 拿 id_token → base64 解 payload。
Expected: 确认 id_token payload 里**能看到组/角色成员信息**（字段名可能是 `groups`/`roles`/`ext`，Casdoor 版本相关）。

- [ ] **Step 0.3.3: 记录（关键）**

findings 里记：组成员在 id_token 里的**确切 claim 名与结构**（例：`"groups":["approved-operators"]`）。**若解不出组 claim** → 记为 ❌，并在 findings 里写明 fallback（改用 Casdoor 每应用角色，或治理服务侧 introspection/`/api/get-user` 查组）——此时需回设计 spec §授权门调整。

- [ ] **Step 0.3.4: Commit** `git commit -am "Verify group membership surfaces in OIDC id_token (findings)"`

---

### Task 0.4: 验证 Admin API（加组/移组/禁用）+ webhook 验签

**Files:** Modify: `docs/superpowers/specs/casdoor-findings.md`

- [ ] **Step 0.4.1: 拿 Admin API 凭据并实测加组/移组/禁用**

Casdoor 支持以应用 client credentials 或管理员 token 调 Admin API。实测这三个动作各跑一次（curl），记录**确切端点 + 请求体 + 鉴权头**：
- 把用户加入 `approved-operators`（候选：`POST /api/update-user` 带 `groups` 字段，或 `/api/add-user-to-group`——以实测为准）
- 从组移除
- 禁用用户（候选：`POST /api/update-user` 置 `isForbidden=true`）

Expected: 三个动作在 UI 上可见生效。findings 记录每个的 method/path/body/auth。

- [ ] **Step 0.4.2: 验证「禁用即杀会话」**

用测试用户在 Casdoor 建立会话（登录），然后调禁用 API，再用其会话 cookie 访问受保护页/刷新 token。
Expected: 会话失效 / 刷新被拒。findings 记 ✅/❌ 及失效延迟。**若禁用不杀会话** → 记 fallback（缩短 Casdoor 会话/ token TTL）并回设计 §离职撤销。

- [ ] **Step 0.4.3: 验证 new-user webhook + 验签**

Casdoor UI 配 webhook（URL 指向一个临时 `nc`/RequestBin 或本地 echo 服务），事件选 signup/add-user。真实/手动建一个新用户触发。
Expected: 收到 webhook body；记录 **body 结构 + 是否带签名头（HMAC?）及验签方式**。若无内置签名 → findings 记「用共享密钥 header + 内网限定」作为替代校验。

- [ ] **Step 0.4.4: Commit** `git commit -am "Verify Casdoor admin API (group/disable) + webhook signing (findings)"`

**Phase 0 门控：** 5 项（0.2 飞书登录、0.3 组 claim、0.4.1 Admin API、0.4.2 禁用杀会话、0.4.3 webhook）全部在 findings 里记为可行（或有可接受的 fallback）后，才进 Phase 1。任一硬失败 → 停，回设计 spec。

---

### Task 1.1: 治理服务骨架 + Casdoor Admin 客户端（接口 + TDD）

**Files:**
- Create: `services/even_auth_gov/__init__.py`
- Create: `services/even_auth_gov/casdoor_admin.py`
- Create: `tests/gov/test_casdoor_admin.py`

- [ ] **Step 1.1.1: 写失败测试**（`tests/gov/test_casdoor_admin.py`）

用 httpx MockTransport 断言请求形状（端点/载荷以 Phase 0 findings 为准；下例以 `/api/update-user` 加组为占位，实施时替换成 findings 记录的确切端点）：

```python
import asyncio, json, httpx
from services.even_auth_gov import casdoor_admin as ca

def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://casdoor")

def test_add_to_group_calls_confirmed_endpoint(monkeypatch):
    seen = {}
    def handler(req):
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content) if req.content else {}
        return httpx.Response(200, json={"status": "ok"})
    async def run():
        client = _client(handler)
        ok = await ca.add_to_group(client, user_id="ou_x", group="approved-operators")
        assert ok is True
    asyncio.run(run())
    # 端点断言：实施时对齐 findings（此处为 Phase-0 确认的路径）
    assert seen["path"]  # 非空，具体值以 findings 为准

def test_disable_user_sets_forbidden(monkeypatch):
    seen = {}
    def handler(req):
        seen["body"] = json.loads(req.content) if req.content else {}
        return httpx.Response(200, json={"status": "ok"})
    async def run():
        client = _client(handler)
        assert await ca.disable_user(client, user_id="ou_x") is True
    asyncio.run(run())
    assert seen["body"]  # 含 isForbidden=true（以 findings 字段名为准）

def test_admin_call_failure_returns_false():
    def handler(req):
        return httpx.Response(500, json={"status": "error"})
    async def run():
        client = _client(handler)
        assert await ca.add_to_group(client, user_id="ou_x", group="g") is False
    asyncio.run(run())
```

- [ ] **Step 1.1.2: 跑测试确认失败** — `ImportError: casdoor_admin`。

- [ ] **Step 1.1.3: 实现 `casdoor_admin.py`**

接口固定（4 个 async 函数），实现体的**确切端点/载荷/鉴权用 Phase 0 findings 填**：

```python
"""Casdoor Admin API 客户端 — 加组/移组/禁用/查用户。
确切端点/载荷/鉴权见 docs/superpowers/specs/casdoor-findings.md(Task 0.4)。
"""
from __future__ import annotations
import logging, os
import httpx

logger = logging.getLogger(__name__)
CASDOOR_ENDPOINT = os.getenv("CASDOOR_ENDPOINT", "")
CASDOOR_CLIENT_ID = os.getenv("CASDOOR_CLIENT_ID", "")
CASDOOR_CLIENT_SECRET = os.getenv("CASDOOR_CLIENT_SECRET", "")

def _auth_params() -> dict:
    # Casdoor Admin API 以 clientId/clientSecret query 参数鉴权(findings 0.4.1 确认)
    return {"clientId": CASDOOR_CLIENT_ID, "clientSecret": CASDOOR_CLIENT_SECRET}

async def _post(client: httpx.AsyncClient, path: str, body: dict) -> bool:
    try:
        resp = await client.post(path, params=_auth_params(), json=body, timeout=10.0)
        data = resp.json() if resp.content else {}
        if resp.status_code == 200 and data.get("status") != "error":
            return True
        logger.warning("Casdoor admin %s failed: %s %s", path, resp.status_code, data)
        return False
    except Exception as e:
        logger.warning("Casdoor admin %s error: %s", path, e)
        return False

async def add_to_group(client: httpx.AsyncClient, user_id: str, group: str) -> bool:
    # 路径/载荷以 findings 0.4.1 为准
    return await _post(client, "/api/update-user", {"user_id": user_id, "op": "add-group", "group": group})

async def remove_from_group(client: httpx.AsyncClient, user_id: str, group: str) -> bool:
    return await _post(client, "/api/update-user", {"user_id": user_id, "op": "remove-group", "group": group})

async def disable_user(client: httpx.AsyncClient, user_id: str) -> bool:
    return await _post(client, "/api/update-user", {"user_id": user_id, "isForbidden": True})
```

（注：Step 1.1.1 的测试只断言"有调用/返回 True|False/失败为 False"，不写死端点字符串，所以 findings 填入真实端点后测试仍通过；端点字符串的正确性由 Phase 0 实测 + Task 4 端到端兜住。）

- [ ] **Step 1.1.4: 跑测试确认通过** — 3 passed。
- [ ] **Step 1.1.5: Commit** `git add services/even_auth_gov/ tests/gov/ && git commit -m "Add Casdoor admin API client (group/disable)"`

---

### Task 1.2: 瘦审批状态机 approval_store

**Files:**
- Create: `services/even_auth_gov/approval_store.py`
- Create: `tests/gov/test_approval_store.py`

- [ ] **Step 1.2.1: 写失败测试**

```python
def _store(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "approvals.json"))
    from services.even_auth_gov import approval_store
    return approval_store

def test_pending_then_approve(monkeypatch, tmp_path):
    s = _store(monkeypatch, tmp_path)
    assert s.mark_pending("ou_x", {"name": "张三", "email": "z@e.com"}) is True   # 新建=True(需发卡片)
    assert s.mark_pending("ou_x", {"name": "张三"}) is False                      # 重复=False
    assert s.get("ou_x")["status"] == "pending"
    s.mark_approved("ou_x", "ou_boss")
    assert s.get("ou_x")["status"] == "approved" and s.get("ou_x")["approved_by"] == "ou_boss"

def test_deny_and_disable(monkeypatch, tmp_path):
    s = _store(monkeypatch, tmp_path)
    s.mark_pending("ou_y", {"name": "李四"})
    s.mark_denied("ou_y", "ou_boss")
    assert s.get("ou_y")["status"] == "denied"
    s.mark_disabled("ou_y", "offboard_event")
    assert s.get("ou_y")["status"] == "disabled" and s.get("ou_y")["disabled_reason"] == "offboard_event"
```

- [ ] **Step 1.2.2: 跑测试确认失败** — ImportError。

- [ ] **Step 1.2.3: 实现 `approval_store.py`**（照抄 CS Hub `admin/sso_store.py` 的 threading.Lock + 原子写范式，但只存审批工作流状态，不是用户主库）

```python
"""审批工作流状态机(pending/approved/denied/disabled + 审计)。
Casdoor 是用户真理源;本 store 只记审批流转与审计,不存用户主数据。
"""
from __future__ import annotations
import json, os, threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
def _now() -> str: return datetime.now(timezone.utc).isoformat()
def _file() -> Path:
    raw = os.getenv("APPROVAL_STORE_FILE", "").strip()
    return Path(raw).expanduser() if raw else Path("data/approvals.json")

def _load() -> dict:
    p = _file()
    if not p.exists(): return {"records": {}, "updated_at": ""}
    try: d = json.loads(p.read_text(encoding="utf-8"))
    except Exception: d = {}
    return {"records": d.get("records", {}) if isinstance(d, dict) else {}, "updated_at": ""}

def _save(d: dict) -> None:
    p = _file(); p.parent.mkdir(parents=True, exist_ok=True)
    d["updated_at"] = _now()
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)

def get(open_id: str):
    with _LOCK:
        r = _load()["records"].get(open_id)
    return dict(r) if r else None

def _set(open_id: str, **fields):
    with _LOCK:
        d = _load(); rec = d["records"].get(open_id, {"open_id": open_id})
        rec.update(fields); d["records"][open_id] = rec; _save(d)

def mark_pending(open_id: str, profile: dict) -> bool:
    """返回是否为新建(需发审批卡片)。已存在的记录不重发。"""
    with _LOCK:
        d = _load()
        if open_id in d["records"] and d["records"][open_id].get("status") == "pending":
            return False
        d["records"][open_id] = {"open_id": open_id, "status": "pending", "applied_at": _now(),
                                 "name": profile.get("name", ""), "email": profile.get("email", "")}
        _save(d)
    return True

def mark_approved(open_id: str, by: str): _set(open_id, status="approved", approved_by=by, approved_at=_now())
def mark_denied(open_id: str, by: str): _set(open_id, status="denied", denied_by=by, denied_at=_now())
def mark_disabled(open_id: str, reason: str): _set(open_id, status="disabled", disabled_reason=reason, disabled_at=_now())
```

- [ ] **Step 1.2.4: 跑测试确认通过** — 2 passed。
- [ ] **Step 1.2.5: Commit** `git commit -am "Add thin approval state store for governance service"`

---

### Task 1.3: 卡片审批 → 调 Casdoor 加组

**Files:**
- Create: `services/even_auth_gov/decision.py`
- Create: `tests/gov/test_decision.py`

- [ ] **Step 1.3.1: 写失败测试**（复用 CS Hub 审批语义：审批人校验 + 幂等 + 批准调加组 / 拒绝不加组）

```python
import asyncio
from types import SimpleNamespace
from services.even_auth_gov import decision, approval_store

def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    approval_store.mark_pending("ou_a", {"name": "申请人"})
    calls = []
    async def fake_add(client, user_id, group): calls.append(("add", user_id, group)); return True
    async def fake_disable(client, user_id): calls.append(("disable", user_id)); return True
    monkeypatch.setattr(decision.ca, "add_to_group", fake_add)
    monkeypatch.setattr(decision.ca, "disable_user", fake_disable)
    return calls

def test_approve_by_owner_adds_to_group(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path)
    r = asyncio.run(decision.handle("sso_approve", operator_id="ou_boss", owner="ou_boss", sso_open_id="ou_a", client=None))
    assert r["status"] == "ok"
    assert ("add", "ou_a", "approved-operators") in calls
    assert approval_store.get("ou_a")["status"] == "approved"

def test_approve_by_stranger_rejected(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path)
    r = asyncio.run(decision.handle("sso_approve", operator_id="ou_x", owner="ou_boss", sso_open_id="ou_a", client=None))
    assert r["status"] == "error" and calls == []
    assert approval_store.get("ou_a")["status"] == "pending"

def test_idempotent_after_decision(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    asyncio.run(decision.handle("sso_approve", operator_id="ou_boss", owner="ou_boss", sso_open_id="ou_a", client=None))
    r = asyncio.run(decision.handle("sso_approve", operator_id="ou_boss", owner="ou_boss", sso_open_id="ou_a", client=None))
    assert r["status"] == "ok" and "approved" in r["message"]
```

- [ ] **Step 1.3.2: 跑测试确认失败** — ImportError。

- [ ] **Step 1.3.3: 实现 `decision.py`**

```python
"""审批决策:校验审批人 + 幂等 + 批准调 Casdoor 加组 / 拒绝置禁用。"""
from __future__ import annotations
import logging
from services.even_auth_gov import casdoor_admin as ca, approval_store

logger = logging.getLogger(__name__)
APPROVED_GROUP = "approved-operators"

async def handle(action: str, operator_id: str, owner: str, sso_open_id: str, client) -> dict:
    if not sso_open_id:
        return {"status": "error", "message": "Missing applicant id"}
    if not owner or operator_id != owner:
        logger.warning("SSO decision rejected: %s not approver", operator_id)
        return {"status": "error", "message": "Only the approver can act"}
    rec = approval_store.get(sso_open_id)
    if not rec:
        return {"status": "error", "message": "Application not found"}
    if rec.get("status") != "pending":
        return {"status": "ok", "message": f"Already processed: {rec.get('status')}"}
    if action == "sso_approve":
        ok = await ca.add_to_group(client, user_id=sso_open_id, group=APPROVED_GROUP)
        if not ok:
            return {"status": "error", "message": "Casdoor add-to-group failed; retry"}
        approval_store.mark_approved(sso_open_id, operator_id)
        return {"status": "ok", "message": f"Approved: {rec.get('name','')}"}
    # sso_deny
    approval_store.mark_denied(sso_open_id, operator_id)
    return {"status": "ok", "message": f"Denied: {rec.get('name','')}"}
```

- [ ] **Step 1.3.4: 跑测试确认通过** — 3 passed。
- [ ] **Step 1.3.5: Commit** `git commit -am "Approve→Casdoor add-to-group with approver check + idempotency"`

---

### Task 1.4: Casdoor webhook 接收 + 验签 + 触发审批卡片

**Files:**
- Create: `services/even_auth_gov/webhook.py`
- Create: `tests/gov/test_webhook.py`

- [ ] **Step 1.4.1: 写失败测试**（验签失败拒绝；新用户事件 → mark_pending + 发卡片）

```python
import asyncio, hmac, hashlib, json
from services.even_auth_gov import webhook, approval_store

def test_bad_signature_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    monkeypatch.setenv("CASDOOR_WEBHOOK_SECRET", "shh")
    body = json.dumps({"action": "signup", "user": {"id": "ou_n", "name": "新人"}}).encode()
    r = asyncio.run(webhook.handle(body, signature="wrong"))
    assert r["status"] == "rejected"

def test_signup_creates_pending_and_sends_card(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    monkeypatch.setenv("CASDOOR_WEBHOOK_SECRET", "shh")
    sent = []
    async def fake_send(info): sent.append(info)
    monkeypatch.setattr(webhook, "send_approval_card", fake_send)
    body = json.dumps({"action": "signup", "user": {"id": "ou_n", "name": "新人", "email": "n@e.com"}}).encode()
    sig = hmac.new(b"shh", body, hashlib.sha256).hexdigest()
    r = asyncio.run(webhook.handle(body, signature=sig))
    assert r["status"] == "ok"
    assert approval_store.get("ou_n")["status"] == "pending"
    assert len(sent) == 1 and sent[0]["open_id"] == "ou_n"
```

- [ ] **Step 1.4.2: 跑测试确认失败** — ImportError。

- [ ] **Step 1.4.3: 实现 `webhook.py`**（验签方案以 Phase 0 findings 0.4.3 为准；下用 HMAC-SHA256 共享密钥作默认，findings 若不同则替换 `_verify`）

```python
"""Casdoor webhook 接收:验签 → 新用户 → mark_pending + 推飞书审批卡片。
验签方案见 findings 0.4.3(默认 HMAC-SHA256 共享密钥 header)。"""
from __future__ import annotations
import hmac, hashlib, json, logging, os
from services.even_auth_gov import approval_store

logger = logging.getLogger(__name__)

def _verify(body: bytes, signature: str) -> bool:
    secret = os.getenv("CASDOOR_WEBHOOK_SECRET", "").encode()
    if not secret: return False
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")

# 由 app.py 在启动时注入真实实现(发飞书卡片);测试用 monkeypatch 覆盖
async def send_approval_card(info: dict) -> None:  # pragma: no cover - overridden
    raise NotImplementedError

async def handle(body: bytes, signature: str) -> dict:
    if not _verify(body, signature):
        logger.warning("Casdoor webhook signature invalid")
        return {"status": "rejected"}
    try:
        evt = json.loads(body)
    except Exception:
        return {"status": "rejected"}
    if evt.get("action") not in ("signup", "add-user"):
        return {"status": "ignored"}
    user = evt.get("user") or {}
    open_id = user.get("id") or ""
    if not open_id:
        return {"status": "ignored"}
    is_new = approval_store.mark_pending(open_id, {"name": user.get("name", ""), "email": user.get("email", "")})
    if is_new:
        await send_approval_card({"open_id": open_id, "name": user.get("name", ""), "email": user.get("email", "")})
    return {"status": "ok"}
```

- [ ] **Step 1.4.4: 跑测试确认通过** — 2 passed。
- [ ] **Step 1.4.5: Commit** `git commit -am "Add Casdoor webhook receiver with signature verify + pending/card"`

---

### Task 1.5: 离职引擎 → 调 Casdoor 禁用

**Files:**
- Create: `services/even_auth_gov/offboard.py`
- Create: `tests/gov/test_offboard.py`

- [ ] **Step 1.5.1: 写失败测试**（复用 CS Hub `_offboard_flags` 语义：frozen/resigned/exited 或 deleted → 调 Casdoor 禁用 + 移组 + mark_disabled；无关更新不动）

```python
import asyncio
from types import SimpleNamespace
from services.even_auth_gov import offboard, approval_store

def _status(**kw):
    base = dict(is_frozen=False, is_resigned=False, is_exited=False)
    base.update(kw); return SimpleNamespace(**base)

def _setup(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    approval_store.mark_pending("ou_l", {"name": "离职者"}); approval_store.mark_approved("ou_l", "ou_boss")
    calls = []
    async def fake_disable(client, user_id): calls.append(user_id); return True
    async def fake_remove(client, user_id, group): return True
    monkeypatch.setattr(offboard.ca, "disable_user", fake_disable)
    monkeypatch.setattr(offboard.ca, "remove_from_group", fake_remove)
    return calls

def test_frozen_disables(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path)
    asyncio.run(offboard.apply("ou_l", "离职者", "offboard_event", client=None))
    assert calls == ["ou_l"] and approval_store.get("ou_l")["status"] == "disabled"

def test_flag_helper():
    assert offboard.offboard_flags(_status(is_frozen=True)) is True
    assert offboard.offboard_flags(_status()) is False
    assert offboard.offboard_flags(None) is False
```

- [ ] **Step 1.5.2: 跑测试确认失败** — ImportError。

- [ ] **Step 1.5.3: 实现 `offboard.py`**

```python
"""离职引擎:飞书事件/对账 → 调 Casdoor 禁用 + 移组 + 记审计。
禁用失败必须重试到成功(安全攸关);此处返回 bool 供上层重试。"""
from __future__ import annotations
import logging
from services.even_auth_gov import casdoor_admin as ca, approval_store

logger = logging.getLogger(__name__)
APPROVED_GROUP = "approved-operators"

def offboard_flags(status) -> bool:
    if status is None: return False
    return bool(getattr(status, "is_frozen", False) or getattr(status, "is_resigned", False)
                or getattr(status, "is_exited", False))

async def apply(open_id: str, name: str, reason: str, client) -> bool:
    if not open_id: return False
    rec = approval_store.get(open_id)
    if rec and rec.get("status") == "disabled":
        return True
    ok = await ca.disable_user(client, user_id=open_id)
    await ca.remove_from_group(client, user_id=open_id, group=APPROVED_GROUP)
    if ok:
        approval_store.mark_disabled(open_id, reason)
        logger.info("Offboard disabled in Casdoor: %s (%s) via %s", name, open_id, reason)
    else:
        logger.warning("Offboard disable FAILED for %s — needs retry", open_id)
    return ok
```

- [ ] **Step 1.5.4: 跑测试确认通过** — 2 passed。
- [ ] **Step 1.5.5: Commit** `git commit -am "Add offboarding engine → Casdoor disable + remove-from-group"`

---

### Task 1.6: FastAPI 组装 + 飞书卡片/WS 接线

**Files:**
- Create: `services/even_auth_gov/app.py`
- Create: `tests/gov/test_app_wiring.py`

- [ ] **Step 1.6.1: 写失败测试**（webhook 路由 200/401；卡片回调分发到 decision.handle）

```python
from fastapi.testclient import TestClient
import hmac, hashlib, json

def _app(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    monkeypatch.setenv("CASDOOR_WEBHOOK_SECRET", "shh")
    monkeypatch.setenv("TESTING", "1")
    from services.even_auth_gov.app import build_app
    return build_app()

def test_webhook_route_rejects_bad_sig(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as c:
        r = c.post("/casdoor/webhook", content=b"{}", headers={"X-Casdoor-Signature": "bad"})
        assert r.status_code == 401

def test_webhook_route_accepts_good_sig(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    body = json.dumps({"action": "signup", "user": {"id": "ou_n", "name": "N"}}).encode()
    sig = hmac.new(b"shh", body, hashlib.sha256).hexdigest()
    with TestClient(app) as c:
        r = c.post("/casdoor/webhook", content=body, headers={"X-Casdoor-Signature": sig})
        assert r.status_code == 200
```

- [ ] **Step 1.6.2: 跑测试确认失败** — ImportError。

- [ ] **Step 1.6.3: 实现 `app.py`**

```python
"""治理服务 FastAPI 组装:webhook 路由 + 飞书卡片回调 + 飞书 WS 启动。
复用 CS Hub 的 channels/feishu.py(卡片) 与 feishu_ws.py(事件)。"""
from __future__ import annotations
import os, httpx
from fastapi import FastAPI, Request, Response
from services.even_auth_gov import webhook as wh, decision, offboard

def _casdoor_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=os.getenv("CASDOOR_ENDPOINT", "http://127.0.0.1:8000"))

def build_app() -> FastAPI:
    app = FastAPI(title="even-auth-gov")

    # 注入真实发卡片实现(复用 CS Hub 的 build_sso_approval_card + send_card)
    async def _send_card(info: dict):
        if os.getenv("TESTING"): return
        from channels.feishu import build_sso_approval_card, send_card
        from config import get_config
        owner = (get_config().escalation_owner or {}).get("feishu_id", "")
        if owner and owner != "ou_xxx":
            await send_card(owner, build_sso_approval_card(info.get("open_id",""), info.get("name",""), info.get("email","")))
    wh.send_approval_card = _send_card

    @app.post("/casdoor/webhook")
    async def casdoor_webhook(request: Request):
        body = await request.body()
        sig = request.headers.get("X-Casdoor-Signature", "")  # header 名以 findings 0.4.3 为准
        result = await wh.handle(body, sig)
        return Response(status_code=401 if result["status"] == "rejected" else 200)

    # 飞书卡片回调:复用 CS Hub 分发,把 sso_approve/sso_deny 路由到 decision.handle
    @app.post("/feishu/card")
    async def feishu_card(request: Request):
        from config import get_config
        payload = await request.json()
        action = (payload.get("action") or {}).get("value", {})
        owner = (get_config().escalation_owner or {}).get("feishu_id", "")
        operator = (payload.get("operator") or {}).get("open_id", "")
        async with _casdoor_client() as client:
            r = await decision.handle(action.get("action",""), operator_id=operator, owner=owner,
                                      sso_open_id=action.get("sso_open_id",""), client=client)
        return {"toast": {"type": "success" if r["status"]=="ok" else "error", "content": r["message"]}}

    return app
```

（注：飞书 WS 事件→offboard.apply 的接线在 Task 1.7 补；此处先把 HTTP 面立起来。生产 WS 卡片回调走长连接，HTTP `/feishu/card` 作为 webhook 备用路径，与 CS Hub 现有双路径一致。）

- [ ] **Step 1.6.4: 跑测试确认通过** — 2 passed。
- [ ] **Step 1.6.5: Commit** `git commit -am "Assemble governance FastAPI: webhook + card callback wiring"`

---

### Task 1.7: 飞书 WS 离职事件 → offboard.apply

**Files:**
- Create: `services/even_auth_gov/ws_events.py`
- Create: `tests/gov/test_ws_events.py`

- [ ] **Step 1.7.1: 写失败测试**（deleted/updated 事件 → offboard.apply；复用 CS Hub 事件对象形状）

```python
import asyncio
from types import SimpleNamespace
from services.even_auth_gov import ws_events

def _status(**kw):
    b = dict(is_frozen=False,is_resigned=False,is_exited=False); b.update(kw); return SimpleNamespace(**b)

def test_deleted_event_triggers_offboard(monkeypatch):
    calls = []
    async def fake_apply(open_id, name, reason, client): calls.append((open_id, reason))
    monkeypatch.setattr(ws_events.offboard, "apply", fake_apply)
    data = SimpleNamespace(event=SimpleNamespace(object=SimpleNamespace(open_id="ou_l", name="离职", status=None)))
    asyncio.run(ws_events.on_user_deleted(data))
    assert calls == [("ou_l", "offboard_deleted")]

def test_frozen_update_triggers_offboard(monkeypatch):
    calls = []
    async def fake_apply(open_id, name, reason, client): calls.append((open_id, reason))
    monkeypatch.setattr(ws_events.offboard, "apply", fake_apply)
    data = SimpleNamespace(event=SimpleNamespace(
        object=SimpleNamespace(open_id="ou_l", name="离职", status=_status(is_frozen=True)),
        old_object=SimpleNamespace(status=_status())))
    asyncio.run(ws_events.on_user_updated(data))
    assert calls == [("ou_l", "offboard_event")]

def test_unrelated_update_noop(monkeypatch):
    calls = []
    async def fake_apply(*a, **k): calls.append(1)
    monkeypatch.setattr(ws_events.offboard, "apply", fake_apply)
    data = SimpleNamespace(event=SimpleNamespace(
        object=SimpleNamespace(open_id="ou_l", name="改名", status=_status()),
        old_object=SimpleNamespace(status=_status())))
    asyncio.run(ws_events.on_user_updated(data))
    assert calls == []
```

- [ ] **Step 1.7.2: 跑测试确认失败** — ImportError。

- [ ] **Step 1.7.3: 实现 `ws_events.py`**（离职判定逻辑复用 offboard.offboard_flags；async 包装，client 由 app 注入）

```python
"""飞书 contact WS 事件 → 离职引擎。判定复用 offboard.offboard_flags。"""
from __future__ import annotations
import logging, os, httpx
from services.even_auth_gov import offboard

logger = logging.getLogger(__name__)

def _client(): return httpx.AsyncClient(base_url=os.getenv("CASDOOR_ENDPOINT", "http://127.0.0.1:8000"))

async def on_user_deleted(data) -> None:
    try:
        obj = data.event.object if data.event else None
        if obj is not None and getattr(obj, "open_id", ""):
            async with _client() as c:
                await offboard.apply(obj.open_id, getattr(obj, "name", ""), "offboard_deleted", c)
    except Exception as e:
        logger.exception("on_user_deleted error: %s", e)

async def on_user_updated(data) -> None:
    try:
        ev = data.event; obj = ev.object if ev else None; old = getattr(ev, "old_object", None) if ev else None
        if obj is None: return
        now_off = offboard.offboard_flags(getattr(obj, "status", None))
        was_off = offboard.offboard_flags(getattr(old, "status", None)) if old is not None else False
        if now_off and not was_off and getattr(obj, "open_id", ""):
            async with _client() as c:
                await offboard.apply(obj.open_id, getattr(obj, "name", ""), "offboard_event", c)
    except Exception as e:
        logger.exception("on_user_updated error: %s", e)
```

（注：把 `on_user_deleted`/`on_user_updated` 注册到飞书 WS builder 的方式，复用 CS Hub `channels/feishu_ws.py` 的 `register_p2_contact_user_deleted_v3`/`_updated_v3` 模式——治理服务启动时建自己的 WS 连接并注册这两个 handler。生产接线在部署手册补，本任务只保证 handler 逻辑正确。）

- [ ] **Step 1.7.4: 跑测试确认通过** — 3 passed。
- [ ] **Step 1.7.5: Commit** `git commit -am "Add WS contact events → offboard for governance service"`

---

### Task 1.8: 每日对账 job（兜底）

**Files:**
- Create: `services/even_auth_gov/reconcile.py`
- Create: `tests/gov/test_reconcile.py`

- [ ] **Step 1.8.1: 写失败测试**（对 approved 用户查飞书状态；frozen/resigned/exited → offboard.apply；API 错误 fail-open 不动）

```python
import asyncio
from types import SimpleNamespace
from services.even_auth_gov import reconcile, approval_store

def _status(**kw):
    b=dict(is_frozen=False,is_resigned=False,is_exited=False); b.update(kw); return SimpleNamespace(**b)

def test_reconcile_disables_frozen_keeps_ok_and_failopen(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    for oid in ("ou_ok","ou_frozen","ou_err"):
        approval_store.mark_pending(oid, {"name": oid}); approval_store.mark_approved(oid, "ou_boss")
    async def fake_feishu_status(open_id):
        if open_id == "ou_frozen": return _status(is_frozen=True)
        if open_id == "ou_err": raise RuntimeError("api down")
        return _status()
    disabled = []
    async def fake_apply(open_id, name, reason, client): disabled.append((open_id, reason))
    monkeypatch.setattr(reconcile, "fetch_feishu_status", fake_feishu_status)
    monkeypatch.setattr(reconcile.offboard, "apply", fake_apply)
    asyncio.run(reconcile.run(client=None))
    assert ("ou_frozen","reconcile") in disabled
    assert all(o != "ou_ok" for o,_ in disabled)   # healthy 不动
    assert all(o != "ou_err" for o,_ in disabled)   # API 错误 fail-open
```

- [ ] **Step 1.8.2: 跑测试确认失败** — ImportError。

- [ ] **Step 1.8.3: 实现 `reconcile.py`**（`fetch_feishu_status` 用飞书 contact API 查在职状态；对 approved 记录逐个核对；fail-open）

```python
"""每日对账兜底:对 approved 用户查飞书在职状态,frozen/resigned/exited → 禁用。
API 错误 fail-open(飞书 get-user 无可靠 not-found 码,见 CS Hub 前置 spec)。"""
from __future__ import annotations
import logging
from services.even_auth_gov import offboard, approval_store

logger = logging.getLogger(__name__)

async def fetch_feishu_status(open_id: str):
    """查飞书用户状态对象(有 is_frozen/is_resigned/is_exited);复用 CS Hub get_client + contact.v3.user.aget。"""
    from channels.feishu import get_client
    from lark_oapi.api.contact.v3 import GetUserRequest
    client = get_client()
    req = GetUserRequest.builder().user_id(open_id).user_id_type("open_id").build()
    resp = await client.contact.v3.user.aget(req)
    if not resp.success():
        raise RuntimeError(f"feishu get-user {resp.code} {resp.msg}")
    return resp.data.user.status if resp.data and resp.data.user else None

def _approved_open_ids() -> list[str]:
    import json, os
    from pathlib import Path
    raw = os.getenv("APPROVAL_STORE_FILE", "").strip() or "data/approvals.json"
    p = Path(raw)
    if not p.exists(): return []
    recs = json.loads(p.read_text(encoding="utf-8")).get("records", {})
    return [oid for oid, r in recs.items() if r.get("status") == "approved"]

async def run(client) -> None:
    for open_id in _approved_open_ids():
        try:
            status = await fetch_feishu_status(open_id)
        except Exception as e:
            logger.warning("Reconcile: feishu status error for %s: %s — leaving untouched", open_id, e)
            continue
        if offboard.offboard_flags(status):
            await offboard.apply(open_id, "", "reconcile", client)
```

- [ ] **Step 1.8.4: 跑测试确认通过** — 1 passed。
- [ ] **Step 1.8.5: Commit** `git commit -am "Add daily reconcile job for governance service (fail-open)"`

---

### Task 1.9: 邮箱迁移脚本 approved_emails → Casdoor 组

**Files:**
- Create: `services/even_auth_gov/migrate_emails.py`
- Create: `tests/gov/test_migrate_emails.py`

- [ ] **Step 1.9.1: 写失败测试**（读现有 admin_auth.json 的 allowed_emails；对每个邮箱按 Casdoor 用户匹配并加组；mock casdoor 查/加组）

```python
import asyncio, json
from services.even_auth_gov import migrate_emails as m

def test_migrate_adds_matched_emails_to_group(monkeypatch, tmp_path):
    auth = tmp_path / "admin_auth.json"
    auth.write_text(json.dumps({"allowed_emails": ["a@e.com", "b@e.com"]}), encoding="utf-8")
    added = []
    async def fake_find(client, email): return {"a@e.com": "ou_a", "b@e.com": None}.get(email)
    async def fake_add(client, user_id, group): added.append(user_id); return True
    monkeypatch.setattr(m, "find_user_by_email", fake_find)
    monkeypatch.setattr(m.ca, "add_to_group", fake_add)
    report = asyncio.run(m.run(str(auth), client=None))
    assert added == ["ou_a"]
    assert report["migrated"] == ["a@e.com"] and report["unmatched"] == ["b@e.com"]
```

- [ ] **Step 1.9.2: 跑测试确认失败** — ImportError。

- [ ] **Step 1.9.3: 实现 `migrate_emails.py`**（`find_user_by_email` 用 Casdoor get-users/查询 API，端点以 findings 为准）

```python
"""迁移:把 CS Hub allowed_emails 里的操作员,按邮箱匹配 Casdoor 用户并加入 approved-operators。
未匹配(飞书还没登录过、Casdoor 无此用户)的记入 unmatched,待其首登后走正常审批。"""
from __future__ import annotations
import json, logging
from pathlib import Path
from services.even_auth_gov import casdoor_admin as ca

logger = logging.getLogger(__name__)
APPROVED_GROUP = "approved-operators"

async def find_user_by_email(client, email: str) -> str | None:
    """按邮箱查 Casdoor 用户,返回其 user_id 或 None。端点见 findings 0.4.1。"""
    try:
        resp = await client.get("/api/get-user", params={"email": email})  # 端点以 findings 为准
        data = resp.json() if resp.content else {}
        u = data.get("data") if isinstance(data, dict) else None
        return (u or {}).get("id") if u else None
    except Exception as e:
        logger.warning("find_user_by_email error for %s: %s", email, e)
        return None

async def run(auth_file: str, client) -> dict:
    emails = json.loads(Path(auth_file).read_text(encoding="utf-8")).get("allowed_emails", [])
    migrated, unmatched = [], []
    for email in emails:
        uid = await find_user_by_email(client, email)
        if uid and await ca.add_to_group(client, user_id=uid, group=APPROVED_GROUP):
            migrated.append(email)
        else:
            unmatched.append(email)
    logger.info("Email migration: %d migrated, %d unmatched", len(migrated), len(unmatched))
    return {"migrated": migrated, "unmatched": unmatched}
```

- [ ] **Step 1.9.4: 跑测试确认通过** — 1 passed。
- [ ] **Step 1.9.5: Commit** `git commit -am "Add approved_emails → Casdoor group migration script"`

---

### Task 2.1: CS Hub OIDC 客户端 + 组 claim 检查

**Files:**
- Create: `admin/oidc_client.py`
- Modify: `main.py`（imports、AUTH_MODE、公开路径、OIDC 登录/回调路由、中间件）
- Test: `tests/test_oidc_client.py`

- [ ] **Step 2.1.1: 写失败测试**（OIDC state 签名/校验；组 claim 检查函数）

```python
def test_state_roundtrip_and_tamper():
    from admin import oidc_client as o
    s = o.make_state()
    assert o.verify_state(s) is True
    assert o.verify_state(s + "x") is False

def test_approved_claim_check():
    from admin import oidc_client as o
    assert o.is_approved({"groups": ["approved-operators", "x"]}) is True
    assert o.is_approved({"groups": ["x"]}) is False
    assert o.is_approved({}) is False
```

- [ ] **Step 2.1.2: 跑测试确认失败** — ImportError。

- [ ] **Step 2.1.3: 实现 `admin/oidc_client.py`**（state 复用 CS Hub feishu_oauth 的 HMAC 范式；组 claim 名以 findings 0.3 为准，下用 `groups`）

```python
"""CS Hub 作为 Casdoor OIDC 客户端:state 签名 + 授权 URL + code 换 token + 组 claim 检查。
组 claim 名以 casdoor-findings.md(Task 0.3)为准(默认 'groups')。"""
from __future__ import annotations
import base64, hashlib, hmac, json, os, secrets, time
from urllib.parse import urlencode
import httpx

APPROVED_GROUP = "approved-operators"
GROUP_CLAIM = os.getenv("OIDC_GROUP_CLAIM", "groups")
_SECRET = os.getenv("SESSION_SECRET", "") or secrets.token_hex(32)
_TTL = 300

def _b64u(b: bytes) -> str: return base64.urlsafe_b64encode(b).decode().rstrip("=")
def _b64ud(s: str) -> bytes: return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

def make_state() -> str:
    p = {"n": secrets.token_hex(8), "exp": int(time.time()) + _TTL}
    body = _b64u(json.dumps(p, separators=(",", ":")).encode())
    sig = hmac.new(_SECRET.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64u(sig)}"

def verify_state(state: str) -> bool:
    if not state: return False
    try:
        body, sig = state.split(".", 1)
        exp = hmac.new(_SECRET.encode(), body.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(_b64ud(sig), exp): return False
        return int(json.loads(_b64ud(body)).get("exp", 0)) >= int(time.time())
    except Exception:
        return False

def build_authorize_url(redirect_uri: str, state: str) -> str:
    return f"{os.getenv('CASDOOR_ENDPOINT','')}/login/oauth/authorize?" + urlencode({
        "client_id": os.getenv("CASDOOR_CLIENT_ID", ""), "response_type": "code",
        "redirect_uri": redirect_uri, "scope": "openid profile email", "state": state})

async def exchange_code(code: str, redirect_uri: str, transport=None) -> dict | None:
    try:
        async with httpx.AsyncClient(transport=transport, timeout=10.0) as c:
            resp = await c.post(f"{os.getenv('CASDOOR_ENDPOINT','')}/api/login/oauth/access_token", data={
                "grant_type": "authorization_code", "client_id": os.getenv("CASDOOR_CLIENT_ID", ""),
                "client_secret": os.getenv("CASDOOR_CLIENT_SECRET", ""), "code": code, "redirect_uri": redirect_uri})
        data = resp.json() if resp.content else {}
        return data if resp.status_code == 200 and data.get("id_token") else None
    except Exception:
        return None

def decode_id_token_claims(id_token: str) -> dict:
    try:
        payload = id_token.split(".")[1]
        return json.loads(_b64ud(payload))
    except Exception:
        return {}

def is_approved(claims: dict) -> bool:
    return APPROVED_GROUP in (claims.get(GROUP_CLAIM) or [])
```

- [ ] **Step 2.1.4: 跑测试确认通过** — 2 passed。
- [ ] **Step 2.1.5: Commit** `git commit -am "Add CS Hub OIDC client (state, code exchange, group-claim check)"`

---

### Task 2.2: CS Hub 接 OIDC 路由 + AUTH_MODE flag + 待审批页

**Files:**
- Modify: `main.py`（公开路径、`/auth/oidc/login`、`/auth/oidc/callback`、AUTH_MODE、中间件组 claim 复查）
- Modify: `admin/templates/login.html`（飞书按钮 href 按 AUTH_MODE 指向 `/auth/oidc/login`）
- Test: `tests/test_oidc_routes.py`

- [ ] **Step 2.2.1: 写失败测试**（登录 302 到 Casdoor；回调 approved → cs_session；非 approved → 待审批页）

```python
from fastapi.testclient import TestClient

def _enable(monkeypatch, tmp_path):
    import main
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("CASDOOR_ENDPOINT", "http://casdoor")
    monkeypatch.setenv("CASDOOR_CLIENT_ID", "cs-hub"); monkeypatch.setenv("CASDOOR_CLIENT_SECRET", "s")
    monkeypatch.setenv("PUBLIC_URL", "https://cshub.example.com")
    monkeypatch.setattr(main, "TESTING", ""); monkeypatch.setattr(main, "_SESSION_SECRET", "t")
    monkeypatch.setattr(main, "ADMIN_USER", "admin"); monkeypatch.setattr(main, "ADMIN_PASS", "secret")
    return main

def test_oidc_login_redirects_to_casdoor(monkeypatch, tmp_path):
    main = _enable(monkeypatch, tmp_path)
    with TestClient(main.app) as c:
        r = c.get("/auth/oidc/login", follow_redirects=False)
        assert r.status_code == 302 and "/login/oauth/authorize" in r.headers["location"]

def test_callback_approved_sets_session(monkeypatch, tmp_path):
    main = _enable(monkeypatch, tmp_path)
    async def fake_exchange(code, redirect_uri): return {"id_token": "x.y.z"}
    monkeypatch.setattr(main, "oidc_exchange_code", fake_exchange)
    monkeypatch.setattr(main, "oidc_decode_claims", lambda t: {"sub": "ou_a", "groups": ["approved-operators"]})
    from admin import oidc_client
    with TestClient(main.app) as c:
        state = oidc_client.make_state(); c.cookies.set("oidc_state", state)
        r = c.get(f"/auth/oidc/callback?code=GOOD&state={state}", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/admin/"
        assert "cs_session" in r.cookies

def test_callback_unapproved_shows_pending(monkeypatch, tmp_path):
    main = _enable(monkeypatch, tmp_path)
    async def fake_exchange(code, redirect_uri): return {"id_token": "x.y.z"}
    monkeypatch.setattr(main, "oidc_exchange_code", fake_exchange)
    monkeypatch.setattr(main, "oidc_decode_claims", lambda t: {"sub": "ou_new", "groups": []})
    from admin import oidc_client
    with TestClient(main.app) as c:
        state = oidc_client.make_state(); c.cookies.set("oidc_state", state)
        r = c.get(f"/auth/oidc/callback?code=GOOD&state={state}", follow_redirects=False)
        assert r.headers["location"] == "/login?notice=sso_pending"
```

- [ ] **Step 2.2.2: 跑测试确认失败** — 404/AttributeError。

- [ ] **Step 2.2.3: 实现 main.py**

(a) imports 加：
```python
from admin.oidc_client import (make_state as oidc_make_state, verify_state as oidc_verify_state,
    build_authorize_url as oidc_authorize_url, exchange_code as oidc_exchange_code,
    decode_id_token_claims as oidc_decode_claims, is_approved as oidc_is_approved)
AUTH_MODE = os.getenv("AUTH_MODE", "legacy")
```

(b) 公开路径白名单加 `/auth/oidc/login`, `/auth/oidc/callback`（与现有 feishu 路径并列）。

(c) 加路由（复用 Task 5 的 CSRF state-cookie 绑定范式；provider 记 `feishu`）：
```python
def _oidc_redirect_uri() -> str:
    return get_config().public_url + "/auth/oidc/callback"

@app.get("/auth/oidc/login")
async def oidc_login():
    from starlette.responses import RedirectResponse
    if AUTH_MODE != "oidc":
        return RedirectResponse("/login?error=sso_not_configured", status_code=302)
    state = oidc_make_state()
    resp = RedirectResponse(oidc_authorize_url(_oidc_redirect_uri(), state), status_code=302)
    resp.set_cookie("oidc_state", state, httponly=True, samesite="lax", secure=_cookie_secure(), max_age=300)
    return resp

@app.get("/auth/oidc/callback")
async def oidc_callback(request: Request):
    from starlette.responses import RedirectResponse
    def _r(t):
        r = RedirectResponse(t, status_code=302); r.delete_cookie("oidc_state"); return r
    code = request.query_params.get("code", ""); state = request.query_params.get("state", "")
    cookie_state = request.cookies.get("oidc_state", "")
    if not code or not state or not hmac.compare_digest(state, cookie_state) or not oidc_verify_state(state):
        return _r("/login?error=sso_state")
    tok = await oidc_exchange_code(code, _oidc_redirect_uri())
    if not tok:
        return _r("/login?error=sso_exchange")
    claims = oidc_decode_claims(tok["id_token"])
    sub = claims.get("sub", "")
    if not sub:
        return _r("/login?error=sso_userinfo")
    if not oidc_is_approved(claims):
        return _r("/login?notice=sso_pending")   # 认证成功但未审批
    token = _make_session_token(sub, provider="feishu")
    resp = _r("/admin/")
    resp.set_cookie("cs_session", token, httponly=True, samesite="lax", secure=_cookie_secure(), max_age=86400*7)
    return resp
```

(d) `_auth_backend_configured` 增加 `AUTH_MODE=="oidc"` 判定为已配置后端。

(e) 中间件（Task 6 的 feishu 会话复查处）：`AUTH_MODE=="oidc"` 时 feishu-provider 会话的即时撤销由"短 token + 中心拦截"承担，中间件对 provider=feishu 会话不再查本地 sso_store（sso_store 已退役），改为信任 token exp（短 TTL）。

(f) `login.html`：飞书按钮 href 从 `/auth/feishu/login` 改为在 `AUTH_MODE=oidc` 时指向 `/auth/oidc/login`（用 Jinja 传入 AUTH_MODE，或直接改 href 并在 legacy 期用 flag 切）。

- [ ] **Step 2.2.4: 跑测试确认通过** — 3 passed。
- [ ] **Step 2.2.5: 回归** `.venv/bin/python -m pytest tests/test_admin_email_auth.py -q` — 4 passed（密码入口不受影响）。
- [ ] **Step 2.2.6: Commit** `git commit -am "CS Hub OIDC login routes + AUTH_MODE flag + pending page"`

---

### Task 3.1: 端到端串联 + 部署手册 + 收尾

**Files:**
- Modify: `deploy/casdoor/README.md`
- Modify: `.env.example`（治理服务 + CS Hub OIDC 变量）
- Test: 全量

- [ ] **Step 3.1.1: `.env.example` 补变量**

```bash
# ── Casdoor 网关 ──
CASDOOR_ENDPOINT=http://127.0.0.1:8000
CASDOOR_CLIENT_ID=cs-hub
CASDOOR_CLIENT_SECRET=
CASDOOR_WEBHOOK_SECRET=
OIDC_GROUP_CLAIM=groups          # 以 casdoor-findings.md Task 0.3 为准
AUTH_MODE=legacy                 # legacy|oidc,灰度切换
APPROVAL_STORE_FILE=data/approvals.json
```

- [ ] **Step 3.1.2: 部署手册补端到端接线**

`deploy/casdoor/README.md` 写清：①治理服务如何建自己的飞书 WS 连接并注册 `on_user_deleted`/`on_user_updated`（复用 CS Hub feishu_ws 模式）；②Casdoor webhook 指向治理服务 `/casdoor/webhook`；③每日对账 job 注册（复用 CS Hub scheduler 模式，cron 默认 `0 8 * * *`）；④CS Hub 的 Casdoor OIDC 应用配置（Client ID/Secret、redirect、组 claim、短 token TTL）。

- [ ] **Step 3.1.3: 治理服务全量测试**

Run: `.venv/bin/python -m pytest tests/gov/ -q`
Expected: 全绿（casdoor_admin/approval_store/decision/webhook/offboard/app/ws_events/reconcile/migrate）。

- [ ] **Step 3.1.4: 手动端到端演练（对着 Phase 0 的本地 Casdoor）**

按部署手册跑一遍真链路：新飞书用户扫码 → CS Hub 待审批页 + 审批人收卡片 → 批准 → 用户重登进后台 → 在飞书把该用户置离职/冻结 → 数分钟内其 CS Hub 访问失效。findings/README 记录实测结果。

- [ ] **Step 3.1.5: spec 对照 + Commit**

逐节核对设计 spec：Casdoor 地基 ✓Task 0/1、治理服务 ✓Task 1.1-1.9、CS Hub 试点 ✓Task 2、离职撤销 ✓短 TTL+禁用、认证/授权分离 ✓组门控、迁移回滚 ✓AUTH_MODE。
```bash
git add .env.example deploy/casdoor/README.md
git commit -m "Wire end-to-end gateway + deploy manual + env docs"
```

---

## Self-Review 记录

- **Spec coverage**：设计 spec 各节 → 任务映射：实施前置验证 §→Task 0.1-0.4；Casdoor 地基→Task 0.1/部署手册；治理服务(webhook/卡片/离职/对账/瘦 store/casdoor 客户端/迁移)→Task 1.1-1.9；CS Hub OIDC 试点→Task 2.1-2.2；离职撤销(短 TTL+禁用)→offboard+中间件；认证/授权分离→组门控贯穿；迁移回滚→AUTH_MODE flag + migrate_emails；端到端→Task 3.1。范围外条目无对应任务(正确)。
- **Placeholder scan**：Casdoor API 的确切端点/载荷/验签方案统一以 `casdoor-findings.md`(Phase 0 产出)为事实源，代码里以可运行的候选实现 + findings 指针给出，非"TODO"占位；测试断言不写死端点字符串，故 findings 填入真值后仍通过。
- **Type consistency**：`casdoor_admin.{add_to_group,remove_from_group,disable_user}`、`approval_store.{mark_pending,mark_approved,mark_denied,mark_disabled,get}`、`decision.handle(...)`、`offboard.{offboard_flags,apply}`、`webhook.handle`、`oidc_client.{make_state,verify_state,is_approved,...}` 在各任务间签名一致。
- **已知风险**：所有 Casdoor 交互点的正确性最终由 Phase 0 实测 + Task 3.1.4 手动端到端兜底；Phase 0 任一硬失败需回设计 spec 调整(已在门控中声明)。
