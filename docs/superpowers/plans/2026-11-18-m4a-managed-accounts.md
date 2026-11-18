# 里程碑 4a · 受管的非飞书账号 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让外部合作方、没飞书的员工、服务账号用邮箱/密码登录,并且每个这样的账号都有"负责人 + 到期日"作为回收依据;没有依据的账号过不了审批闸门。

**Architecture:** 账号当前状态写在 Casdoor 用户 `properties` 的 `evauth_*` 键上(Casdoor 是唯一数据来源)。`accounts.py` 放纯规则和闸门,`account_lifecycle.py` 放会读写 Casdoor / 发飞书的操作(建号、确认、扫描、续期、连带停用、换负责人、设密码),变更历史追加到 `account_events.jsonl`。停用一律复用 `offboard.apply`(重试 + 告警 + 清准入组和角色)。

**Tech Stack:** Python 3.12、FastAPI、httpx(测试用 `httpx.MockTransport`)、APScheduler、lark-oapi、pytest。不加新依赖(时区用标准库 `zoneinfo`)。

**Spec:** `docs/superpowers/specs/2026-11-18-m4a-managed-accounts-design.md`(先读;Casdoor 行为见 `docs/casdoor-findings.md` §9)

## Global Constraints

- 跑测试:`PYTEST="env TESTING=1 /private/tmp/claude-501/-Users-even-Desktop-project-even-auth-gateway/dd52d484-beb5-4d0b-97b2-aac8faf7841a/scratchpad/venv/bin/python -m pytest -q -p no:cacheprovider"`,在仓库根目录执行 `$PYTEST tests/...`。每个任务结束跑全量 `$PYTEST`,必须全绿(开工前基线 208 个测试)。
- **飞书账号(id 以 `ou_` 开头)的行为一律不变**:现有测试一个都不能改坏;闸门对 `ou_` 直接放行、不多调 Casdoor。
- Casdoor 已实测的行为(findings §9 + 计划阶段补测):`add-user` 接受 `isForbidden` / `needUpdatePassword` / `properties`;`update-user?columns=properties` 只落这一列且保留原有键;`update-user?columns=password,needUpdatePassword` 能由管理员直接设密码;`get-user?email=` 查不到返回 `status=ok, data=null`;邮箱可直接登录;`needUpdatePassword=true` 时 password grant 被拒(服务账号不能置)。
- `properties` 的键用 `evauth_` 前缀,值一律是字符串;删除键 = 在 `set_properties` 里传 `None`。
- 账号 id:`x_` + 16 位十六进制,`name = id`。
- 用户可见文字用中文,风格和现有卡片/通知一致。
- 提交信息平实、面向项目,**不加任何 AI 署名行**(用户全局要求)。不要提交 `deploy/casdoor/.env`、`deploy/casdoor/gov.env`、`docs/*.pptx|*.potx`。
- 分支:`feat/m4a-managed-accounts`(已建,基于 main `4d7cd0a`)。

---

## File Structure

| 文件 | 职责 |
|---|---|
| `even_auth_gov/accounts.py`(新) | 账号模型与规则(纯函数)、闸门 `gate()`、身份行 `identity_of()`、管理员命令行 `main()` |
| `even_auth_gov/account_lifecycle.py`(新) | 建号/纳管/确认/拒绝、到期扫描与续期提醒、续期、连带停用、换负责人、设密码、列表、变更日志 |
| `tests/_fake_casdoor.py`(新) | 内存版 Casdoor(get-user / get-users / add-user / update-user / get-roles),生命周期类测试共用 |
| `tests/test_accounts.py`(新) | 规则、闸门、命令行 |
| `tests/test_account_lifecycle.py`(新) | 生命周期操作 |
| `even_auth_gov/settings.py` | `timezone()`、`account_max_days()`、`account_renew_days()`、`account_remind_days()`、`account_events_file()` |
| `even_auth_gov/casdoor_admin.py` | `get_user_by_email`、`create_user`、`set_properties`、`set_password`、`enable_user` |
| `even_auth_gov/approval_store.py` | `mark_denied(reason=)`、`expired` 状态可重新申请、`reopen_after_renewal` |
| `even_auth_gov/webhook.py`、`app.py`、`decision.py` | 闸门;审批卡片身份行 |
| `even_auth_gov/feishu.py`、`feishu_ws.py` | 确认卡片、续期卡片;`acct_*` 按钮路由 |
| `even_auth_gov/offboard.py`、`reconcile.py`、`scheduler.py` | 连带停用;对账纳入负责人;每小时账号扫描 |
| `even_auth_gov/access_report.py`、`audit_export.py`、`deploy/casdoor/backup.sh` | 新异常与新列;`--accounts` 导出;备份变更日志 |

---

### Task 1: 配置项 + 账号规则(纯函数)

**Files:**
- Modify: `even_auth_gov/settings.py`(文件末尾追加)
- Create: `even_auth_gov/accounts.py`
- Test: `tests/test_accounts.py`

**Interfaces:**
- Produces(settings):`timezone() -> str`、`account_max_days() -> int`、`account_renew_days() -> int`、`account_remind_days() -> list[int]`(去重、降序)、`account_events_file() -> pathlib.Path`
- Produces(accounts):常量 `KINDS`、`P_KIND / P_SPONSOR / P_EXPIRES / P_CONFIRMED / P_REJECTED / P_REMINDED / P_SPONSOR_PENDING / P_ORIGIN`、`REASONS`;函数 `today() -> date`、`now_iso() -> str`、`props(user) -> dict`、`is_feishu(user) -> bool`、`is_managed(user) -> bool`、`display_name(user) -> str`、`parse_date(v) -> date|None`、`expires_of(user)`、`sponsor_ok(user) -> bool`、`metadata_ok(user) -> bool`、`days_left(user, on) -> int|None`、`eligibility(user, on) -> str`、`reminder_due(user, on, remind_days) -> int|None`、`renewed_expiry(current, on, renew_days, max_days) -> date`、`expiry_error(expires, on, max_days) -> str`、`describe(user, sponsor_name="") -> str`

- [ ] **Step 1: 写失败的测试** —— 新建 `tests/test_accounts.py`:

```python
"""受管账号规则(里程碑 4a)。纯函数,today 由参数传入。"""
from datetime import date, timedelta
from pathlib import Path

import pytest

from even_auth_gov import accounts as acc, settings

D = date(2026, 11, 18)


def _u(props=None, id="x_1", forbidden=False):
    return {"id": id, "owner": "even-test", "name": id, "displayName": "张三", "email": "z@p.com",
            "isForbidden": forbidden, "properties": dict(props or {})}


def _ok(**over):
    p = {"evauth_kind": "partner", "evauth_sponsor": "ou_boss", "evauth_expires": "2026-12-01",
         "evauth_confirmed_at": "2026-11-01T00:00:00+00:00"}
    p.update(over)
    return _u({k: v for k, v in p.items() if v is not None})


def test_feishu_account_is_always_eligible_and_never_managed():
    feishu = {"id": "ou_a", "properties": {"evauth_kind": "partner"}}
    assert acc.eligibility(feishu, D) == ""
    assert acc.is_feishu(feishu) and not acc.is_managed(feishu)


def test_unregistered_non_feishu_account():
    assert acc.eligibility(_u({}), D) == "unregistered"
    assert not acc.is_managed(_u({}))


@pytest.mark.parametrize("over", [
    {"evauth_kind": "vendor"},             # 类型不认识
    {"evauth_sponsor": "x_other"},         # 负责人不是飞书账号
    {"evauth_sponsor": None},
    {"evauth_expires": "2026-13-01"},      # 日期非法
    {"evauth_expires": None},
])
def test_invalid_metadata(over):
    assert acc.eligibility(_ok(**over), D) == "invalid_metadata"


def test_rejected_and_unconfirmed():
    assert acc.eligibility(_ok(evauth_confirmed_at=None), D) == "unconfirmed"
    assert acc.eligibility(_ok(evauth_confirmed_at=None, evauth_rejected_at="2026-11-02T00:00:00+00:00"), D) == "rejected"


def test_expiry_day_itself_is_still_valid():
    assert acc.eligibility(_ok(evauth_expires="2026-11-18"), D) == ""
    assert acc.eligibility(_ok(evauth_expires="2026-11-17"), D) == "expired"
    assert acc.days_left(_ok(evauth_expires="2026-11-28"), D) == 10


@pytest.mark.parametrize("expires,reminded,want", [
    ("2026-12-08", "", None),                  # 还剩 20 天
    ("2026-12-02", "", 14),                    # 剩 14 天
    ("2026-11-28", "2026-11-28:14", None),     # 14 档发过了,剩 10 天还不到 3 档
    ("2026-11-21", "2026-11-21:14", 3),        # 剩 3 天,该发 3 档
    ("2026-11-20", "", 3),                     # 剩 2 天才首次扫到:只发最小一档
    ("2026-11-21", "2026-11-21:3", None),      # 3 档发过了
    ("2026-11-21", "2026-10-01:3", 3),         # 上次提醒针对旧到期日,续期后重新开始
    ("2026-11-17", "", None),                  # 已过期,不提醒
])
def test_reminder_due(expires, reminded, want):
    u = _ok(evauth_expires=expires, evauth_reminded=reminded or None)
    assert acc.reminder_due(u, D, [14, 3]) == want


def test_renewed_expiry_extends_from_later_of_today_and_current_with_cap():
    assert acc.renewed_expiry(date(2026, 12, 1), D, 90, 180) == date(2027, 3, 1)
    assert acc.renewed_expiry(date(2026, 10, 1), D, 90, 180) == D + timedelta(days=90)
    assert acc.renewed_expiry(None, D, 90, 180) == D + timedelta(days=90)
    assert acc.renewed_expiry(date(2027, 5, 1), D, 90, 180) == D + timedelta(days=180)   # 不超过上限


def test_expiry_error():
    assert "YYYY-MM-DD" in acc.expiry_error(None, D, 180)
    assert "早于今天" in acc.expiry_error(date(2026, 11, 17), D, 180)
    assert "最长 180 天" in acc.expiry_error(D + timedelta(days=181), D, 180)
    assert acc.expiry_error(D, D, 180) == "" and acc.expiry_error(D + timedelta(days=180), D, 180) == ""


def test_describe():
    assert acc.describe(_ok(), "李四") == "外部合作方 · 负责人 李四 · 有效期至 2026-12-01"
    assert acc.describe({"id": "ou_a"}, "李四") == ""


def test_settings_parsing(monkeypatch):
    monkeypatch.setenv("ACCOUNT_REMIND_DAYS", "3, 14,x,14")
    assert settings.account_remind_days() == [14, 3]
    monkeypatch.setenv("ACCOUNT_REMIND_DAYS", "")
    assert settings.account_remind_days() == [14, 3]
    for bad in ("abc", "0", "-5"):
        monkeypatch.setenv("ACCOUNT_MAX_DAYS", bad)
        monkeypatch.setenv("ACCOUNT_RENEW_DAYS", bad)
        assert settings.account_max_days() == 180 and settings.account_renew_days() == 90
    monkeypatch.delenv("ACCOUNT_EVENTS_FILE", raising=False)
    monkeypatch.setenv("APPROVAL_STORE_FILE", "/data/approvals.json")
    assert settings.account_events_file() == Path("/data/account_events.jsonl")
    monkeypatch.setenv("ACCOUNT_EVENTS_FILE", "/tmp/ev.jsonl")
    assert settings.account_events_file() == Path("/tmp/ev.jsonl")


def test_today_survives_bad_timezone(monkeypatch):
    monkeypatch.setenv("SCHEDULER_TIMEZONE", "Nope/Nowhere")
    assert isinstance(acc.today(), date)
```

- [ ] **Step 2: 运行确认失败** —— `$PYTEST tests/test_accounts.py`,预期 `ModuleNotFoundError: even_auth_gov.accounts` / `AttributeError: settings.account_remind_days`。

- [ ] **Step 3: 实现 settings** —— 在 `even_auth_gov/settings.py` 顶部 `import os` 下加 `from pathlib import Path`,文件末尾追加:

```python
def timezone() -> str:
    """业务日期(账号到期日等)按哪个时区算;与调度器同一个变量。"""
    return os.getenv("SCHEDULER_TIMEZONE", "Asia/Shanghai")


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def account_max_days() -> int:
    """受管账号每次有效期上限(天),建号/纳管/续期都受它约束。ACCOUNT_MAX_DAYS,默认 180。"""
    return _positive_int("ACCOUNT_MAX_DAYS", 180)


def account_renew_days() -> int:
    """一次续期延长的天数。ACCOUNT_RENEW_DAYS,默认 90。"""
    return _positive_int("ACCOUNT_RENEW_DAYS", 90)


def account_remind_days() -> list[int]:
    """到期前第几天提醒负责人续期(去重、降序)。ACCOUNT_REMIND_DAYS="14,3",配错或为空回到默认。"""
    out = {int(x) for x in os.getenv("ACCOUNT_REMIND_DAYS", "14,3").replace(" ", "").split(",") if x.isdigit()}
    return sorted(out, reverse=True) or [14, 3]


def account_events_file() -> Path:
    """受管账号变更历史(JSON Lines)。ACCOUNT_EVENTS_FILE,默认和审批库同目录的 account_events.jsonl。"""
    raw = os.getenv("ACCOUNT_EVENTS_FILE", "").strip()
    if raw:
        return Path(raw).expanduser()
    store = os.getenv("APPROVAL_STORE_FILE", "").strip()
    return (Path(store).expanduser().parent if store else Path("data")) / "account_events.jsonl"
```

- [ ] **Step 4: 实现 accounts.py** —— 新建 `even_auth_gov/accounts.py`:

```python
"""受管的非飞书账号(里程碑 4a):外部合作方 / 没飞书的内部员工 / 服务账号。

当前状态记在 Casdoor 用户的 properties(evauth_* 键,见设计文档第 3 节),Casdoor 是唯一数据来源。
本模块放:账号规则(纯函数,today 由参数传入便于测试)、闸门 gate()、管理员命令行 main()。
会改 Casdoor / 发飞书的操作在 account_lifecycle.py。
设计:docs/superpowers/specs/2026-11-18-m4a-managed-accounts-design.md
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone as _tz
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from even_auth_gov import settings

KINDS = {"partner": "外部合作方", "staff": "内部员工(无飞书)", "service": "服务账号"}

P_KIND = "evauth_kind"
P_SPONSOR = "evauth_sponsor"
P_EXPIRES = "evauth_expires"
P_CONFIRMED = "evauth_confirmed_at"
P_REJECTED = "evauth_rejected_at"
P_REMINDED = "evauth_reminded"
P_SPONSOR_PENDING = "evauth_sponsor_pending"
P_ORIGIN = "evauth_origin"   # created:命令行新建,确认后启用;registered:纳管存量账号,确认不改启停

# eligibility() / gate() 的不合格原因 → 给人看的说明
REASONS = {
    "unregistered": "不是飞书账号,也没有登记负责人和到期日",
    "invalid_metadata": "登记信息不完整或格式不对",
    "unconfirmed": "负责人还没确认",
    "rejected": "负责人拒绝负责",
    "expired": "已过期",
    "disabled": "已停用",
    "lookup_failed": "暂时查不到账号状态",
}


def today() -> date:
    """按 SCHEDULER_TIMEZONE(默认 Asia/Shanghai)算的今天;时区名无效时按东八区。"""
    try:
        tz = ZoneInfo(settings.timezone())
    except (ZoneInfoNotFoundError, ValueError):
        tz = _tz(timedelta(hours=8))
    return datetime.now(tz).date()


def now_iso() -> str:
    return datetime.now(_tz.utc).isoformat()


def props(user: dict | None) -> dict:
    p = (user or {}).get("properties")
    return dict(p) if isinstance(p, dict) else {}


def is_feishu(user: dict | None) -> bool:
    return str((user or {}).get("id") or "").startswith("ou_")


def is_managed(user: dict | None) -> bool:
    """非飞书、并且登记了 evauth_kind(不论登记信息是否完整)。"""
    return not is_feishu(user) and bool(props(user).get(P_KIND))


def display_name(user: dict | None) -> str:
    u = user or {}
    return u.get("displayName") or u.get("name") or u.get("id") or ""


def parse_date(value) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip())
    except ValueError:
        return None


def expires_of(user: dict | None) -> date | None:
    return parse_date(props(user).get(P_EXPIRES))


def sponsor_ok(user: dict | None) -> bool:
    return str(props(user).get(P_SPONSOR) or "").startswith("ou_")


def metadata_ok(user: dict | None) -> bool:
    """类型合法、负责人是飞书 open_id、到期日能解析。"""
    return props(user).get(P_KIND) in KINDS and sponsor_ok(user) and expires_of(user) is not None


def days_left(user: dict | None, on: date) -> int | None:
    exp = expires_of(user)
    return None if exp is None else (exp - on).days


def eligibility(user: dict | None, on: date) -> str:
    """能否进审批:'' = 合格;否则 REASONS 的键。只看登记信息,账号是否停用另查。"""
    if is_feishu(user):
        return ""
    p = props(user)
    if not p.get(P_KIND):
        return "unregistered"
    if not metadata_ok(user):
        return "invalid_metadata"
    if p.get(P_REJECTED):
        return "rejected"
    if not p.get(P_CONFIRMED):
        return "unconfirmed"
    if days_left(user, on) < 0:
        return "expired"
    return ""


def reminder_due(user: dict | None, on: date, remind_days: list[int]) -> int | None:
    """今天该发哪一档续期提醒(提前天数),不该发返回 None。
    只发满足 days_left <= d 的最小一档;本次到期日已经发过不大于它的档位就不再发。"""
    left = days_left(user, on)
    if left is None or left < 0:
        return None
    due = [d for d in remind_days if left <= d]
    if not due:
        return None
    d = min(due)
    sent_for, _, stage = str(props(user).get(P_REMINDED) or "").partition(":")
    if sent_for == props(user).get(P_EXPIRES) and stage.isdigit() and int(stage) <= d:
        return None
    return d


def renewed_expiry(current: date | None, on: date, renew_days: int, max_days: int) -> date:
    """续期后的到期日:从今天和原到期日中较晚的一个往后加 renew_days,不超过今天 + max_days。"""
    base = max(on, current) if current else on
    return min(base + timedelta(days=renew_days), on + timedelta(days=max_days))


def expiry_error(expires: date | None, on: date, max_days: int) -> str:
    """建号 / 纳管 / 指定续期日时的到期日校验;合法返回 ''。"""
    if expires is None:
        return "到期日格式应为 YYYY-MM-DD"
    if expires < on:
        return "到期日不能早于今天"
    if (expires - on).days > max_days:
        return f"有效期最长 {max_days} 天(最晚 {(on + timedelta(days=max_days)).isoformat()})"
    return ""


def describe(user: dict | None, sponsor_name: str = "") -> str:
    """审批卡片上的身份行,如"外部合作方 · 负责人 李四 · 有效期至 2027-02-01";飞书 / 未登记账号返回 ''。"""
    if not is_managed(user):
        return ""
    p = props(user)
    kind = KINDS.get(p.get(P_KIND), p.get(P_KIND))
    return f"{kind} · 负责人 {sponsor_name or p.get(P_SPONSOR, '')} · 有效期至 {p.get(P_EXPIRES, '')}"
```

- [ ] **Step 5: 运行确认通过** —— `$PYTEST tests/test_accounts.py`,全部 PASS;再跑全量 `$PYTEST`。

- [ ] **Step 6: 提交**

```bash
git add even_auth_gov/settings.py even_auth_gov/accounts.py tests/test_accounts.py
git commit -m "M4a: managed account rules and settings (kind, sponsor, expiry, reminders)"
```

---

### Task 2: Casdoor 辅助函数

**Files:**
- Modify: `even_auth_gov/casdoor_admin.py`(`find_user_by_email` 之后追加)
- Test: `tests/test_casdoor_admin.py`(末尾追加)

**Interfaces:**
- Produces:`get_user_by_email(client, email) -> tuple[bool, dict | None]`(查询是否成功, 用户);`create_user(client, user: dict) -> bool`;`set_properties(client, user_id, updates: dict) -> bool`(值为 `None` 删除该键,持 `_GROUP_LOCK`);`set_password(client, user_id, password, need_update: bool) -> bool`;`enable_user(client, user_id) -> bool`

- [ ] **Step 1: 写失败的测试** —— 追加到 `tests/test_casdoor_admin.py`:

```python
# ── 里程碑 4a:受管账号用到的写操作 ──

def _email_handler(seen, data=None, status="ok"):
    def handler(req):
        seen.setdefault("params", []).append(dict(req.url.params))
        return httpx.Response(200, json={"status": status, "data": data})
    return handler


def test_get_user_by_email_distinguishes_missing_from_error(monkeypatch):
    monkeypatch.setenv("CASDOOR_ORG", "even-test")
    seen = {}
    found = {"id": "x_1", "email": "a@p.com"}
    async def run(handler):
        async with _client(handler) as c:
            return await ca.get_user_by_email(c, "a@p.com")
    assert asyncio.run(run(_email_handler(seen, data=found))) == (True, found)
    assert seen["params"][0]["email"] == "a@p.com" and seen["params"][0]["owner"] == "even-test"
    assert asyncio.run(run(_email_handler(seen, data=None))) == (True, None)          # 没这个人
    assert asyncio.run(run(_email_handler(seen, status="error"))) == (False, None)   # 查询出错


def test_create_user_posts_add_user(monkeypatch):
    monkeypatch.setenv("CASDOOR_ORG", "even-test")
    monkeypatch.setenv("CASDOOR_CLIENT_ID", "cid")
    seen = {}
    def handler(req):
        seen["path"], seen["params"], seen["body"] = req.url.path, dict(req.url.params), json.loads(req.content)
        return httpx.Response(200, json={"status": "ok", "data": "Affected"})
    async def run():
        async with _client(handler) as c:
            return await ca.create_user(c, {"owner": "even-test", "name": "x_1", "id": "x_1", "isForbidden": True})
    assert asyncio.run(run()) is True
    assert seen["path"] == "/api/add-user" and seen["params"]["clientId"] == "cid"
    assert seen["body"]["isForbidden"] is True


def test_create_user_failure_returns_false(monkeypatch):
    def handler(req):
        return httpx.Response(200, json={"status": "error", "msg": "dup"})
    async def run():
        async with _client(handler) as c:
            return await ca.create_user(c, {"id": "x_1"})
    assert asyncio.run(run()) is False


def test_set_properties_merges_and_deletes(monkeypatch):
    monkeypatch.setenv("CASDOOR_ORG", "even-test")
    seen = {}
    user = {**_user_obj(), "properties": {"oauth_Lark_id": "ou_feishu123", "evauth_reminded": "x"}}
    async def run():
        async with _client(_handler_factory(seen, user=user)) as c:
            return await ca.set_properties(c, OPEN_ID, {"evauth_kind": "partner", "evauth_reminded": None})
    assert asyncio.run(run()) is True
    assert seen["upd_columns"] == "properties"
    assert seen["upd_body"]["properties"] == {"oauth_Lark_id": "ou_feishu123", "evauth_kind": "partner"}


def test_set_password_writes_both_columns(monkeypatch):
    monkeypatch.setenv("CASDOOR_ORG", "even-test")
    seen = {}
    async def run():
        async with _client(_handler_factory(seen, user=_user_obj())) as c:
            return await ca.set_password(c, OPEN_ID, "Pw-123456!", need_update=True)
    assert asyncio.run(run()) is True
    assert seen["upd_columns"] == "password,needUpdatePassword"
    assert seen["upd_body"]["password"] == "Pw-123456!" and seen["upd_body"]["needUpdatePassword"] is True


def test_enable_user(monkeypatch):
    monkeypatch.setenv("CASDOOR_ORG", "even-test")
    seen = {}
    async def run():
        async with _client(_handler_factory(seen, user=_user_obj(isForbidden=True))) as c:
            return await ca.enable_user(c, OPEN_ID)
    assert asyncio.run(run()) is True
    assert seen["upd_columns"] == "isForbidden" and seen["upd_body"]["isForbidden"] is False


def test_writes_fail_when_user_missing(monkeypatch):
    seen = {}
    async def run():
        async with _client(_handler_factory(seen, user=None)) as c:
            return (await ca.set_properties(c, "x_none", {"a": "b"}), await ca.set_password(c, "x_none", "p", False),
                    await ca.enable_user(c, "x_none"))
    assert asyncio.run(run()) == (False, False, False)
```

- [ ] **Step 2: 运行确认失败** —— `$PYTEST tests/test_casdoor_admin.py`,预期 `AttributeError: module 'even_auth_gov.casdoor_admin' has no attribute 'get_user_by_email'`。

- [ ] **Step 3: 实现** —— 追加到 `even_auth_gov/casdoor_admin.py` 末尾:

```python
# ── 里程碑 4a:受管的非飞书账号 ──

async def get_user_by_email(client: httpx.AsyncClient, email: str) -> tuple[bool, dict | None]:
    """邮箱 → (查询是否成功, 用户对象或 None)。区分"没这个人"和"查询出错":
    建号时的邮箱占用检查、负责人查找都要在读不到 Casdoor 时中止(实测:查不到返回 status=ok, data=null)。"""
    try:
        resp = await client.get("/api/get-user", params={"owner": _org(), "email": email, **_auth()}, timeout=10.0)
        data = resp.json() if resp.content else {}
        if resp.status_code != 200 or not isinstance(data, dict) or data.get("status") == "error":
            logger.warning("Casdoor get-user(email=%s) failed: %s %s", email, resp.status_code, data)
            return False, None
        return True, (data.get("data") or None)
    except Exception as e:
        logger.warning("Casdoor get-user(email=%s) error: %s", email, e)
        return False, None

async def create_user(client: httpx.AsyncClient, user: dict) -> bool:
    """add-user。user 自带 owner/name/id;实测 isForbidden、needUpdatePassword、properties 都会照写。"""
    try:
        resp = await client.post("/api/add-user", params=_auth(), json=user, timeout=10.0)
        data = resp.json() if resp.content else {}
        if resp.status_code == 200 and data.get("status") != "error":
            return True
        logger.warning("Casdoor add-user %s failed: %s %s", user.get("id"), resp.status_code, data)
        return False
    except Exception as e:
        logger.warning("Casdoor add-user %s error: %s", user.get("id"), e)
        return False

async def set_properties(client: httpx.AsyncClient, user_id: str, updates: dict) -> bool:
    """合并写用户 properties:值为 None 的键删除,其它转成字符串写入;原有键(如 oauth_Lark_*)保留。
    整对象读-改-写(columns=properties 只落这一列),持 _GROUP_LOCK 防止并发改丢写。"""
    async with _GROUP_LOCK:
        u = await _get_by_open_id(client, user_id)
        if u is None:
            return False
        merged = dict(u.get("properties") or {})
        for key, value in updates.items():
            if value is None:
                merged.pop(key, None)
            else:
                merged[key] = str(value)
        u["properties"] = merged
        return await _update_user(client, _casdoor_id(u), u, "properties")

async def set_password(client: httpx.AsyncClient, user_id: str, password: str, need_update: bool) -> bool:
    """管理员直接设密码(实测 columns=password,needUpdatePassword 生效,旧密码失效)。
    need_update=True:下次网页登录必须先改密码;服务账号不能置(否则 password grant 永远失败)。"""
    u = await _get_by_open_id(client, user_id)
    if u is None:
        return False
    u["password"] = password
    u["needUpdatePassword"] = bool(need_update)
    return await _update_user(client, _casdoor_id(u), u, "password,needUpdatePassword")

async def enable_user(client: httpx.AsyncClient, user_id: str) -> bool:
    u = await _get_by_open_id(client, user_id)
    if u is None:
        return False
    u["isForbidden"] = False
    return await _update_user(client, _casdoor_id(u), u, "isForbidden")
```

- [ ] **Step 4: 运行确认通过** —— `$PYTEST tests/test_casdoor_admin.py`,再全量 `$PYTEST`。

- [ ] **Step 5: 提交**

```bash
git add even_auth_gov/casdoor_admin.py tests/test_casdoor_admin.py
git commit -m "M4a: Casdoor helpers for managed accounts (email lookup, create, properties, password, enable)"
```

---

### Task 3: 闸门(webhook、/request-access、批准)

这一步单独就补上了"陌生人自助注册后骗批准"的风险(spec §1.1),完成后可以先合进 main。

**Files:**
- Modify: `even_auth_gov/accounts.py`(加 `gate`)、`even_auth_gov/approval_store.py:129-130`(`mark_denied`)、`even_auth_gov/webhook.py:68-91`、`even_auth_gov/app.py:91-125`、`even_auth_gov/decision.py`、`even_auth_gov/audit_export.py`(COLUMNS)
- Test: `tests/test_accounts.py`、`tests/test_approval_store.py`、`tests/test_webhook.py`、`tests/test_app_wiring.py`、`tests/test_decision.py`、`tests/test_audit_export.py`(若断言了表头)

**Interfaces:**
- Consumes:Task 1 `eligibility`、`today`、`REASONS`;`casdoor_admin.get_user`
- Produces:`accounts.gate(client, user_id) -> str`('' 放行;否则 REASONS 的键);`approval_store.mark_denied(open_id, app, by, reason="")`(reason 非空时写 `denied_reason`)

- [ ] **Step 1: 写失败的测试**

`tests/test_accounts.py` 追加:

```python
import asyncio
import httpx


def _client_returning(data, status="ok"):
    def handler(req):
        return httpx.Response(200, json={"status": status, "data": data})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://casdoor")


def _gate(user_id, data=None, status="ok"):
    async def run():
        async with _client_returning(data, status) as c:
            return await acc.gate(c, user_id)
    return asyncio.run(run())


def test_gate_lets_feishu_through_without_asking_casdoor():
    assert asyncio.run(acc.gate(None, "ou_a")) == ""     # client=None:真去查会炸


def test_gate_reasons(monkeypatch):
    far = (acc.today() + timedelta(days=30)).isoformat()
    good = _ok(evauth_expires=far)
    assert _gate("x_1", good) == ""
    assert _gate("x_1", _u({})) == "unregistered"
    assert _gate("x_1", {**good, "isForbidden": True}) == "disabled"
    assert _gate("x_1", None) == "lookup_failed"                # 查不到
    assert _gate("x_1", None, status="error") == "lookup_failed"
```

`tests/test_approval_store.py` 追加:

```python
def test_mark_denied_records_reason_only_when_given(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    approval_store.mark_pending("x_1", "app-a", {"name": "张三"})
    approval_store.mark_denied("x_1", "app-a", "system", reason="ineligible:unregistered")
    rec = approval_store.get("x_1", "app-a")
    assert rec["status"] == "denied" and rec["denied_by"] == "system" and rec["denied_reason"] == "ineligible:unregistered"
    approval_store.mark_pending("ou_b", "app-a", {"name": "b"})
    approval_store.mark_denied("ou_b", "app-a", "ou_boss")
    assert "denied_reason" not in approval_store.get("ou_b", "app-a")
```

`tests/test_webhook.py` 追加:

```python
from datetime import timedelta
import httpx
from even_auth_gov import accounts


def _casdoor_with(monkeypatch, user, status="ok"):
    def handler(req):
        return httpx.Response(200, json={"status": status, "data": user})
    monkeypatch.setattr("even_auth_gov.casdoor_admin.make_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://casdoor"))


def _managed_props():
    return {"evauth_kind": "partner", "evauth_sponsor": "ou_boss",
            "evauth_expires": (accounts.today() + timedelta(days=30)).isoformat(),
            "evauth_confirmed_at": "2026-11-01T00:00:00+00:00"}


def _signup(uid):
    return json.dumps({"action": "signup", "extendedUser": {"id": uid, "name": "n", "displayName": "王总(CEO)"}}).encode()


def test_self_signup_of_unregistered_account_gets_no_card(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _casdoor_with(monkeypatch, {"id": "uuid-1", "owner": "even-test", "name": "n", "properties": {}})
    sent = []
    async def fake_send(info): sent.append(info)
    monkeypatch.setattr(webhook, "send_approval_card", fake_send)
    r = asyncio.run(webhook.handle(_signup("uuid-1"), token="shh"))
    assert r["status"] == "ignored" and sent == []
    assert approval_store.get("uuid-1", "app-a") is None


def test_unconfirmed_managed_account_creation_gets_no_card(monkeypatch, tmp_path):
    """命令行建号可能触发 add-user webhook:账号停用中、负责人没确认 → 不发卡片。"""
    _env(monkeypatch, tmp_path)
    props = {k: v for k, v in _managed_props().items() if k != "evauth_confirmed_at"}
    _casdoor_with(monkeypatch, {"id": "x_1", "owner": "even-test", "name": "x_1", "isForbidden": True, "properties": props})
    sent = []
    async def fake_send(info): sent.append(info)
    monkeypatch.setattr(webhook, "send_approval_card", fake_send)
    assert asyncio.run(webhook.handle(_signup("x_1"), token="shh"))["status"] == "ignored" and sent == []


def test_eligible_managed_account_goes_to_approval(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _casdoor_with(monkeypatch, {"id": "x_1", "owner": "even-test", "name": "x_1", "properties": _managed_props()})
    sent = []
    async def fake_send(info): sent.append(info)
    monkeypatch.setattr(webhook, "send_approval_card", fake_send)
    assert asyncio.run(webhook.handle(_signup("x_1"), token="shh"))["status"] == "ok"
    assert approval_store.get("x_1", "app-a")["status"] == "pending" and len(sent) == 1


def test_gate_fails_closed_when_casdoor_unreadable(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _casdoor_with(monkeypatch, None, status="error")
    assert asyncio.run(webhook.handle(_signup("x_1"), token="shh"))["status"] == "ignored"
    assert approval_store.get("x_1", "app-a") is None


def test_feishu_signup_does_not_query_casdoor(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    def boom():
        raise AssertionError("飞书账号不该查 Casdoor")
    monkeypatch.setattr("even_auth_gov.casdoor_admin.make_client", boom)
    async def fake_send(info): pass
    monkeypatch.setattr(webhook, "send_approval_card", fake_send)
    assert asyncio.run(webhook.handle(_signup("ou_new"), token="shh"))["status"] == "ok"
```

`tests/test_app_wiring.py`:先改 `_fake_casdoor`,加参数 `users=None`(id → 覆盖字段),get-user 分支改为:

```python
        if req.url.path == "/api/get-user":
            if not user_ok:
                return httpx.Response(200, json={"status": "error", "msg": "boom", "data": None})
            uid = req.url.params.get("userId")
            data = {"owner": "even-test", "name": "randname", "id": uid,
                    "groups": user_groups or [], "isForbidden": forbidden}
            data.update((users or {}).get(uid, {}))
            return httpx.Response(200, json={"status": "ok", "data": data})
```

签名改为 `def _fake_casdoor(monkeypatch, roles=None, jwks_status=200, roles_ok=True, user_groups=None, forbidden=False, user_ok=True, users=None):`。然后追加测试:

```python
from datetime import timedelta


def _managed_props(days=30, **over):
    from even_auth_gov import accounts
    p = {"evauth_kind": "partner", "evauth_sponsor": "ou_boss",
         "evauth_expires": (accounts.today() + timedelta(days=days)).isoformat(),
         "evauth_confirmed_at": "2026-11-01T00:00:00+00:00"}
    p.update(over)
    return p


def test_request_access_rejects_unregistered_non_feishu_account(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path)
    with TestClient(app) as c:
        r = _ra(c, token(sub="uuid-stranger"))
    assert r.status_code == 403 and "未登记" in r.json()["error"]
    from even_auth_gov import approval_store
    assert approval_store.get("uuid-stranger", "app-b") is None


def test_request_access_rejects_expired_managed_account(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, users={"x_ext": {"properties": _managed_props(days=-1)}})
    with TestClient(app) as c:
        assert _ra(c, token(sub="x_ext")).status_code == 403


def test_request_access_accepts_eligible_managed_account(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, users={"x_ext": {"properties": _managed_props()}})
    with TestClient(app) as c:
        r = _ra(c, token(sub="x_ext"))
    assert r.status_code == 200 and r.json() == {"status": "ok", "app": "app-b"}
```

`tests/test_decision.py` 追加:

```python
from datetime import timedelta
from even_auth_gov import accounts


def _external(props=None, forbidden=False):
    return {"id": "x_ext", "owner": "even-test", "name": "x_ext", "isForbidden": forbidden, "properties": props or {}}


def _good_props(**over):
    p = {"evauth_kind": "partner", "evauth_sponsor": "ou_boss",
         "evauth_expires": (accounts.today() + timedelta(days=30)).isoformat(),
         "evauth_confirmed_at": "2026-11-01T00:00:00+00:00"}
    p.update(over)
    return p


def _setup_external(monkeypatch, tmp_path, user):
    calls = _setup(monkeypatch, tmp_path)
    approval_store.mark_pending("x_ext", "app-a", {"name": "张三"})
    async def fake_get_user(client, open_id): return user
    monkeypatch.setattr(decision.ca, "get_user", fake_get_user)
    return calls


def _approve_ext():
    return asyncio.run(decision.handle("sso_approve", operator_id="ou_boss", owner="ou_boss",
                                       sso_open_id="x_ext", app="app-a", client=None))


@pytest.mark.parametrize("user,reason", [
    (_external({}), "unregistered"),
    (_external(_good_props(evauth_expires="2020-01-01")), "expired"),
    (_external(_good_props(evauth_confirmed_at=None)), "unconfirmed"),
    (_external(_good_props(), forbidden=True), "disabled"),
])
def test_approving_ineligible_external_account_is_auto_denied(monkeypatch, tmp_path, user, reason):
    props = {k: v for k, v in user["properties"].items() if v is not None}
    calls = _setup_external(monkeypatch, tmp_path, {**user, "properties": props})
    r = _approve_ext()
    assert r["status"] == "ok" and r["decision"] == "denied" and "已自动拒绝" in r["message"]
    rec = approval_store.get("x_ext", "app-a")
    assert (rec["status"], rec["denied_by"], rec["denied_reason"]) == ("denied", "system", f"ineligible:{reason}")
    assert not any(c[0] == "add" for c in calls)


def test_approving_external_when_casdoor_unreadable_stays_pending(monkeypatch, tmp_path):
    _setup_external(monkeypatch, tmp_path, None)
    r = _approve_ext()
    assert r["status"] == "error" and approval_store.get("x_ext", "app-a")["status"] == "pending"


def test_approving_eligible_external_account_proceeds(monkeypatch, tmp_path):
    calls = _setup_external(monkeypatch, tmp_path, _external(_good_props()))
    assert _approve_ext()["decision"] == "approved"
    assert ("add", "x_ext", "approved-app-a") in calls
```

- [ ] **Step 2: 运行确认失败** —— `$PYTEST tests/test_accounts.py tests/test_approval_store.py tests/test_webhook.py tests/test_app_wiring.py tests/test_decision.py`,预期新测试失败(`gate` 不存在、`mark_denied` 不接受 reason、未登记账号照样建待批)。

- [ ] **Step 3: 实现**

`even_auth_gov/accounts.py` 末尾追加:

```python
async def gate(client, user_id: str) -> str:
    """闸门:这个账号能不能进审批。'' = 放行;否则 REASONS 的键。
    飞书账号(ou_)直接放行、不查 Casdoor —— 行为和以前完全一样。读不到 Casdoor 时不放行。"""
    if str(user_id or "").startswith("ou_"):
        return ""
    from even_auth_gov import casdoor_admin as ca
    user = await ca.get_user(client, user_id)
    if user is None:
        return "lookup_failed"
    if user.get("isForbidden"):
        return "disabled"
    return eligibility(user, today())
```

`even_auth_gov/approval_store.py` 把 `mark_denied` 换成:

```python
def mark_denied(open_id: str, app: str, by: str, reason: str = ""):
    """reason:系统自动拒绝时的原因(如闸门拦下的 ineligible:<原因>),人工拒绝不写。"""
    fields = {"status": "denied", "denied_by": by, "denied_at": _now()}
    if reason:
        fields["denied_reason"] = reason
    _set(open_id, app, **fields)
```

`even_auth_gov/webhook.py`:在 `open_id = user.get("id") or ""` / `if not open_id: return {"status": "ignored"}` 之后插入:

```python
    if not open_id.startswith("ou_"):
        # 里程碑 4a 闸门:非飞书账号必须是已登记、负责人已确认、没过期、没停用的受管账号,
        # 否则不建待批、不发卡片(防陌生人自助注册后骗批准;命令行建号的 add-user 回调也在此被挡下)。
        from even_auth_gov import accounts, casdoor_admin as ca
        async with ca.make_client() as client:
            reason = await accounts.gate(client, open_id)
        if reason:
            logger.info("Casdoor webhook: %s 不能进审批(%s),不建待批", open_id, reason)
            return {"status": "ignored"}
```

`even_auth_gov/app.py`:import 行加 `accounts`(`from even_auth_gov import (webhook as wh, settings, feishu_ws, scheduler, approval_store, logging_setup, casdoor_admin as ca, id_token, accounts)`);在 `if account.get("isForbidden"): ... return 403` 之后插入:

```python
        if not open_id.startswith("ou_"):
            reason = accounts.eligibility(account, accounts.today())   # 里程碑 4a 闸门
            if reason:
                logger.warning("request-access 拒绝(403): %s 不是合格的受管账号(%s) app=%s", open_id, reason, app_name)
                return JSONResponse({"error": "账号未登记、未确认或已过期,请联系管理员"}, status_code=403)
```

`even_auth_gov/decision.py`:import 改为 `from even_auth_gov import accounts, casdoor_admin as ca, approval_store, settings`;在 `group = _group_for(app)` 之前插入:

```python
    if action == "sso_approve" and not sso_open_id.startswith("ou_"):
        # 里程碑 4a 闸门(纵深防御):非飞书账号按 Casdoor 实时数据核对,不合格就系统拒绝,
        # 免得这条申请挂在待批里被反复催办。
        reason = await accounts.gate(client, sso_open_id)
        if reason == "lookup_failed":
            return {"status": "error", "message": f"批准 {name} 失败:暂时查不到该账号在 Casdoor 的状态,申请仍待批,请稍后再点一次"}
        if reason:
            approval_store.mark_denied(sso_open_id, app, "system", reason=f"ineligible:{reason}")
            logger.warning("SSO approve blocked: %s app=%s ineligible (%s)", sso_open_id, app, reason)
            return {"status": "ok", "decision": "denied",
                    "message": f"未批准 {name}:该账号{accounts.REASONS[reason]},已自动拒绝。需要开通请联系管理员"}
```

`even_auth_gov/audit_export.py`:`COLUMNS` 里 `"denied_by", "denied_at",` 改为 `"denied_by", "denied_at", "denied_reason",`。如果 `tests/test_audit_export.py` 硬编码了表头,同步加上 `denied_reason`。

- [ ] **Step 4: 运行确认通过** —— 跑 Step 2 的命令,再全量 `$PYTEST`。

- [ ] **Step 5: 提交**

```bash
git add even_auth_gov/accounts.py even_auth_gov/approval_store.py even_auth_gov/webhook.py even_auth_gov/app.py \
        even_auth_gov/decision.py even_auth_gov/audit_export.py tests/
git commit -m "M4a: eligibility gate for non-Feishu accounts on signup webhook, request-access and approval

Self-registered or unconfirmed/expired non-Feishu accounts no longer get
approval cards; approving one is auto-denied with the reason recorded."
```

---

### Task 4: 审批卡片身份行

**Files:**
- Modify: `even_auth_gov/accounts.py`(加 `identity_of`)、`even_auth_gov/feishu.py:build_sso_approval_card`、`even_auth_gov/app.py:_send_card`
- Test: `tests/test_feishu_card.py`、`tests/test_app_wiring.py`

**Interfaces:**
- Produces:`accounts.identity_of(client, user_id) -> str`;`build_sso_approval_card(..., note="", identity="")`

- [ ] **Step 1: 写失败的测试**

`tests/test_feishu_card.py` 追加(沿用该文件已有的 import 方式拿到 `feishu`):

```python
def test_card_shows_identity_line_for_managed_accounts():
    card = feishu.build_sso_approval_card("x_1", "张三", "z@p.com", "app-a",
                                          identity="外部合作方 · 负责人 李四 · 有效期至 2027-02-01")
    assert "**账号类型:** 外部合作方 · 负责人 李四 · 有效期至 2027-02-01" in card["elements"][0]["content"]


def test_card_has_no_identity_line_by_default():
    card = feishu.build_sso_approval_card("ou_1", "张三", "z@p.com", "app-a")
    assert "账号类型" not in card["elements"][0]["content"]
```

`tests/test_app_wiring.py` 追加:

```python
def test_card_for_managed_account_shows_kind_sponsor_and_expiry(monkeypatch, tmp_path):
    props = _managed_props()
    app = _app_live(monkeypatch, tmp_path, users={"x_ext": {"properties": props}, "ou_boss": {"displayName": "李四"}})
    monkeypatch.setenv("APPROVER_APP_B", "ou_app_b_boss")
    cards = []
    async def fake_send_card(to, card, receive_id_type="open_id"):
        cards.append(card); return True
    monkeypatch.setattr("even_auth_gov.feishu.send_card", fake_send_card)
    with TestClient(app) as c:
        assert _ra(c, token(sub="x_ext")).status_code == 200
    expected = f"外部合作方 · 负责人 李四 · 有效期至 {props['evauth_expires']}"
    assert expected in cards[0]["elements"][0]["content"]
```

- [ ] **Step 2: 运行确认失败** —— `$PYTEST tests/test_feishu_card.py tests/test_app_wiring.py`,预期 `TypeError: unexpected keyword argument 'identity'`、身份行缺失。

- [ ] **Step 3: 实现**

`even_auth_gov/accounts.py` 末尾追加:

```python
async def identity_of(client, user_id: str) -> str:
    """审批卡片身份行(带负责人姓名);飞书账号返回 '' 且不查 Casdoor。查不到登记信息时给出提示而不是空白。"""
    if str(user_id or "").startswith("ou_"):
        return ""
    from even_auth_gov import casdoor_admin as ca
    user = await ca.get_user(client, user_id)
    if not is_managed(user):
        return "非飞书账号(未查到登记信息)"
    sponsor = await ca.get_user(client, props(user).get(P_SPONSOR, ""))
    return describe(user, display_name(sponsor) if sponsor else "")
```

`even_auth_gov/feishu.py:build_sso_approval_card`:签名加 `identity: str = ""`,docstring 末尾加一句"`identity`:非飞书受管账号的身份行(类型 · 负责人 · 有效期),飞书账号为空";函数体里 `email_line` 之后加 `identity_line = f"\n**账号类型:** {identity}" if identity else ""`,markdown 内容改为 `f"{note_line}**申请人:** {name}{email_line}{identity_line}{app_line}\n\n{hint}"`。

`even_auth_gov/app.py:_send_card`:把 `async with ca.make_client() as client:` 块改为

```python
        async with ca.make_client() as client:
            roles = await ca.list_app_roles(client, app_name, settings.known_apps())
            identity = await accounts.identity_of(client, info.get("open_id", ""))   # 里程碑 4a:非飞书账号的类型/负责人/有效期
```

并在 `build_sso_approval_card(...)` 调用里加 `identity=identity`。

- [ ] **Step 4: 运行确认通过** —— 跑 Step 2 的命令,再全量 `$PYTEST`。

- [ ] **Step 5: 提交**

```bash
git add even_auth_gov/accounts.py even_auth_gov/feishu.py even_auth_gov/app.py tests/test_feishu_card.py tests/test_app_wiring.py
git commit -m "M4a: approval card shows kind, sponsor and expiry for managed accounts"
```

---

### Task 5: 建号、纳管、负责人确认/拒绝(含确认卡片、按钮路由、命令行)

**Files:**
- Create: `tests/_fake_casdoor.py`、`even_auth_gov/account_lifecycle.py`、`tests/test_account_lifecycle.py`
- Modify: `even_auth_gov/feishu.py`(加 `build_account_confirm_card`)、`even_auth_gov/feishu_ws.py:_process_card`、`even_auth_gov/accounts.py`(加 `main`)
- Test: `tests/test_feishu_card.py`、`tests/test_ws_wiring.py`、`tests/test_accounts.py`

**Interfaces:**
- Consumes:Task 1 规则;Task 2 `get_user_by_email / create_user / set_properties / enable_user`;`offboard.apply(open_id, name, reason, client)`
- Produces(account_lifecycle):`AccountError`;`log_event(event, user, actor="", detail="")`;`new_password(length=20) -> str`;`new_user_id() -> str`;`create(client, kind, name, email, sponsor_email, expires, actor="admin") -> dict`(`user_id, email, password, sponsor, confirm_sent`);`register(client, ref, kind, sponsor_email, expires, actor="admin") -> dict`(`user_id, sponsor, confirm_sent`);`resend_confirm(client, ref) -> bool`;`send_confirm(user, mode, to="") -> bool`;`handle_action(action, operator_id, user_id, client) -> str`;内部 `_resolve_user / _resolve_sponsor / _merged / _notify`
- Produces(feishu):`build_account_confirm_card(user_id, name, email, kind_label, expires, mode="create") -> dict`(按钮 value `{"action": "acct_confirm"|"acct_reject", "account_id": user_id}`)
- Produces(accounts):`main(argv=None) -> int`,子命令 `create`、`register`、`resend-confirm`(后续任务往 `_COMMANDS` 里加)

- [ ] **Step 1: 内存版 Casdoor** —— 新建 `tests/_fake_casdoor.py`:

```python
"""测试用内存 Casdoor:get-user(userId / email)、get-users、add-user、update-user(按 columns 落列)、get-roles。
生命周期类测试用它代替逐个 monkeypatch casdoor_admin,让读改写的真实代码路径都跑到。"""
import copy
import json

import httpx


class FakeCasdoor:
    def __init__(self, org: str = "even-test"):
        self.org = org
        self.users: dict[str, dict] = {}
        self.fail: set[str] = set()   # 命中的路径返回 status=error

    def add(self, **user) -> dict:
        user.setdefault("owner", self.org)
        user.setdefault("name", user["id"])
        user.setdefault("groups", [])
        user.setdefault("isForbidden", False)
        user.setdefault("properties", {})
        self.users[user["id"]] = user
        return user

    def props(self, user_id: str) -> dict:
        return self.users[user_id]["properties"]

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle), base_url="http://casdoor")

    @staticmethod
    def _ok(data=None):
        return httpx.Response(200, json={"status": "ok", "data": data})

    def _handle(self, req: httpx.Request) -> httpx.Response:
        path, q = req.url.path, req.url.params
        if path in self.fail:
            return httpx.Response(200, json={"status": "error", "msg": "boom", "data": None})
        if path == "/api/get-user":
            if "email" in q:
                user = next((u for u in self.users.values() if u.get("email") == q["email"]), None)
            else:
                user = self.users.get(q.get("userId"))
            return self._ok(copy.deepcopy(user))
        if path == "/api/get-users":
            return self._ok([copy.deepcopy(u) for u in self.users.values()])
        if path == "/api/get-roles":
            return self._ok([])
        if path == "/api/add-user":
            body = json.loads(req.content)
            if body["id"] in self.users:
                return httpx.Response(200, json={"status": "error", "msg": "exists"})
            self.add(**body)
            return self._ok("Affected")
        if path == "/api/update-user":
            owner, name = q["id"].split("/", 1)
            target = next((u for u in self.users.values() if u["owner"] == owner and u["name"] == name), None)
            if target is None:
                return httpx.Response(200, json={"status": "error", "msg": "no such user"})
            body = json.loads(req.content)
            for col in q.get("columns", "").split(","):
                target[col] = copy.deepcopy(body.get(col))
            return self._ok("Affected")
        return httpx.Response(404)
```

- [ ] **Step 2: 写失败的测试** —— 新建 `tests/test_account_lifecycle.py`:

```python
"""受管账号生命周期(里程碑 4a):真实代码 + 内存版 Casdoor,飞书发送改成捕获。"""
import asyncio
import json
from datetime import timedelta

import pytest

from even_auth_gov import account_lifecycle as lc, accounts as acc, approval_store
from tests._fake_casdoor import FakeCasdoor


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("CASDOOR_ORG", "even-test")
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    monkeypatch.setenv("APPROVER_FEISHU_ID", "ou_root")
    monkeypatch.delenv("ACCOUNT_EVENTS_FILE", raising=False)
    sent = {"cards": [], "texts": [], "card_ok": True}
    async def send_card(to, card, receive_id_type="open_id"):
        sent["cards"].append((to, card)); return sent["card_ok"]
    async def send_text(to, text, receive_id_type="open_id"):
        sent["texts"].append((to, text)); return True
    monkeypatch.setattr("even_auth_gov.feishu.send_card", send_card)
    monkeypatch.setattr("even_auth_gov.feishu.send_text", send_text)
    async def no_sleep(*a, **k): return None
    monkeypatch.setattr("even_auth_gov.offboard.asyncio.sleep", no_sleep)
    fc = FakeCasdoor()
    fc.add(id="ou_boss", displayName="李四", email="boss@e.com")
    fc.add(id="ou_other", displayName="王五", email="other@e.com")
    return {"fc": fc, "sent": sent, "dir": tmp_path}


def run(fc, fn):
    async def go():
        async with fc.client() as c:
            return await fn(c)
    return asyncio.run(go())


def day(n: int) -> str:
    return (acc.today() + timedelta(days=n)).isoformat()


def events(env):
    path = env["dir"] / "account_events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def buttons(card):
    return [b["value"] for b in card["elements"][-1]["actions"]]


def create(env, **over):
    args = dict(kind="partner", name="张三", email="z@partner.com", sponsor_email="boss@e.com", expires=day(30))
    args.update(over)
    return run(env["fc"], lambda c: lc.create(c, **args))


def click(env, action, operator, user_id):
    return run(env["fc"], lambda c: lc.handle_action(action, operator, user_id, c))


# ── 建号 ──

def test_create_builds_disabled_account_and_asks_sponsor(env):
    r = create(env)
    u = env["fc"].users[r["user_id"]]
    assert r["user_id"].startswith("x_") and len(r["password"]) == 20 and r["confirm_sent"] is True
    assert u["isForbidden"] is True and u["needUpdatePassword"] is True and u["email"] == "z@partner.com"
    assert u["properties"] == {"evauth_kind": "partner", "evauth_sponsor": "ou_boss",
                               "evauth_expires": day(30), "evauth_origin": "created"}
    to, card = env["sent"]["cards"][0]
    assert to == "ou_boss"
    assert buttons(card) == [{"action": "acct_confirm", "account_id": r["user_id"]},
                             {"action": "acct_reject", "account_id": r["user_id"]}]
    assert [e["event"] for e in events(env)] == ["created"]
    assert r["password"] not in (env["dir"] / "account_events.jsonl").read_text(encoding="utf-8")


def test_service_account_is_not_forced_to_change_password(env):
    r = create(env, kind="service")
    assert env["fc"].users[r["user_id"]]["needUpdatePassword"] is False


@pytest.mark.parametrize("over,msg", [
    ({"kind": "vendor"}, "类型"),
    ({"expires": "2020-01-01"}, "早于今天"),
    ({"expires": day(181)}, "最长"),
    ({"sponsor_email": "nobody@e.com"}, "不是飞书账号"),
    ({"email": "boss@e.com"}, "已被账号"),
    ({"name": " "}, "姓名"),
])
def test_create_rejects_bad_input_and_creates_nothing(env, over, msg):
    before = set(env["fc"].users)
    with pytest.raises(lc.AccountError, match=msg):
        create(env, **over)
    assert set(env["fc"].users) == before and env["sent"]["cards"] == []


def test_create_rejects_disabled_or_non_feishu_sponsor(env):
    env["fc"].users["ou_boss"]["isForbidden"] = True
    with pytest.raises(lc.AccountError, match="已停用"):
        create(env)
    env["fc"].add(id="x_svc", email="svc@e.com", properties={"evauth_kind": "service"})
    with pytest.raises(lc.AccountError, match="不是飞书账号"):
        create(env, sponsor_email="svc@e.com")


def test_create_aborts_when_casdoor_unreadable(env):
    env["fc"].fail.add("/api/get-user")
    with pytest.raises(lc.AccountError, match="读不到 Casdoor"):
        create(env)


def test_create_reports_card_failure_but_keeps_account(env):
    env["sent"]["card_ok"] = False
    r = create(env)
    assert r["confirm_sent"] is False and r["user_id"] in env["fc"].users


# ── 负责人确认 / 拒绝 ──

def test_sponsor_confirm_enables_created_account(env):
    r = create(env)
    msg = click(env, "acct_confirm", "ou_boss", r["user_id"])
    u = env["fc"].users[r["user_id"]]
    assert "账号已启用" in msg and u["isForbidden"] is False and u["properties"]["evauth_confirmed_at"]
    assert acc.eligibility(u, acc.today()) == ""
    assert [e["event"] for e in events(env)] == ["created", "confirmed"]


def test_only_the_sponsor_can_confirm(env):
    r = create(env)
    assert "只有该账号的负责人" in click(env, "acct_confirm", "ou_other", r["user_id"])
    assert env["fc"].users[r["user_id"]]["isForbidden"] is True


def test_confirm_after_expiry_does_not_enable(env):
    r = create(env)
    env["fc"].props(r["user_id"])["evauth_expires"] = day(-1)
    assert "没有启用" in click(env, "acct_confirm", "ou_boss", r["user_id"])
    assert env["fc"].users[r["user_id"]]["isForbidden"] is True


def test_sponsor_reject_keeps_created_account_disabled(env):
    r = create(env)
    assert "保持停用" in click(env, "acct_reject", "ou_boss", r["user_id"])
    u = env["fc"].users[r["user_id"]]
    assert u["isForbidden"] is True and u["properties"]["evauth_rejected_at"]
    assert "拒绝过" in click(env, "acct_confirm", "ou_boss", r["user_id"])


def test_repeat_confirm_is_idempotent(env):
    r = create(env)
    click(env, "acct_confirm", "ou_boss", r["user_id"])
    assert "已确认过" in click(env, "acct_confirm", "ou_boss", r["user_id"])


def test_unknown_account_or_not_managed(env):
    assert "找不到" in click(env, "acct_confirm", "ou_boss", "x_none")
    assert "不是受管账号" in click(env, "acct_confirm", "ou_boss", "ou_other")


# ── 纳管存量账号 ──

def _legacy(env, forbidden=False):
    return env["fc"].add(id="uuid-legacy", email="legacy@partner.com", displayName="老外包", isForbidden=forbidden,
                         groups=["even-test/approved-app-a"])


def test_register_adopts_existing_account_without_touching_state(env):
    _legacy(env)
    r = run(env["fc"], lambda c: lc.register(c, "legacy@partner.com", "partner", "boss@e.com", day(60)))
    u = env["fc"].users["uuid-legacy"]
    assert r["confirm_sent"] is True and u["isForbidden"] is False and u["groups"] == ["even-test/approved-app-a"]
    assert u["properties"]["evauth_origin"] == "registered" and u["properties"]["evauth_sponsor"] == "ou_boss"
    assert acc.eligibility(u, acc.today()) == "unconfirmed"
    msg = click(env, "acct_confirm", "ou_boss", "uuid-legacy")
    assert "已启用" not in msg and env["fc"].users["uuid-legacy"]["isForbidden"] is False


def test_confirm_does_not_enable_a_disabled_registered_account(env):
    _legacy(env, forbidden=True)
    run(env["fc"], lambda c: lc.register(c, "uuid-legacy", "staff", "boss@e.com", day(60)))
    click(env, "acct_confirm", "ou_boss", "uuid-legacy")
    assert env["fc"].users["uuid-legacy"]["isForbidden"] is True


def test_rejecting_an_enabled_registered_account_disables_it(env):
    _legacy(env)
    run(env["fc"], lambda c: lc.register(c, "uuid-legacy", "partner", "boss@e.com", day(60)))
    assert "已停用" in click(env, "acct_reject", "ou_boss", "uuid-legacy")
    u = env["fc"].users["uuid-legacy"]
    assert u["isForbidden"] is True and u["groups"] == []


def test_register_refuses_feishu_and_already_managed(env):
    with pytest.raises(lc.AccountError, match="飞书账号"):
        run(env["fc"], lambda c: lc.register(c, "ou_other", "partner", "boss@e.com", day(30)))
    r = create(env)
    with pytest.raises(lc.AccountError, match="已经是受管账号"):
        run(env["fc"], lambda c: lc.register(c, r["user_id"], "partner", "boss@e.com", day(30)))


def test_resend_confirm(env):
    r = create(env)
    assert run(env["fc"], lambda c: lc.resend_confirm(c, "z@partner.com")) is True
    assert len(env["sent"]["cards"]) == 2
    click(env, "acct_confirm", "ou_boss", r["user_id"])
    with pytest.raises(lc.AccountError, match="不需要重发"):
        run(env["fc"], lambda c: lc.resend_confirm(c, r["user_id"]))
```

`tests/test_feishu_card.py` 追加:

```python
@pytest.mark.parametrize("mode,phrase", [("create", "建了账号"), ("register", "纳入管理"), ("transfer", "负责人改为你")])
def test_account_confirm_card(mode, phrase):
    card = feishu.build_account_confirm_card("x_1", "张三", "z@p.com", "外部合作方", "2027-02-01", mode=mode)
    md = card["elements"][0]["content"]
    assert phrase in md and "2027-02-01" in md and "张三" in md
    assert [b["value"] for b in card["elements"][-1]["actions"]] == [
        {"action": "acct_confirm", "account_id": "x_1"}, {"action": "acct_reject", "account_id": "x_1"}]
```

(若 `tests/test_feishu_card.py` 还没有 `import pytest`,在顶部补上。)

`tests/test_ws_wiring.py` 追加:

```python
def test_process_card_routes_account_buttons_to_lifecycle(monkeypatch):
    from even_auth_gov import account_lifecycle, decision
    seen, texts = [], []
    async def fake_action(action, operator_id, user_id, client):
        seen.append((action, operator_id, user_id)); return "已确认负责 张三 的账号,账号已启用"
    async def fake_decision(*a, **k):
        raise AssertionError("账号按钮不该走应用审批")
    async def fake_send_text(to, text, receive_id_type="open_id"):
        texts.append((to, text)); return True
    monkeypatch.setattr(account_lifecycle, "handle_action", fake_action)
    monkeypatch.setattr(decision, "handle", fake_decision)
    monkeypatch.setattr("even_auth_gov.feishu.send_text", fake_send_text)
    asyncio.run(feishu_ws._process_card(_card_data({"action": "acct_confirm", "account_id": "x_1"}, "ou_boss")))
    assert seen == [("acct_confirm", "ou_boss", "x_1")]
    assert texts == [("ou_boss", "👤 已确认负责 张三 的账号,账号已启用")]
```

`tests/test_accounts.py` 追加(命令行):

```python
from tests._fake_casdoor import FakeCasdoor


def _cli_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CASDOOR_ORG", "even-test")
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    monkeypatch.setenv("TESTING", "1")   # 飞书发送被抑制、返回成功
    fc = FakeCasdoor()
    fc.add(id="ou_boss", displayName="李四", email="boss@e.com")
    monkeypatch.setattr("even_auth_gov.casdoor_admin.make_client", fc.client)
    return fc


def test_cli_create_prints_one_time_password(monkeypatch, tmp_path, capsys):
    fc = _cli_env(monkeypatch, tmp_path)
    exp = (acc.today() + timedelta(days=30)).isoformat()
    code = acc.main(["create", "--kind", "partner", "--name", "张三", "--email", "z@p.com",
                     "--sponsor", "boss@e.com", "--expires", exp])
    out = capsys.readouterr().out
    created = [u for u in fc.users if u.startswith("x_")]
    assert code == 0 and len(created) == 1 and created[0] in out
    assert "初始密码" in out and "z@p.com" in out and "确认卡片已发给负责人 李四" in out


def test_cli_validation_error_exits_2(monkeypatch, tmp_path, capsys):
    _cli_env(monkeypatch, tmp_path)
    code = acc.main(["create", "--kind", "partner", "--name", "张三", "--email", "z@p.com",
                     "--sponsor", "boss@e.com", "--expires", "2020-01-01"])
    assert code == 2 and "早于今天" in capsys.readouterr().err
```

- [ ] **Step 3: 运行确认失败** —— `$PYTEST tests/test_account_lifecycle.py tests/test_feishu_card.py tests/test_ws_wiring.py tests/test_accounts.py`,预期 `ModuleNotFoundError: even_auth_gov.account_lifecycle` 等。

- [ ] **Step 4: 确认卡片** —— `even_auth_gov/feishu.py` 末尾追加:

```python
def build_account_confirm_card(user_id: str, name: str, email: str, kind_label: str, expires: str,
                               mode: str = "create") -> dict:
    """负责人确认卡片(里程碑 4a)。mode:create 新建 / register 纳管已有账号 / transfer 换负责人。
    按钮 value 带 account_id,点击由 feishu_ws 转给 account_lifecycle.handle_action。"""
    who = f"**{name}**({email},{kind_label})" if email else f"**{name}**({kind_label})"
    intro = {
        "create": f"管理员为 {who} 建了账号,登记你为负责人,有效期至 **{expires}**。确认后账号才会启用。",
        "register": f"管理员把已有账号 {who} 纳入管理,登记你为负责人,有效期至 **{expires}**。",
        "transfer": f"管理员把 {who} 的负责人改为你,有效期至 **{expires}**。确认后由你负责。",
    }[mode]
    duty = "你离职时这个账号会被一起停用;到期前会提醒你续期,不续期就到期自动停用。"
    base = {"account_id": user_id}
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "👤 请确认账号负责人"}, "template": "blue"},
        "elements": [
            {"tag": "markdown", "content": f"{intro}\n\n{duty}"},
            {"tag": "hr"},
            {"tag": "action", "actions": [
                {"tag": "button", "text": {"tag": "plain_text", "content": "确认负责"}, "type": "primary",
                 "value": {"action": "acct_confirm", **base}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "拒绝"}, "type": "danger",
                 "value": {"action": "acct_reject", **base}},
            ]},
        ],
    }
```

- [ ] **Step 5: 生命周期模块** —— 新建 `even_auth_gov/account_lifecycle.py`:

```python
"""受管账号的生命周期操作(会读写 Casdoor、发飞书)—— 里程碑 4a。

本任务:建号 create、纳管 register、负责人确认/拒绝(handle_action)、重发确认卡片。
后续任务在此追加:到期扫描与续期(sweep / renew)、负责人离职连带停用(cascade_sponsor)、换负责人、设密码、列表。
规则在 accounts.py;卡片样式在 feishu.py;卡片按钮由 feishu_ws 转到 handle_action。
停用一律走 offboard.apply(重试 + 告警 + 清准入组和角色)。变更历史追加写到 account_events.jsonl,写失败只记日志。
"""
from __future__ import annotations

import json
import logging
import secrets
import string
import threading

from even_auth_gov import accounts as acc, casdoor_admin as ca, settings

logger = logging.getLogger(__name__)
_EVENTS_LOCK = threading.Lock()
_PW_SYMBOLS = "!@#%^*-_=+"


class AccountError(Exception):
    """管理员操作被拒:校验不通过或 Casdoor 读写失败。命令行打印信息并以退出码 2 结束。"""


def log_event(event: str, user: dict, actor: str = "", detail: str = "") -> None:
    p = acc.props(user)
    rec = {"ts": acc.now_iso(), "event": event, "user_id": user.get("id", ""), "name": acc.display_name(user),
           "email": user.get("email", ""), "kind": p.get(acc.P_KIND, ""), "sponsor": p.get(acc.P_SPONSOR, ""),
           "expires": p.get(acc.P_EXPIRES, ""), "actor": actor, "detail": detail}
    try:
        path = settings.account_events_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with _EVENTS_LOCK, open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("写账号变更记录失败 %s %s: %s", event, user.get("id"), e)


def new_password(length: int = 20) -> str:
    """随机初始密码,保证含大小写字母、数字和符号(兼容 Casdoor 组织的密码复杂度要求)。"""
    alphabet = string.ascii_letters + string.digits + _PW_SYMBOLS
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in _PW_SYMBOLS for c in pw)):
            return pw


def new_user_id() -> str:
    return "x_" + secrets.token_hex(8)


def _merged(user: dict, updates: dict) -> dict:
    """和 casdoor_admin.set_properties 同样的合并规则,用来在本地同步刚写进去的属性。"""
    p = acc.props(user)
    for key, value in updates.items():
        if value is None:
            p.pop(key, None)
        else:
            p[key] = str(value)
    return p


async def _notify(to: str, text: str) -> None:
    if not to or to == "ou_xxx":
        return
    try:
        from even_auth_gov.feishu import send_text
        await send_text(to, text)
    except Exception as e:
        logger.warning("账号通知发送失败 → %s: %s", to, e)


async def _resolve_sponsor(client, email: str) -> dict:
    ok, user = await ca.get_user_by_email(client, email.strip())
    if not ok:
        raise AccountError("查询负责人失败(读不到 Casdoor),什么都没改")
    if not user or not acc.is_feishu(user):
        raise AccountError(f"负责人 {email} 不是飞书账号(按邮箱查不到以 ou_ 开头的账号)")
    if user.get("isForbidden"):
        raise AccountError(f"负责人 {email} 的账号已停用")
    return user


async def _resolve_user(client, ref: str) -> dict:
    """邮箱或账号 id → 用户对象。"""
    ref = ref.strip()
    if "@" in ref:
        ok, user = await ca.get_user_by_email(client, ref)
        if not ok:
            raise AccountError("查询账号失败(读不到 Casdoor)")
    else:
        user = await ca.get_user(client, ref)
    if not user:
        raise AccountError(f"找不到账号 {ref}")
    return user


def _check_kind_and_expiry(kind: str, expires: str):
    if kind not in acc.KINDS:
        raise AccountError(f"类型只能是 {'/'.join(acc.KINDS)}")
    exp = acc.parse_date(expires)
    err = acc.expiry_error(exp, acc.today(), settings.account_max_days())
    if err:
        raise AccountError(err)
    return exp


async def send_confirm(user: dict, mode: str, to: str = "") -> bool:
    """给负责人(换负责人时是新负责人)发确认卡片,送达返回 True。"""
    from even_auth_gov.feishu import build_account_confirm_card, send_card
    p = acc.props(user)
    target = to or p.get(acc.P_SPONSOR, "")
    card = build_account_confirm_card(user.get("id", ""), acc.display_name(user), user.get("email", ""),
                                      acc.KINDS.get(p.get(acc.P_KIND), ""), p.get(acc.P_EXPIRES, ""), mode=mode)
    try:
        return bool(await send_card(target, card))
    except Exception as e:
        logger.warning("确认卡片发送失败 %s → %s: %s", user.get("id"), target, e)
        return False


async def create(client, kind: str, name: str, email: str, sponsor_email: str, expires: str,
                 actor: str = "admin") -> dict:
    """建号:校验 → 建停用中的 Casdoor 账号 → 给负责人发确认卡片。任何校验不过都抛 AccountError,什么都不建。
    返回的 password 只给命令行打印一次,不落盘、不进变更记录。"""
    exp = _check_kind_and_expiry(kind, expires)
    name, email = name.strip(), email.strip()
    if not name or "@" not in email:
        raise AccountError("姓名不能为空,邮箱格式不对")
    sponsor = await _resolve_sponsor(client, sponsor_email)
    ok, existing = await ca.get_user_by_email(client, email)
    if not ok:
        raise AccountError("查询邮箱是否已被占用失败(读不到 Casdoor),什么都没建")
    if existing:
        raise AccountError(f"邮箱 {email} 已被账号 {existing.get('id')} 使用")
    uid, password = new_user_id(), new_password()
    user = {"owner": ca._org(), "name": uid, "id": uid, "displayName": name, "email": email,
            "password": password, "type": "normal-user", "isForbidden": True,
            "needUpdatePassword": kind != "service",
            "properties": {acc.P_KIND: kind, acc.P_SPONSOR: sponsor["id"], acc.P_EXPIRES: exp.isoformat(),
                           acc.P_ORIGIN: "created"}}
    if not await ca.create_user(client, user):
        raise AccountError("Casdoor 建号失败,什么都没建")
    log_event("created", user, actor)
    sent = await send_confirm(user, mode="create")
    return {"user_id": uid, "email": email, "password": password,
            "sponsor": acc.display_name(sponsor), "confirm_sent": sent}


async def register(client, ref: str, kind: str, sponsor_email: str, expires: str, actor: str = "admin") -> dict:
    """纳管存量的非飞书账号:只写登记信息、发确认卡片,不改启停、不动已有准入。"""
    exp = _check_kind_and_expiry(kind, expires)
    user = await _resolve_user(client, ref)
    if acc.is_feishu(user):
        raise AccountError("飞书账号不需要纳管(离职按飞书在职状态处理)")
    if acc.is_managed(user):
        raise AccountError("已经是受管账号:续期用 renew,换负责人用 transfer")
    sponsor = await _resolve_sponsor(client, sponsor_email)
    updates = {acc.P_KIND: kind, acc.P_SPONSOR: sponsor["id"], acc.P_EXPIRES: exp.isoformat(),
               acc.P_ORIGIN: "registered", acc.P_CONFIRMED: None, acc.P_REJECTED: None,
               acc.P_REMINDED: None, acc.P_SPONSOR_PENDING: None}
    if not await ca.set_properties(client, user["id"], updates):
        raise AccountError("写入登记信息失败(Casdoor)")
    user = {**user, "properties": _merged(user, updates)}
    log_event("registered", user, actor)
    sent = await send_confirm(user, mode="register")
    return {"user_id": user["id"], "sponsor": acc.display_name(sponsor), "confirm_sent": sent}


async def resend_confirm(client, ref: str) -> bool:
    user = await _resolve_user(client, ref)
    if not acc.is_managed(user):
        raise AccountError("不是受管账号")
    p = acc.props(user)
    if p.get(acc.P_SPONSOR_PENDING):
        return await send_confirm(user, mode="transfer", to=p[acc.P_SPONSOR_PENDING])
    if p.get(acc.P_CONFIRMED) or p.get(acc.P_REJECTED):
        raise AccountError("负责人已经确认或拒绝过,不需要重发")
    return await send_confirm(user, mode="register" if p.get(acc.P_ORIGIN) == "registered" else "create")


async def handle_action(action: str, operator_id: str, user_id: str, client) -> str:
    """账号卡片按钮。返回给点击人看的结果。一律按 Casdoor 实时数据核对点的人是不是当前负责人。"""
    user = await ca.get_user(client, user_id) if user_id else None
    if user is None:
        return "找不到这个账号,或暂时读不到 Casdoor,请稍后再点"
    if not acc.is_managed(user):
        return "这个账号不是受管账号"
    if operator_id != acc.props(user).get(acc.P_SPONSOR):
        return "只有该账号的负责人可以操作"
    if action == "acct_confirm":
        return await _confirm(client, user, operator_id)
    if action == "acct_reject":
        return await _reject(client, user, operator_id)
    return f"未知操作: {action}"


async def _confirm(client, user: dict, operator_id: str) -> str:
    p, name = acc.props(user), acc.display_name(user)
    if p.get(acc.P_REJECTED):
        return f"你已拒绝过负责 {name} 的账号;如需开通请联系管理员"
    if p.get(acc.P_CONFIRMED):
        return f"{name} 的账号已确认过"
    if not await ca.set_properties(client, user["id"], {acc.P_CONFIRMED: acc.now_iso()}):
        return "确认失败:写入 Casdoor 失败,请稍后再点"
    log_event("confirmed", user, operator_id)
    msg = f"已确认负责 {name} 的账号"
    if p.get(acc.P_ORIGIN) == "created" and user.get("isForbidden"):
        left = acc.days_left(user, acc.today())
        if left is None or left < 0:
            msg += ";但已过了到期日,账号没有启用,请联系管理员续期"
        elif await ca.enable_user(client, user["id"]):
            msg += ",账号已启用"
        else:
            logger.warning("负责人已确认但启用账号失败 %s", user["id"])
            msg += ";启用账号失败,请联系管理员"
    return msg


async def _reject(client, user: dict, operator_id: str) -> str:
    p, name = acc.props(user), acc.display_name(user)
    if p.get(acc.P_REJECTED):
        return f"已拒绝过负责 {name} 的账号"
    if p.get(acc.P_CONFIRMED):
        return f"{name} 的账号已确认过;不再负责请联系管理员换负责人"
    if not await ca.set_properties(client, user["id"], {acc.P_REJECTED: acc.now_iso()}):
        return "操作失败:写入 Casdoor 失败,请稍后再点"
    log_event("rejected", user, operator_id)
    if user.get("isForbidden"):
        return f"已拒绝负责 {name} 的账号,账号保持停用"
    from even_auth_gov import offboard   # 纳管的账号还启用着:没人负责就停掉
    await offboard.apply(user["id"], name, "sponsor_rejected", client)
    return f"已拒绝负责 {name} 的账号,该账号已停用"
```

- [ ] **Step 6: 卡片路由** —— `even_auth_gov/feishu_ws.py:_process_card`:在 `operator_id = operator.open_id if operator else ""` 之后插入:

```python
    if str(value.get("action", "")).startswith("acct_"):
        # 里程碑 4a:受管账号卡片(负责人确认 / 续期)不走应用审批。
        from even_auth_gov import account_lifecycle
        from even_auth_gov.feishu import send_text
        async with httpx.AsyncClient(base_url=settings.casdoor_endpoint()) as client:
            msg = await account_lifecycle.handle_action(value["action"], operator_id, value.get("account_id", ""), client)
        if operator_id:
            try:
                await send_text(operator_id, f"👤 {msg}")
            except Exception as e:
                logger.warning("账号操作结果回告失败 %s: %s", operator_id, e)
        return
```

- [ ] **Step 7: 命令行** —— `even_auth_gov/accounts.py` 末尾追加:

```python
# ── 管理员命令行:python -m even_auth_gov.accounts <子命令> ──

def _lc():
    from even_auth_gov import account_lifecycle   # 延迟导入:account_lifecycle 依赖本模块
    return account_lifecycle


async def _cmd_create(client, a) -> str:
    r = await _lc().create(client, a.kind, a.name, a.email, a.sponsor, a.expires)
    lines = [f"已建号(停用中,负责人确认后启用):{r['user_id']}",
             f"登录邮箱:{r['email']}",
             f"初始密码(只显示这一次,请线下交给对方):{r['password']}"]
    lines.append(f"确认卡片已发给负责人 {r['sponsor']}" if r["confirm_sent"] else
                 f"⚠️ 确认卡片发送失败,稍后运行:python -m even_auth_gov.accounts resend-confirm --user {r['email']}")
    return "\n".join(lines)


async def _cmd_register(client, a) -> str:
    r = await _lc().register(client, a.user, a.kind, a.sponsor, a.expires)
    tail = f"确认卡片已发给负责人 {r['sponsor']}" if r["confirm_sent"] else \
        f"⚠️ 确认卡片发送失败,稍后运行 resend-confirm --user {a.user}"
    return f"已纳管 {r['user_id']}(启停状态和已有准入不变)。{tail}"


async def _cmd_resend(client, a) -> str:
    return "确认卡片已重发" if await _lc().resend_confirm(client, a.user) else "⚠️ 确认卡片发送失败"


_COMMANDS = {"create": _cmd_create, "register": _cmd_register, "resend-confirm": _cmd_resend}


def _parser():
    import argparse
    ap = argparse.ArgumentParser(prog="python -m even_auth_gov.accounts",
                                 description="受管的非飞书账号(外部合作方 / 无飞书员工 / 服务账号)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="建号(停用中,负责人确认后启用)")
    c.add_argument("--kind", required=True, choices=sorted(KINDS))
    c.add_argument("--name", required=True)
    c.add_argument("--email", required=True, help="对方的邮箱,也是登录名")
    c.add_argument("--sponsor", required=True, help="负责人的飞书邮箱")
    c.add_argument("--expires", required=True, help="到期日 YYYY-MM-DD")
    r = sub.add_parser("register", help="把已有的非飞书账号纳入管理(不改启停、不动已有准入)")
    r.add_argument("--user", required=True, help="邮箱或账号 id")
    r.add_argument("--kind", required=True, choices=sorted(KINDS))
    r.add_argument("--sponsor", required=True, help="负责人的飞书邮箱")
    r.add_argument("--expires", required=True, help="到期日 YYYY-MM-DD")
    s = sub.add_parser("resend-confirm", help="重发负责人确认卡片")
    s.add_argument("--user", required=True, help="邮箱或账号 id")
    return ap, sub


def main(argv: list[str] | None = None) -> int:
    import asyncio
    import sys
    from even_auth_gov import casdoor_admin as ca, logging_setup
    ap, _ = _parser()
    args = ap.parse_args(argv)
    logging_setup.setup()

    async def go():
        async with ca.make_client() as client:
            return await _COMMANDS[args.cmd](client, args)

    try:
        print(asyncio.run(go()))
    except _lc().AccountError as e:
        print(f"失败:{e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys
    sys.exit(main())
```

- [ ] **Step 8: 运行确认通过** —— 跑 Step 3 的命令,再全量 `$PYTEST`。

- [ ] **Step 9: 提交**

```bash
git add even_auth_gov/account_lifecycle.py even_auth_gov/accounts.py even_auth_gov/feishu.py even_auth_gov/feishu_ws.py \
        tests/_fake_casdoor.py tests/test_account_lifecycle.py tests/test_accounts.py tests/test_feishu_card.py tests/test_ws_wiring.py
git commit -m "M4a: invite-only account creation and adoption with sponsor confirmation card"
```

---

### Task 6: 到期扫描、续期提醒、续期、重新申请

**Files:**
- Modify: `even_auth_gov/account_lifecycle.py`、`even_auth_gov/feishu.py`(加 `build_account_renew_card`)、`even_auth_gov/approval_store.py`、`even_auth_gov/scheduler.py:_run_pending_job`、`even_auth_gov/reconcile.py:run`、`even_auth_gov/accounts.py`(加 `renew` 子命令)、`even_auth_gov/audit_export.py`(COLUMNS 加 `reopened_at`)
- Test: `tests/test_account_lifecycle.py`、`tests/test_approval_store.py`、`tests/test_scheduler.py`、`tests/test_reconcile.py`、`tests/test_feishu_card.py`、`tests/test_accounts.py`

**Interfaces:**
- Consumes:Task 5 全部;`offboard.apply`
- Produces:`account_lifecycle.sweep(client) -> None`;`renew(client, user_or_ref, actor, expires="") -> dict`(`expires, enabled, reopened`);`handle_action` 支持 `acct_renew` / `acct_no_renew`;`approval_store.reopen_after_renewal(open_id) -> int`;`feishu.build_account_renew_card(user_id, name, kind_label, expires, days_left, renew_days) -> dict`(按钮 `acct_renew` / `acct_no_renew`)

- [ ] **Step 1: 写失败的测试**

`tests/test_account_lifecycle.py` 追加:

```python
def active(env, uid="x_a", sponsor="ou_boss", expires=None, groups=("even-test/approved-app-a",), **props):
    p = {"evauth_kind": "partner", "evauth_sponsor": sponsor, "evauth_expires": expires or day(30),
         "evauth_confirmed_at": "2026-11-01T00:00:00+00:00", "evauth_origin": "created"}
    p.update(props)
    return env["fc"].add(id=uid, displayName=f"受管{uid}", email=f"{uid}@p.com", groups=list(groups),
                         properties={k: v for k, v in p.items() if v is not None})


def sweep(env):
    run(env["fc"], lc.sweep)


@pytest.mark.parametrize("over,reason,event", [
    ({"expires": "2020-01-01"}, "account_expired", "expired"),
    ({"evauth_kind": "vendor"}, "account_invalid", "invalid"),
    ({"evauth_rejected_at": "2026-11-02T00:00:00+00:00"}, "sponsor_rejected", "rejected_disabled"),
])
def test_sweep_disables_lapsed_accounts_via_offboard(env, over, reason, event):
    approval_store.mark_pending("x_a", "app-a", {"name": "a"})
    approval_store.mark_approved("x_a", "app-a", "ou_appr")
    active(env, **over)
    sweep(env)
    u = env["fc"].users["x_a"]
    assert u["isForbidden"] is True and u["groups"] == []
    rec = approval_store.get("x_a", "app-a")
    assert rec["status"] == "disabled" and rec["disabled_reason"] == reason
    assert events(env)[-1]["event"] == event


def test_sweep_disables_when_sponsor_is_disabled_or_gone(env):
    env["fc"].users["ou_other"]["isForbidden"] = True
    active(env, uid="x_a", sponsor="ou_other")
    active(env, uid="x_b", sponsor="ou_ghost")
    sweep(env)
    assert env["fc"].users["x_a"]["isForbidden"] and env["fc"].users["x_b"]["isForbidden"]
    assert any(to == "ou_root" and "负责人已停用" in t for to, t in env["sent"]["texts"])


def test_expiry_notice_goes_to_sponsor(env):
    active(env, expires="2020-01-01")
    sweep(env)
    assert any(to == "ou_boss" and "已停用" in t for to, t in env["sent"]["texts"])


def test_sweep_leaves_feishu_unregistered_disabled_and_healthy_accounts_alone(env):
    env["fc"].add(id="uuid-s", email="s@x.com", groups=["even-test/approved-app-a"])   # 未登记:只拦不停
    active(env, uid="x_off", expires="2020-01-01", groups=())
    env["fc"].users["x_off"]["isForbidden"] = True
    active(env, uid="x_ok")
    sweep(env)
    assert env["fc"].users["uuid-s"]["isForbidden"] is False
    assert env["fc"].users["x_ok"]["isForbidden"] is False
    assert env["fc"].users["ou_boss"]["isForbidden"] is False


def test_sweep_skips_everything_when_users_unreadable(env):
    active(env, expires="2020-01-01")
    env["fc"].fail.add("/api/get-users")
    sweep(env)
    assert env["fc"].users["x_a"]["isForbidden"] is False


def test_reminders_once_per_stage(env, monkeypatch):
    exp = day(14)                      # 先存下来:下面会把"今天"往后挪,day() 会跟着变
    active(env, expires=exp)
    sweep(env); sweep(env)
    assert len(env["sent"]["cards"]) == 1
    to, card = env["sent"]["cards"][0]
    assert to == "ou_boss" and [b["action"] for b in buttons(card)] == ["acct_renew", "acct_no_renew"]
    assert env["fc"].props("x_a")["evauth_reminded"] == f"{exp}:14"
    real_today = acc.today()
    monkeypatch.setattr(acc, "today", lambda: real_today + timedelta(days=11))   # 剩 3 天
    sweep(env)
    assert len(env["sent"]["cards"]) == 2 and env["fc"].props("x_a")["evauth_reminded"] == f"{exp}:3"


def test_reminder_send_failure_is_retried(env):
    active(env, expires=day(3))
    env["sent"]["card_ok"] = False
    sweep(env)
    assert "evauth_reminded" not in env["fc"].props("x_a")


def test_sponsor_renews_from_card(env):
    active(env, expires=day(10), evauth_reminded=f"{day(10)}:14")
    msg = click(env, "acct_renew", "ou_boss", "x_a")
    assert day(100) in msg
    p = env["fc"].props("x_a")
    assert p["evauth_expires"] == day(100) and "evauth_reminded" not in p
    assert events(env)[-1]["event"] == "renewed"
    assert "只有该账号的负责人" in click(env, "acct_renew", "ou_other", "x_a")


def test_no_renew_stops_reminders(env, monkeypatch):
    active(env, expires=day(14))
    assert "不再提醒" in click(env, "acct_no_renew", "ou_boss", "x_a")
    real_today = acc.today()
    monkeypatch.setattr(acc, "today", lambda: real_today + timedelta(days=12))
    sweep(env)
    assert env["sent"]["cards"] == []


def test_renew_after_expiry_reenables_and_allows_reapplying(env):
    approval_store.mark_pending("x_a", "app-a", {"name": "a"})
    approval_store.mark_approved("x_a", "app-a", "ou_appr")
    active(env, expires="2020-01-01")
    sweep(env)
    assert approval_store.mark_pending("x_a", "app-a", {"name": "a"}) is False   # 停用记录挡住重新申请
    r = run(env["fc"], lambda c: lc.renew(c, "x_a", actor="admin"))
    assert r["enabled"] is True and r["reopened"] == 1 and r["expires"] == day(90)
    assert env["fc"].users["x_a"]["isForbidden"] is False
    assert approval_store.get("x_a", "app-a")["status"] == "expired"
    assert approval_store.mark_pending("x_a", "app-a", {"name": "a"}) is True


def test_renew_refusals(env):
    active(env, uid="x_rej", evauth_rejected_at="2026-11-02T00:00:00+00:00")
    with pytest.raises(lc.AccountError, match="拒绝过"):
        run(env["fc"], lambda c: lc.renew(c, "x_rej", actor="admin"))
    env["fc"].users["ou_other"]["isForbidden"] = True
    active(env, uid="x_orphan", sponsor="ou_other")
    env["fc"].users["x_orphan"]["isForbidden"] = True
    with pytest.raises(lc.AccountError, match="负责人已停用"):
        run(env["fc"], lambda c: lc.renew(c, "x_orphan", actor="admin"))
    active(env, uid="x_c")
    with pytest.raises(lc.AccountError, match="最长"):
        run(env["fc"], lambda c: lc.renew(c, "x_c", actor="admin", expires=day(500)))


def test_renew_does_not_enable_unconfirmed_created_account(env):
    r = create(env)
    run(env["fc"], lambda c: lc.renew(c, r["user_id"], actor="admin"))
    assert env["fc"].users[r["user_id"]]["isForbidden"] is True
```

`tests/test_approval_store.py` 追加:

```python
def test_reopen_after_renewal_only_flips_lifecycle_disables(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    for app in ("app-a", "app-b", "app-c"):
        approval_store.mark_pending("x_1", app, {"name": "x"})
        approval_store.mark_approved("x_1", app, "ou_boss")
    approval_store.mark_all_disabled("x_1", "account_expired")
    approval_store._set("x_1", "app-c", disabled_reason="offboard_event")   # 别的原因停用的不动
    assert approval_store.reopen_after_renewal("x_1") == 2
    assert approval_store.get("x_1", "app-a")["status"] == "expired"
    assert approval_store.get("x_1", "app-c")["status"] == "disabled"
    assert approval_store.mark_pending("x_1", "app-a", {"name": "x"}) is True
    assert approval_store.mark_pending("x_1", "app-c", {"name": "x"}) is False
```

`tests/test_feishu_card.py` 追加:

```python
def test_account_renew_card():
    card = feishu.build_account_renew_card("x_1", "张三", "外部合作方", "2027-02-01", 3, 90)
    md = card["elements"][0]["content"]
    assert "2027-02-01" in md and "3 天后" in md and "90 天" in md
    assert [b["value"] for b in card["elements"][-1]["actions"]] == [
        {"action": "acct_renew", "account_id": "x_1"}, {"action": "acct_no_renew", "account_id": "x_1"}]
    assert "今天" in feishu.build_account_renew_card("x_1", "张三", "外部合作方", "2027-02-01", 0, 90)["elements"][0]["content"]
```

`tests/test_scheduler.py`:把现有 `test_pending_job_calls_sweep_pending` 改为同时断言账号扫描在其后执行,并加失败不外抛的测试:

```python
def test_pending_job_runs_pending_sweep_then_account_sweep(monkeypatch):
    from even_auth_gov import account_lifecycle, reconcile
    order = []
    async def fake_pending(): order.append("pending")
    async def fake_accounts(client): order.append("accounts")
    monkeypatch.setattr(reconcile, "sweep_pending", fake_pending)
    monkeypatch.setattr(account_lifecycle, "sweep", fake_accounts)
    asyncio.run(sch._run_pending_job())
    assert order == ["pending", "accounts"]


def test_pending_job_survives_account_sweep_failure(monkeypatch):
    from even_auth_gov import account_lifecycle, reconcile
    async def fake_pending(): pass
    async def boom(client): raise RuntimeError("casdoor down")
    monkeypatch.setattr(reconcile, "sweep_pending", fake_pending)
    monkeypatch.setattr(account_lifecycle, "sweep", boom)
    asyncio.run(sch._run_pending_job())   # 不抛
```

(删除原 `test_pending_job_calls_sweep_pending`,它被第一个测试覆盖。)

`tests/test_reconcile.py` 追加:

```python
def test_reconcile_runs_account_sweep_after_pending_sweep(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    from even_auth_gov import account_lifecycle
    order = []
    async def fake_pending(): order.append("pending")
    async def fake_accounts(client): order.append("accounts")
    monkeypatch.setattr(reconcile, "sweep_pending", fake_pending)
    monkeypatch.setattr(account_lifecycle, "sweep", fake_accounts)
    asyncio.run(reconcile.run(client=None))
    assert order[:2] == ["pending", "accounts"]
```

`tests/test_accounts.py` 追加:

```python
def test_cli_renew(monkeypatch, tmp_path, capsys):
    fc = _cli_env(monkeypatch, tmp_path)
    fc.add(id="x_1", email="z@p.com", properties={"evauth_kind": "partner", "evauth_sponsor": "ou_boss",
           "evauth_expires": (acc.today() + timedelta(days=5)).isoformat(), "evauth_confirmed_at": "t"})
    target = (acc.today() + timedelta(days=60)).isoformat()
    assert acc.main(["renew", "--user", "z@p.com", "--expires", target]) == 0
    assert fc.props("x_1")["evauth_expires"] == target and target in capsys.readouterr().out
```

- [ ] **Step 2: 运行确认失败** —— `$PYTEST tests/test_account_lifecycle.py tests/test_approval_store.py tests/test_feishu_card.py tests/test_scheduler.py tests/test_reconcile.py tests/test_accounts.py`。

- [ ] **Step 3: 实现**

`even_auth_gov/feishu.py` 末尾追加:

```python
def build_account_renew_card(user_id: str, name: str, kind_label: str, expires: str, days_left: int,
                             renew_days: int) -> dict:
    """到期前给负责人的续期提醒卡片(里程碑 4a)。"""
    when = "今天" if days_left == 0 else f"{days_left} 天后"
    text = (f"**{name}**({kind_label})的账号将于 **{expires}** 到期({when})。\n\n"
            f"续期后有效期延长 {renew_days} 天;不续期就到期自动停用,之后对方要重新申请各应用。")
    base = {"account_id": user_id}
    return {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": "⏳ 账号即将到期"}, "template": "yellow"},
        "elements": [
            {"tag": "markdown", "content": text},
            {"tag": "hr"},
            {"tag": "action", "actions": [
                {"tag": "button", "text": {"tag": "plain_text", "content": f"续期 {renew_days} 天"},
                 "type": "primary", "value": {"action": "acct_renew", **base}},
                {"tag": "button", "text": {"tag": "plain_text", "content": "不再续期"},
                 "type": "default", "value": {"action": "acct_no_renew", **base}},
            ]},
        ],
    }
```

`even_auth_gov/approval_store.py`:`_REAPPLY_ALLOWED = {None, "denied", "expired"}`(上方注释改为"无记录、曾被拒、或受管账号续期后重新开放(expired)");`mark_pending` docstring 的守卫说明同步加上 `expired`;`mark_disable_failed_all` 之后追加:

```python
# 受管账号(里程碑 4a)因这些原因被停用后,续期重新启用时允许重新申请。离职事件等其它原因不在此列。
_REOPEN_REASONS = {"account_expired", "account_invalid", "sponsor_left", "sponsor_rejected"}

def reopen_after_renewal(open_id: str) -> int:
    """受管账号续期后重新启用:把它因到期 / 信息异常 / 负责人离职或拒绝而停用的记录改成 expired,
    让对方可以重新申请(mark_pending 放行 expired)。返回改了几条。"""
    n = 0
    with _mutate() as recs:
        for rec in recs.values():
            if (rec.get("open_id") == open_id and rec.get("status") == "disabled"
                    and rec.get("disabled_reason") in _REOPEN_REASONS):
                rec["status"] = "expired"
                rec["reopened_at"] = _now()
                n += 1
    return n
```

`even_auth_gov/audit_export.py`:COLUMNS 里 `"disabled_reason", "disabled_at",` 改为 `"disabled_reason", "disabled_at", "reopened_at",`(测试若硬编码表头同步改)。

`even_auth_gov/account_lifecycle.py`:顶部 import 加 `import asyncio`;文件末尾追加:

```python
# ── 到期扫描、续期提醒、续期 ──

_SWEEP_LOCK = asyncio.Lock()   # 每小时任务和每日对账都会调 sweep,同一时刻只跑一个
_DISABLE_EVENTS = {"account_expired": "expired", "account_invalid": "invalid",
                   "sponsor_left": "sponsor_left", "sponsor_rejected": "rejected_disabled"}


def _lapse_reason(user: dict, on, by_id: dict) -> str:
    """启用中的受管账号该不该停,'' = 不停。登记信息异常按到期处理(宁可错停)。"""
    if not acc.metadata_ok(user):
        return "account_invalid"
    if acc.days_left(user, on) < 0:
        return "account_expired"
    if acc.props(user).get(acc.P_REJECTED):
        return "sponsor_rejected"
    sponsor = by_id.get(acc.props(user).get(acc.P_SPONSOR))
    if sponsor is None or sponsor.get("isForbidden"):
        return "sponsor_left"
    return ""


async def sweep(client) -> None:
    """受管账号扫描:停掉过期 / 信息异常 / 负责人拒绝 / 负责人已停用的账号,给快到期的发续期提醒。
    读不到 Casdoor 用户列表就整步跳过(下次再试)。飞书账号和未登记账号不在这里处理。"""
    async with _SWEEP_LOCK:
        users = await ca.list_users(client)
        if users is None:
            logger.warning("受管账号扫描跳过:读不到 Casdoor 用户列表(下次再试)")
            return
        by_id = {u.get("id"): u for u in users if u.get("id")}
        on = acc.today()
        for user in users:
            if not acc.is_managed(user) or user.get("isForbidden"):
                continue
            reason = _lapse_reason(user, on, by_id)
            if reason:
                await _disable(client, user, reason)
            else:
                await _maybe_remind(client, user, on)


async def _disable(client, user: dict, reason: str) -> bool:
    from even_auth_gov import offboard
    name = acc.display_name(user)
    if not await offboard.apply(user["id"], name, reason, client):
        return False   # offboard 已标 disable_failed 并告警;下次扫描还会再试
    log_event(_DISABLE_EVENTS[reason], user, "system")
    p = acc.props(user)
    if reason == "account_expired":
        await _notify(p.get(acc.P_SPONSOR, ""),
                      f"{name} 的账号已于 {p.get(acc.P_EXPIRES)} 到期,已停用。"
                      "如需继续使用请联系管理员续期(续期后对方要重新申请各应用)。")
    elif reason in ("account_invalid", "sponsor_left"):
        why = "登记信息异常" if reason == "account_invalid" else "的负责人已停用"
        await _notify(settings.approver_feishu_id(),
                      f"受管账号 {name}({user['id']}){why},账号已停用。需要保留的请修正登记信息或换负责人后续期。")
    return True


async def _maybe_remind(client, user: dict, on) -> None:
    stage = acc.reminder_due(user, on, settings.account_remind_days())
    if stage is None:
        return
    from even_auth_gov.feishu import build_account_renew_card, send_card
    p = acc.props(user)
    card = build_account_renew_card(user["id"], acc.display_name(user), acc.KINDS.get(p.get(acc.P_KIND), ""),
                                    p.get(acc.P_EXPIRES, ""), acc.days_left(user, on), settings.account_renew_days())
    try:
        ok = bool(await send_card(p.get(acc.P_SPONSOR, ""), card))
    except Exception as e:
        logger.warning("续期提醒发送异常 %s: %s", user["id"], e)
        ok = False
    if not ok:
        logger.warning("续期提醒发送失败 %s(下次扫描重试)", user["id"])
        return
    if await ca.set_properties(client, user["id"], {acc.P_REMINDED: f"{p.get(acc.P_EXPIRES)}:{stage}"}):
        log_event("reminded", user, "system", detail=str(stage))


async def renew(client, user_or_ref, actor: str, expires: str = "") -> dict:
    """续期。不给 expires 时按 ACCOUNT_RENEW_DAYS 延长(不超过上限)。
    账号停用中而且负责人确认过的,同时重新启用,并放开它的审批记录允许重新申请。"""
    user = user_or_ref if isinstance(user_or_ref, dict) else await _resolve_user(client, user_or_ref)
    if not acc.is_managed(user):
        raise AccountError("不是受管账号")
    p, on = acc.props(user), acc.today()
    if p.get(acc.P_KIND) not in acc.KINDS or not acc.sponsor_ok(user):
        raise AccountError("登记信息不完整(类型或负责人),先修正再续期")
    if p.get(acc.P_REJECTED):
        raise AccountError("负责人拒绝过,不能续期;请先换负责人(transfer)")
    if expires:
        exp = acc.parse_date(expires)
        err = acc.expiry_error(exp, on, settings.account_max_days())
        if err:
            raise AccountError(err)
    else:
        exp = acc.renewed_expiry(acc.expires_of(user), on, settings.account_renew_days(), settings.account_max_days())
    will_enable = bool(user.get("isForbidden") and p.get(acc.P_CONFIRMED))
    if will_enable:
        sponsor = await ca.get_user(client, p[acc.P_SPONSOR])
        if not sponsor or sponsor.get("isForbidden"):
            raise AccountError("负责人已停用或查不到,先换负责人(transfer)再续期")
    updates = {acc.P_EXPIRES: exp.isoformat(), acc.P_REMINDED: None}
    if not await ca.set_properties(client, user["id"], updates):
        raise AccountError("写入到期日失败(Casdoor)")
    user = {**user, "properties": _merged(user, updates)}
    reopened = 0
    if will_enable:
        if not await ca.enable_user(client, user["id"]):
            raise AccountError("到期日已更新,但启用账号失败(Casdoor),请重试")
        from even_auth_gov import approval_store
        reopened = approval_store.reopen_after_renewal(user["id"])
    log_event("renewed", user, actor, detail="re-enabled" if will_enable else "")
    return {"expires": exp.isoformat(), "enabled": will_enable, "reopened": reopened}
```

`handle_action` 里 `if action == "acct_reject": ...` 之后、`return f"未知操作..."` 之前插入:

```python
    if action == "acct_renew":
        try:
            r = await renew(client, user, actor=operator_id)
        except AccountError as e:
            return f"续期失败:{e}"
        msg = f"已为 {acc.display_name(user)} 续期到 {r['expires']}"
        return msg + (",账号已重新启用(对方需要重新申请各应用)" if r["enabled"] else "")
    if action == "acct_no_renew":
        p = acc.props(user)
        last = f"{p.get(acc.P_EXPIRES)}:{min(settings.account_remind_days())}"
        if not await ca.set_properties(client, user["id"], {acc.P_REMINDED: last}):
            return "操作失败:写入 Casdoor 失败,请稍后再点"
        log_event("no_renew", user, operator_id)
        return f"好的,不再提醒;{acc.display_name(user)} 的账号将于 {p.get(acc.P_EXPIRES)} 到期后停用"
```

`even_auth_gov/scheduler.py:_run_pending_job` 换成:

```python
async def _run_pending_job() -> None:
    """Job body: 待批补发/催办/超时升级,然后受管账号扫描(到期停用、续期提醒、负责人已停用)。
    账号扫描失败只记日志,不影响待批扫描。"""
    import httpx
    from even_auth_gov import account_lifecycle, reconcile, settings

    await reconcile.sweep_pending()
    try:
        async with httpx.AsyncClient(base_url=settings.casdoor_endpoint()) as client:
            await account_lifecycle.sweep(client)
    except Exception as e:
        logger.warning("受管账号扫描失败: %s", e)
```

模块 docstring 的"待批扫描"一行补上"+ 受管账号到期/续期提醒(里程碑 4a)"。

`even_auth_gov/reconcile.py:run`:`await sweep_pending()` 之后插入:

```python
    # 0b) 受管账号扫描(里程碑 4a:到期 / 负责人已停用;每小时任务也跑,内部有锁)。
    from even_auth_gov import account_lifecycle
    try:
        await account_lifecycle.sweep(client)
    except Exception as e:
        logger.warning("Reconcile: 受管账号扫描失败: %s", e)
```

`even_auth_gov/accounts.py`:加命令与参数:

```python
async def _cmd_renew(client, a) -> str:
    r = await _lc().renew(client, a.user, actor="admin", expires=a.expires or "")
    tail = f",账号已重新启用;{r['reopened']} 条审批记录已放开,对方可以重新申请" if r["enabled"] else ""
    return f"已续期到 {r['expires']}{tail}"
```

`_COMMANDS` 加 `"renew": _cmd_renew`;`_parser()` 里加:

```python
    n = sub.add_parser("renew", help="续期(停用中且负责人确认过的会重新启用)")
    n.add_argument("--user", required=True, help="邮箱或账号 id")
    n.add_argument("--expires", default="", help="指定到期日 YYYY-MM-DD;不给则按 ACCOUNT_RENEW_DAYS 延长")
```

- [ ] **Step 4: 运行确认通过** —— 跑 Step 2 的命令,再全量 `$PYTEST`。

- [ ] **Step 5: 提交**

```bash
git add even_auth_gov/ tests/
git commit -m "M4a: expiry sweep, renewal reminders and renewal; renewed accounts can re-apply"
```

---

### Task 7: 负责人离职连带停用 + 对账纳入负责人

**Files:**
- Modify: `even_auth_gov/account_lifecycle.py`、`even_auth_gov/offboard.py:apply`、`even_auth_gov/reconcile.py:run`
- Test: `tests/test_account_lifecycle.py`、`tests/test_reconcile.py`

**Interfaces:**
- Produces:`cascade_sponsor(sponsor_id, sponsor_name, client) -> list[str]`(停用成功的账号名);`active_sponsors(client) -> list[str]`

- [ ] **Step 1: 写失败的测试**

`tests/test_account_lifecycle.py` 追加:

```python
from even_auth_gov import offboard


def test_offboarding_a_sponsor_disables_their_accounts(env):
    active(env, uid="x_a")
    active(env, uid="x_b")
    active(env, uid="x_c", sponsor="ou_other")
    ok = run(env["fc"], lambda c: offboard.apply("ou_boss", "李四", "offboard_event", c))
    users = env["fc"].users
    assert ok is True and users["ou_boss"]["isForbidden"]
    assert users["x_a"]["isForbidden"] and users["x_b"]["isForbidden"] and not users["x_c"]["isForbidden"]
    summary = [t for to, t in env["sent"]["texts"] if to == "ou_root"]
    assert len(summary) == 1 and "李四" in summary[0] and "受管x_a" in summary[0] and "受管x_b" in summary[0]
    assert {e["user_id"] for e in events(env) if e["event"] == "sponsor_left"} == {"x_a", "x_b"}


def test_cascade_failure_does_not_change_offboard_result(env):
    active(env)
    env["fc"].fail.add("/api/get-users")
    assert run(env["fc"], lambda c: offboard.apply("ou_boss", "李四", "offboard_event", c)) is True
    assert env["fc"].users["x_a"]["isForbidden"] is False   # 每小时扫描兜底


def test_offboarding_a_managed_account_does_not_cascade(env):
    active(env)
    run(env["fc"], lambda c: offboard.apply("x_a", "受管x_a", "account_expired", c))
    assert not [t for to, t in env["sent"]["texts"] if to == "ou_root"]


def test_active_sponsors(env):
    active(env, uid="x_a")
    active(env, uid="x_b", sponsor="ou_other")
    env["fc"].users["x_b"]["isForbidden"] = True                # 停用账号的负责人不算
    env["fc"].users["ou_gone"] = {**env["fc"].users["ou_other"], "id": "ou_gone", "isForbidden": True}
    active(env, uid="x_c", sponsor="ou_gone")                   # 已停用的负责人不算(扫描会处理)
    assert run(env["fc"], lc.active_sponsors) == ["ou_boss"]
```

`tests/test_reconcile.py` 追加:

```python
def test_reconcile_checks_sponsors_who_have_no_app_access(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    from even_auth_gov import account_lifecycle
    async def fake_sponsors(client): return ["ou_sponsor"]
    async def fake_members(client): return []
    async def no_sweep(client): pass
    async def fake_status(open_id): return _status(is_resigned=True)
    disabled = []
    async def fake_apply(open_id, name, reason, client): disabled.append(open_id); return True
    monkeypatch.setattr(account_lifecycle, "active_sponsors", fake_sponsors)
    monkeypatch.setattr(account_lifecycle, "sweep", no_sweep)
    monkeypatch.setattr("even_auth_gov.casdoor_admin.list_approved_feishu_members", fake_members)
    monkeypatch.setattr(reconcile, "fetch_feishu_status", fake_status)
    monkeypatch.setattr(reconcile.offboard, "apply", fake_apply)
    asyncio.run(reconcile.run(client=None))
    assert disabled == ["ou_sponsor"]
```

- [ ] **Step 2: 运行确认失败** —— `$PYTEST tests/test_account_lifecycle.py tests/test_reconcile.py`。

- [ ] **Step 3: 实现**

`even_auth_gov/account_lifecycle.py` 末尾追加:

```python
# ── 负责人离职 ──

async def cascade_sponsor(sponsor_id: str, sponsor_name: str, client) -> list[str]:
    """负责人被停用后,停用他名下所有启用中的受管账号(原因 sponsor_left),并给全局审批人发一条汇总。
    返回停用成功的账号名。读不到用户列表就放弃 —— 每小时扫描会按"负责人已停用"兜底。"""
    users = await ca.list_users(client)
    if users is None:
        logger.warning("连带停用跳过:读不到 Casdoor 用户列表(负责人 %s),每小时扫描会兜底", sponsor_id)
        return []
    targets = [u for u in users if acc.is_managed(u) and not u.get("isForbidden")
               and acc.props(u).get(acc.P_SPONSOR) == sponsor_id]
    if not targets:
        return []
    from even_auth_gov import offboard
    done, failed = [], []
    for user in targets:
        name = acc.display_name(user)
        if await offboard.apply(user["id"], name, "sponsor_left", client):
            log_event("sponsor_left", user, "system", detail=sponsor_id)
            done.append(name)
        else:
            failed.append(name)
    lines = [f"负责人 {sponsor_name or sponsor_id} 已停用,名下 {len(targets)} 个受管账号已处理:"]
    if done:
        lines.append("已停用:" + "、".join(done))
    if failed:
        lines.append("停用失败(已另行告警):" + "、".join(failed))
    lines.append("需要保留的账号:python -m even_auth_gov.accounts transfer 换负责人,确认后再 renew 续期。")
    await _notify(settings.approver_feishu_id(), "\n".join(lines))
    return done


async def active_sponsors(client) -> list[str]:
    """启用中的受管账号的负责人(只取启用中的飞书账号)。每日对账把他们纳入飞书在职检查 ——
    负责人自己可能不在任何准入组里,原来的对账查不到他。"""
    users = await ca.list_users(client)
    if not users:
        return []
    by_id = {u.get("id"): u for u in users}
    out = set()
    for user in users:
        if acc.is_managed(user) and not user.get("isForbidden"):
            sid = acc.props(user).get(acc.P_SPONSOR, "")
            sponsor = by_id.get(sid)
            if sid.startswith("ou_") and sponsor and not sponsor.get("isForbidden"):
                out.add(sid)
    return sorted(out)
```

`even_auth_gov/offboard.py:apply`:在 `logger.info("Offboard 已禁用 Casdoor: ...")` 之后、`return True` 之前插入:

```python
            if open_id.startswith("ou_"):
                # 里程碑 4a:负责人停用 → 名下受管账号一起停。受管账号不能当负责人,所以不会循环触发;
                # 失败只记日志,每小时账号扫描会按"负责人已停用"兜底。
                try:
                    from even_auth_gov import account_lifecycle
                    await account_lifecycle.cascade_sponsor(open_id, name, client)
                except Exception as e:
                    logger.warning("连带停用 %s 名下账号失败: %s(每小时扫描兜底)", open_id, e)
```

模块 docstring 末尾补一句:"负责人(飞书账号)停用成功后,连带停用其名下受管账号(里程碑 4a,account_lifecycle.cascade_sponsor)。"

`even_auth_gov/reconcile.py:run`:把

```python
    from even_auth_gov import casdoor_admin
    check_ids = set(_distinct_open_ids_by_status("approved")) | set(await casdoor_admin.list_approved_feishu_members(client))
```

换成

```python
    # 里程碑 4a:再加上受管账号的负责人 —— 他们可能自己不用任何应用,但离职了名下账号要跟着停。
    from even_auth_gov import casdoor_admin
    check_ids = (set(_distinct_open_ids_by_status("approved"))
                 | set(await casdoor_admin.list_approved_feishu_members(client))
                 | set(await account_lifecycle.active_sponsors(client)))
```

(`account_lifecycle` 已在 Task 6 的 0b 步骤里导入;注释 "2) 常规对账:检查集 = ..." 同步写上"∪ 受管账号的负责人"。)

- [ ] **Step 4: 运行确认通过** —— 跑 Step 2 的命令,再全量 `$PYTEST`(现有 offboard/reconcile 测试传 `client=None`:`list_users(None)` 返回 None,连带停用和负责人收集都安静地跳过)。

- [ ] **Step 5: 提交**

```bash
git add even_auth_gov/account_lifecycle.py even_auth_gov/offboard.py even_auth_gov/reconcile.py tests/
git commit -m "M4a: disable a sponsor's managed accounts when the sponsor is offboarded; reconcile checks sponsors"
```

---

### Task 8: 换负责人、设密码、列表

**Files:**
- Modify: `even_auth_gov/account_lifecycle.py`、`even_auth_gov/accounts.py`
- Test: `tests/test_account_lifecycle.py`、`tests/test_accounts.py`

**Interfaces:**
- Produces:`transfer(client, ref, sponsor_email, actor="admin") -> dict`(`user_id, sponsor, confirm_sent`);`set_password(client, ref, actor="admin") -> dict`(`user_id, email, password, must_change`);`list_accounts(client) -> list[dict]`;`LIST_COLUMNS`;`handle_action` 处理待确认的新负责人;命令行 `transfer`、`set-password`、`list`

- [ ] **Step 1: 写失败的测试**

`tests/test_account_lifecycle.py` 追加:

```python
def transfer(env, ref="x_a", to="other@e.com"):
    return run(env["fc"], lambda c: lc.transfer(c, ref, to))


def test_transfer_needs_new_sponsor_confirmation(env):
    active(env)
    r = transfer(env)
    assert r["confirm_sent"] is True and env["fc"].props("x_a")["evauth_sponsor_pending"] == "ou_other"
    to, card = env["sent"]["cards"][-1]
    assert to == "ou_other" and "负责人改为你" in card["elements"][0]["content"]
    assert env["fc"].props("x_a")["evauth_sponsor"] == "ou_boss"           # 确认前旧负责人有效
    assert "只有该账号的负责人" in click(env, "acct_confirm", "ou_root", "x_a")
    assert "已确认负责" in click(env, "acct_confirm", "ou_other", "x_a")
    p = env["fc"].props("x_a")
    assert p["evauth_sponsor"] == "ou_other" and "evauth_sponsor_pending" not in p
    assert [e["event"] for e in events(env)][-2:] == ["transfer_requested", "transferred"]


def test_new_sponsor_can_decline_transfer(env):
    active(env)
    transfer(env)
    assert "负责人保持不变" in click(env, "acct_reject", "ou_other", "x_a")
    p = env["fc"].props("x_a")
    assert p["evauth_sponsor"] == "ou_boss" and "evauth_sponsor_pending" not in p


def test_transfer_then_renew_restores_an_orphaned_account(env):
    env["fc"].users["ou_boss"]["isForbidden"] = True
    active(env)
    sweep(env)                                                               # 负责人已停用 → 停
    assert env["fc"].users["x_a"]["isForbidden"]
    with pytest.raises(lc.AccountError, match="已停用"):
        transfer(env, to="boss@e.com")
    transfer(env)
    assert "需要管理员续期" in click(env, "acct_confirm", "ou_other", "x_a")
    run(env["fc"], lambda c: lc.renew(c, "x_a", actor="admin"))
    assert env["fc"].users["x_a"]["isForbidden"] is False


def test_transfer_refusals(env):
    active(env)
    with pytest.raises(lc.AccountError, match="一样"):
        transfer(env, to="boss@e.com")
    with pytest.raises(lc.AccountError, match="不是受管账号"):
        transfer(env, ref="ou_other")


def test_set_password_for_feishu_backup_login_and_service(env):
    r = run(env["fc"], lambda c: lc.set_password(c, "boss@e.com"))
    u = env["fc"].users["ou_boss"]
    assert r["must_change"] is True and u["password"] == r["password"] and u["needUpdatePassword"] is True
    active(env, uid="x_svc", evauth_kind="service")
    r = run(env["fc"], lambda c: lc.set_password(c, "x_svc"))
    assert r["must_change"] is False and env["fc"].users["x_svc"]["needUpdatePassword"] is False
    log = (env["dir"] / "account_events.jsonl").read_text(encoding="utf-8")
    assert "password_set" in log and r["password"] not in log


def test_set_password_refusals(env):
    env["fc"].add(id="uuid-s", email="s@x.com")
    with pytest.raises(lc.AccountError, match="register"):
        run(env["fc"], lambda c: lc.set_password(c, "uuid-s"))
    active(env)
    env["fc"].users["x_a"]["isForbidden"] = True
    with pytest.raises(lc.AccountError, match="已停用"):
        run(env["fc"], lambda c: lc.set_password(c, "x_a"))


def test_list_accounts(env):
    active(env, uid="x_a", expires=day(5))
    r = create(env)
    rows = run(env["fc"], lc.list_accounts)
    assert [row["id"] for row in rows] == ["x_a", r["user_id"]]                # 按到期日排序
    assert rows[0]["sponsor_name"] == "李四" and rows[0]["days_left"] == 5 and rows[0]["state"] == "启用/有效"
    assert rows[1]["state"] == "停用/待确认" and rows[1]["kind"] == "外部合作方"
```

`tests/test_accounts.py` 追加:

```python
def test_cli_list_outputs_csv(monkeypatch, tmp_path, capsys):
    import csv, io
    fc = _cli_env(monkeypatch, tmp_path)
    fc.add(id="x_1", email="z@p.com", displayName="张三", properties={"evauth_kind": "staff",
           "evauth_sponsor": "ou_boss", "evauth_expires": (acc.today() + timedelta(days=9)).isoformat(),
           "evauth_confirmed_at": "t"})
    assert acc.main(["list"]) == 0
    rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert rows[0]["id"] == "x_1" and rows[0]["kind"] == "内部员工(无飞书)" and rows[0]["days_left"] == "9"


def test_cli_set_password_prints_it_once(monkeypatch, tmp_path, capsys):
    fc = _cli_env(monkeypatch, tmp_path)
    assert acc.main(["set-password", "--user", "boss@e.com"]) == 0
    assert fc.users["ou_boss"]["password"] in capsys.readouterr().out
```

- [ ] **Step 2: 运行确认失败** —— `$PYTEST tests/test_account_lifecycle.py tests/test_accounts.py`。

- [ ] **Step 3: 实现**

`even_auth_gov/account_lifecycle.py`:`handle_action` 里把

```python
    if operator_id != acc.props(user).get(acc.P_SPONSOR):
        return "只有该账号的负责人可以操作"
```

换成

```python
    pending = acc.props(user).get(acc.P_SPONSOR_PENDING, "")
    if pending and operator_id == pending and action in ("acct_confirm", "acct_reject"):
        return await _decide_transfer(client, user, operator_id, accept=action == "acct_confirm")
    if operator_id != acc.props(user).get(acc.P_SPONSOR):
        return "只有该账号的负责人可以操作"
```

文件末尾追加:

```python
# ── 换负责人、设密码、列表 ──

async def transfer(client, ref: str, sponsor_email: str, actor: str = "admin") -> dict:
    """换负责人:记下待确认的新负责人并给他发确认卡片;确认前旧负责人一直有效。"""
    user = await _resolve_user(client, ref)
    if not acc.is_managed(user):
        raise AccountError("不是受管账号")
    new = await _resolve_sponsor(client, sponsor_email)
    if new["id"] == acc.props(user).get(acc.P_SPONSOR):
        raise AccountError("新负责人和现在的一样")
    updates = {acc.P_SPONSOR_PENDING: new["id"]}
    if not await ca.set_properties(client, user["id"], updates):
        raise AccountError("写入失败(Casdoor)")
    user = {**user, "properties": _merged(user, updates)}
    log_event("transfer_requested", user, actor, detail=new["id"])
    sent = await send_confirm(user, mode="transfer", to=new["id"])
    return {"user_id": user["id"], "sponsor": acc.display_name(new), "confirm_sent": sent}


async def _decide_transfer(client, user: dict, operator_id: str, accept: bool) -> str:
    name = acc.display_name(user)
    if accept:
        updates = {acc.P_SPONSOR: operator_id, acc.P_SPONSOR_PENDING: None,
                   acc.P_CONFIRMED: acc.now_iso(), acc.P_REJECTED: None}
    else:
        updates = {acc.P_SPONSOR_PENDING: None}
    if not await ca.set_properties(client, user["id"], updates):
        return "操作失败:写入 Casdoor 失败,请稍后再点"
    log_event("transferred" if accept else "transfer_rejected",
              {**user, "properties": _merged(user, updates)}, operator_id)
    if not accept:
        return f"已拒绝负责 {name} 的账号,负责人保持不变"
    msg = f"已确认负责 {name} 的账号"
    return msg + ("。账号目前是停用状态,需要管理员续期后才会启用" if user.get("isForbidden") else "")


async def set_password(client, ref: str, actor: str = "admin") -> dict:
    """设随机密码:飞书员工的备用登录、或受管账号重置密码。服务账号不强制改密(否则 password grant 永远失败)。"""
    user = await _resolve_user(client, ref)
    if user.get("isForbidden"):
        raise AccountError("账号已停用,不能设密码")
    if not acc.is_feishu(user) and not acc.is_managed(user):
        raise AccountError("未登记的非飞书账号,先用 register 纳管")
    must_change = acc.props(user).get(acc.P_KIND) != "service"
    password = new_password()
    if not await ca.set_password(client, user["id"], password, need_update=must_change):
        raise AccountError("设密码失败(Casdoor)")
    log_event("password_set", user, actor)
    return {"user_id": user["id"], "email": user.get("email", ""), "password": password, "must_change": must_change}


LIST_COLUMNS = ["name", "email", "id", "kind", "sponsor_name", "sponsor", "expires", "days_left", "state"]
_STATE = {"": "有效", "unconfirmed": "待确认", "rejected": "已拒绝", "expired": "已过期", "invalid_metadata": "信息不全"}


async def list_accounts(client) -> list[dict]:
    users = await ca.list_users(client)
    if users is None:
        raise AccountError("读不到 Casdoor 用户列表")
    by_id = {u.get("id"): u for u in users}
    on, rows = acc.today(), []
    for user in users:
        if not acc.is_managed(user):
            continue
        p = acc.props(user)
        sponsor = by_id.get(p.get(acc.P_SPONSOR))
        left = acc.days_left(user, on)
        state = ("停用" if user.get("isForbidden") else "启用") + "/" + _STATE[acc.eligibility(user, on)]
        if p.get(acc.P_SPONSOR_PENDING):
            state += "(换负责人待确认)"
        rows.append({"name": acc.display_name(user), "email": user.get("email", ""), "id": user.get("id", ""),
                     "kind": acc.KINDS.get(p.get(acc.P_KIND), p.get(acc.P_KIND, "")),
                     "sponsor_name": acc.display_name(sponsor) if sponsor else "", "sponsor": p.get(acc.P_SPONSOR, ""),
                     "expires": p.get(acc.P_EXPIRES, ""), "days_left": "" if left is None else left, "state": state})
    rows.sort(key=lambda r: (r["expires"], r["name"]))
    return rows
```

`even_auth_gov/accounts.py`:加命令

```python
async def _cmd_transfer(client, a) -> str:
    r = await _lc().transfer(client, a.user, a.sponsor)
    return (f"已发确认卡片给新负责人 {r['sponsor']},确认后生效(之前仍由原负责人负责)" if r["confirm_sent"]
            else f"⚠️ 确认卡片发送失败,稍后运行 resend-confirm --user {a.user}")


async def _cmd_set_password(client, a) -> str:
    r = await _lc().set_password(client, a.user)
    tail = "下次网页登录必须先改密码" if r["must_change"] else "服务账号,不强制改密码"
    return f"新密码(只显示这一次,请线下交给对方):{r['password']}\n登录邮箱:{r['email']};{tail}"


async def _cmd_list(client, a) -> str:
    import csv
    import io
    lc = _lc()
    rows = await lc.list_accounts(client)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=lc.LIST_COLUMNS)
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue().rstrip("\n")
```

`_COMMANDS` 加 `"transfer": _cmd_transfer, "set-password": _cmd_set_password, "list": _cmd_list`;`_parser()` 加:

```python
    t = sub.add_parser("transfer", help="换负责人(新负责人确认后生效)")
    t.add_argument("--user", required=True, help="邮箱或账号 id")
    t.add_argument("--sponsor", required=True, help="新负责人的飞书邮箱")
    p = sub.add_parser("set-password", help="设随机密码:飞书员工备用登录 / 受管账号重置密码")
    p.add_argument("--user", required=True, help="邮箱或账号 id")
    sub.add_parser("list", help="列出受管账号(CSV)")
```

- [ ] **Step 4: 运行确认通过** —— 跑 Step 2 的命令,再全量 `$PYTEST`。

- [ ] **Step 5: 提交**

```bash
git add even_auth_gov/account_lifecycle.py even_auth_gov/accounts.py tests/
git commit -m "M4a: sponsor transfer, password set/reset for backup login, managed account listing"
```

---

### Task 9: 核对报表、审计导出、备份

**Files:**
- Modify: `even_auth_gov/access_report.py`、`even_auth_gov/audit_export.py`、`deploy/casdoor/backup.sh`
- Test: `tests/test_access_report.py`、`tests/test_audit_export.py`

**Interfaces:**
- Consumes:Task 1 规则
- Produces:`access_report.build(snap, today=None)`;新列 `kind / sponsor / expires`;新 KINDS `unregistered_with_access / lapsed_but_enabled / sponsor_gone_but_enabled / unregistered_account / invalid_account_metadata / expiring_soon / awaiting_sponsor`;`audit_export.EVENT_COLUMNS`、`export_account_events(out) -> int`、命令行 `--accounts`

- [ ] **Step 1: 写失败的测试**

`tests/test_access_report.py` 追加:

```python
from datetime import date, timedelta

D = date(2026, 11, 18)


def _managed(oid, groups=(), forbidden=False, **props):
    p = {"evauth_kind": "partner", "evauth_sponsor": "ou_boss", "evauth_expires": "2026-12-31",
         "evauth_confirmed_at": "2026-11-01T00:00:00+00:00"}
    p.update(props)
    return {**_user(oid, oid, groups=groups, forbidden=forbidden),
            "properties": {k: v for k, v in p.items() if v is not None}}


def _kinds(users, roles=()):
    _, anomalies = ar.build(_snap(users, list(roles)), today=D)
    return {(a["kind"], a["open_id"]): a["severity"] for a in anomalies
            if a["kind"] not in ("access_without_approval",)}


def test_managed_and_unregistered_account_anomalies(env):
    boss = _user("ou_boss", "boss")
    users = [
        boss,
        _user("uuid-stranger", "s1", groups=["approved-app-a"]),            # 未登记 + 有准入
        _user("uuid-idle", "s2"),                                            # 未登记,没准入
        _user("uuid-off", "s3", forbidden=True),                             # 未登记但已停用:不报
        _managed("x_expired", evauth_expires="2026-11-17"),
        _managed("x_rejected", evauth_rejected_at="2026-11-02T00:00:00+00:00"),
        _managed("x_bad", evauth_kind="vendor"),
        _managed("x_bad_off", forbidden=True, evauth_kind="vendor"),
        _managed("x_orphan", evauth_sponsor="ou_ghost"),
        _managed("x_soon", evauth_expires="2026-11-25"),
        _managed("x_wait", forbidden=True, evauth_confirmed_at=None),
        _managed("x_fine", evauth_expires="2027-01-31"),
    ]
    assert _kinds(users) == {
        ("unregistered_with_access", "uuid-stranger"): "high",
        ("unregistered_account", "uuid-idle"): "medium",
        ("lapsed_but_enabled", "x_expired"): "high",
        ("lapsed_but_enabled", "x_rejected"): "high",
        ("lapsed_but_enabled", "x_bad"): "high",
        ("invalid_account_metadata", "x_bad_off"): "medium",
        ("sponsor_gone_but_enabled", "x_orphan"): "high",
        ("expiring_soon", "x_soon"): "info",
        ("awaiting_sponsor", "x_wait"): "info",
    }


def test_sponsor_disabled_counts_as_gone(env):
    users = [_user("ou_boss", "boss", forbidden=True), _managed("x_a")]
    assert _kinds(users) == {("sponsor_gone_but_enabled", "x_a"): "high"}


def test_view_rows_carry_kind_sponsor_and_expiry(env):
    users = [_user("ou_boss", "boss", display="李四"), _managed("x_a", groups=["approved-app-a"]),
             _user("ou_emp", "emp", groups=["approved-app-a"])]
    rows, _ = ar.build(_snap(users, []), today=D)
    by_id = {r["open_id"]: r for r in rows}
    assert (by_id["x_a"]["kind"], by_id["x_a"]["sponsor"], by_id["x_a"]["expires"]) == ("外部合作方", "李四", "2026-12-31")
    assert (by_id["ou_emp"]["kind"], by_id["ou_emp"]["sponsor"], by_id["ou_emp"]["expires"]) == ("飞书员工", "", "")
```

`tests/test_audit_export.py` 追加:

```python
def test_export_account_events(monkeypatch, tmp_path):
    import csv, io, json
    from even_auth_gov import audit_export
    path = tmp_path / "account_events.jsonl"
    monkeypatch.setenv("ACCOUNT_EVENTS_FILE", str(path))
    path.write_text(json.dumps({"ts": "t1", "event": "created", "user_id": "x_1", "name": "张三", "actor": "admin"},
                               ensure_ascii=False) + "\n" + '{"ts": "t2", "event": "conf' + "\n", encoding="utf-8")  # 第二行是半行
    out = io.StringIO()
    assert audit_export.export_account_events(out) == 1
    rows = list(csv.DictReader(io.StringIO(out.getvalue())))
    assert list(rows[0]) == audit_export.EVENT_COLUMNS and rows[0]["event"] == "created" and rows[0]["name"] == "张三"


def test_export_account_events_when_file_missing(monkeypatch, tmp_path):
    import io
    from even_auth_gov import audit_export
    monkeypatch.setenv("ACCOUNT_EVENTS_FILE", str(tmp_path / "none.jsonl"))
    assert audit_export.export_account_events(io.StringIO()) == 0
```

- [ ] **Step 2: 运行确认失败** —— `$PYTEST tests/test_access_report.py tests/test_audit_export.py`。

- [ ] **Step 3: 实现**

`even_auth_gov/access_report.py`:
1. import 行改为 `from even_auth_gov import accounts as acc, approval_store, casdoor_admin as ca, settings`。
2. `COLUMNS` 改为 `["name", "open_id", "email", "app", "account", "kind", "sponsor", "expires", "has_access", "roles", "approval_status", "approved_by", "approved_at", "source"]`。
3. `KINDS` 字典里按严重度插入:

```python
    "unregistered_with_access": ("high", "非飞书账号,没有登记负责人和到期日,却有准入或角色"),
    "lapsed_but_enabled": ("high", "受管账号已过期、登记信息异常或负责人拒绝,但还启用着"),
    "sponsor_gone_but_enabled": ("high", "受管账号的负责人已停用或已不在 Casdoor,但账号还启用着"),
    "unregistered_account": ("medium", "非飞书账号,没有登记负责人和到期日(启用中,暂无准入)"),
    "invalid_account_metadata": ("medium", "已停用的受管账号登记信息不完整或格式不对,续期前要先修正"),
    "expiring_soon": ("info", "受管账号即将到期"),
    "awaiting_sponsor": ("info", "受管账号等负责人确认"),
```

4. 在 `build` 之前加:

```python
def _account_anomalies(u: dict, name: str, oid: str, enabled: bool, has_any: bool, by_id: dict,
                       on, remind_max: int) -> list[dict]:
    """非飞书账号的核对(里程碑 4a)。飞书账号返回 []。"""
    if acc.is_feishu(u):
        return []
    if not acc.is_managed(u):
        if not enabled:
            return []
        return [_anomaly("unregistered_with_access" if has_any else "unregistered_account", name, oid)]
    p = acc.props(u)
    if not acc.metadata_ok(u):
        return [_anomaly("lapsed_but_enabled" if enabled else "invalid_account_metadata", name, oid, extra="登记信息异常")]
    out = []
    left = acc.days_left(u, on)
    if enabled and left < 0:
        out.append(_anomaly("lapsed_but_enabled", name, oid, extra=f"{p.get(acc.P_EXPIRES)} 到期"))
    elif enabled and p.get(acc.P_REJECTED):
        out.append(_anomaly("lapsed_but_enabled", name, oid, extra="负责人已拒绝"))
    sponsor = by_id.get(p.get(acc.P_SPONSOR))
    if enabled and (sponsor is None or sponsor.get("isForbidden")):
        out.append(_anomaly("sponsor_gone_but_enabled", name, oid, extra=p.get(acc.P_SPONSOR, "")))
    if not p.get(acc.P_CONFIRMED) and not p.get(acc.P_REJECTED):
        out.append(_anomaly("awaiting_sponsor", name, oid))
    elif enabled and 0 <= left <= remind_max:
        out.append(_anomaly("expiring_soon", name, oid, extra=f"{left} 天后到期"))
    return out


def _kind_label(u: dict) -> str:
    if acc.is_feishu(u):
        return "飞书员工"
    kind = acc.props(u).get(acc.P_KIND)
    return acc.KINDS.get(kind, kind) if kind else "未登记"
```

5. `build` 签名改为 `def build(snap: dict, today=None) -> tuple[list[dict], list[dict]]:`,开头加:

```python
    on = today or acc.today()
    remind_max = max(settings.account_remind_days())
    by_id = {u.get("id"): u for u in snap["users"]}
```

6. 在每个用户循环里 `offboarded = ...` 那段之后加:

```python
        anomalies += _account_anomalies(u, name, oid, enabled, bool(group_apps or role_map), by_id, on, remind_max)
        sponsor = by_id.get(acc.props(u).get(acc.P_SPONSOR)) if acc.is_managed(u) else None
        account_cols = {"kind": _kind_label(u),
                        "sponsor": acc.display_name(sponsor) if sponsor else acc.props(u).get(acc.P_SPONSOR, "") if acc.is_managed(u) else "",
                        "expires": acc.props(u).get(acc.P_EXPIRES, "") if acc.is_managed(u) else ""}
```

并在 `rows.append({...})` 的字典里加 `**account_cols,`。
7. 模块 docstring 的"两种输出"下补一句:"非飞书账号:未登记的、过期/负责人拒绝/负责人已停用仍启用的受管账号都会报出(里程碑 4a)。"

`even_auth_gov/audit_export.py`:import 加 `import json` 和 `from even_auth_gov import approval_store, settings`;追加:

```python
EVENT_COLUMNS = ["ts", "event", "user_id", "name", "email", "kind", "sponsor", "expires", "actor", "detail"]


def export_account_events(out: IO[str]) -> int:
    """受管账号变更历史(account_events.jsonl,里程碑 4a)导出为 CSV,返回行数。坏行(如进程被杀留下的半行)跳过。"""
    path = settings.account_events_file()
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                rows.append(rec)
    w = csv.DictWriter(out, fieldnames=EVENT_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in EVENT_COLUMNS})
    return len(rows)
```

`main` 里加参数 `ap.add_argument("--accounts", action="store_true", help="导出受管账号变更历史(account_events.jsonl)")`,导出时 `fn = (lambda f: export_account_events(f)) if args.accounts else (lambda f: export_csv(f, app=args.app, status=args.status))`,两个分支(写文件 / stdout)都改用 `fn`。docstring 用法里加一行 `python -m even_auth_gov.audit_export --accounts -o accounts.csv`。

`deploy/casdoor/backup.sh`:在审批库备份的 `if ...; then ... fi` 块里、`echo "backup -> $GOV_OUT ..."` 之后加:

```bash
  # 受管账号变更历史(里程碑 4a)——文件存在才备份。
  if docker compose -f docker-compose.yml --profile gov exec -T even-auth-gov test -f /data/account_events.jsonl; then
    EV_OUT="$DIR/account-events-$TS.jsonl.gz"
    docker compose -f docker-compose.yml --profile gov exec -T even-auth-gov cat /data/account_events.jsonl | gzip > "$EV_OUT"
    echo "backup -> $EV_OUT ($(du -h "$EV_OUT" | cut -f1))"
  fi
```

轮转处加一行 `ls -1t "$DIR"/account-events-*.jsonl.gz 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f`。

- [ ] **Step 4: 运行确认通过** —— `$PYTEST tests/test_access_report.py tests/test_audit_export.py`;`bash -n deploy/casdoor/backup.sh`;再全量 `$PYTEST`。

- [ ] **Step 5: 提交**

```bash
git add even_auth_gov/access_report.py even_auth_gov/audit_export.py deploy/casdoor/backup.sh tests/
git commit -m "M4a: access report flags unregistered and lapsed accounts; export and back up account events"
```

---

### Task 10: 文档 + 端到端实测

**Files:**
- Modify: `README.md`、`deploy/casdoor/README.md`、`deploy/casdoor/gov.env.example`、`docs/integration-guide.md`、`docs/design.md`、`docs/casdoor-findings.md`(§9 末尾加端到端结果)
- Create(不提交):`$SP/m4a/e2e_m4a.py`(`SP=/private/tmp/claude-501/-Users-even-Desktop-project-even-auth-gateway/dd52d484-beb5-4d0b-97b2-aac8faf7841a/scratchpad`)

- [ ] **Step 1: gov.env.example** —— "可选"段追加:

```
# ── 受管的非飞书账号(里程碑 4a,python -m even_auth_gov.accounts)──
# ACCOUNT_MAX_DAYS=180                  # 每次有效期上限(天)
# ACCOUNT_RENEW_DAYS=90                 # 一次续期延长的天数
# ACCOUNT_REMIND_DAYS=14,3              # 到期前第几天提醒负责人续期
# ACCOUNT_EVENTS_FILE=                  # 变更历史,默认和审批库同目录的 account_events.jsonl
```

- [ ] **Step 2: deploy/casdoor/README.md** —— 新增一节"受管的非飞书账号(里程碑 4a)",内容:
  - **必须**:Casdoor 应用打开 `disableSelfSignup`(`enableSignUp` 保持开着,否则飞书首次登录不能自动注册)。依据 findings §9 S5;不关的话陌生人能自助注册,虽然治理层闸门会拦住审批,但账号会留在 Casdoor 里。改完用 `curl -X POST <casdoor>/api/signup` 验证返回 "does not allow to sign up"。
  - 组织的密码策略建议至少 12 位并要求复杂度(初始密码 20 位随机,满足常见策略)。
  - 服务账号:对接的应用要在 Casdoor 里允许 `password` grant。
  - 运维命令(容器里 `docker compose ... exec even-auth-gov` 前缀):`create / register / resend-confirm / renew / transfer / set-password / list` 各一行示例。
  - 到期与提醒在每小时的待批扫描里跑;负责人离职会连带停用,汇总发给 `APPROVER_FEISHU_ID`。
  - `backup.sh` 会备份 `account-events-*.jsonl.gz`;恢复方法同审批库(`cat > /data/account_events.jsonl`)。
  - "定时任务"小节把每小时任务的说明补上"受管账号到期/续期提醒"。

- [ ] **Step 3: README.md**
  - 模块表加 `accounts.py`(规则、闸门、命令行)和 `account_lifecycle.py`(建号、确认、到期、续期、连带停用、换负责人、设密码)。
  - 关键设计加三条:非飞书账号必须有负责人 + 到期日(写在 Casdoor `properties`),没有的过不了审批;负责人离职连带停用;到期前提醒续期、过期按离职流程停用。
  - 环境变量表加 `ACCOUNT_*` 四项;运维命令加 `accounts` 子命令和 `audit_export --accounts`。
  - 待办:勾上"里程碑 4a · 非飞书账号与受管外部账号";未勾项新增"生产 Casdoor 应用打开 `disableSelfSignup`(并在真实飞书联调时确认飞书首登仍能自动注册)",并在"接真实飞书联调"一条里加上"负责人确认卡片、续期提醒卡片的显示和按钮回调"。
  - "PRD 后续里程碑"改为只剩 4b(账号自动供给/回收)。

- [ ] **Step 4: docs/integration-guide.md** —— 加一小节"非飞书账号":接入方不用改,外部账号和员工一样看 `groups` / `roles`;服务账号用 password grant 取 token(应用需允许该 grant),token 里的身份就是这个服务账号;想区分外部人员目前看不到,`evauth_kind` 进 token 不在本次范围。

- [ ] **Step 5: docs/design.md** —— 状态表加三行:受管账号(负责人 + 到期)✅、闸门 ✅、连带停用与续期 ✅(参照 M3 那几行的格式,放在空 `>` 行之前)。

- [ ] **Step 6: 端到端实测** —— 用已经起着的临时 Casdoor(`docker compose -p evauth-m4a`,端口 18000;凭据在 `$SP/m4a/creds.json`、`$SP/m4a/adminpw`)。参照 `$SP/cv/e2e_m3.py` 写 `$SP/m4a/e2e_m4a.py`:真实代码,只把 `feishu.send_card` / `send_text` 换成捕获;环境变量同 e2e_m3,另设 `APPROVAL_STORE_FILE` 到临时目录。逐项 `check(...)`:
  1. `account_lifecycle.create` 建号 → Casdoor 里账号停用、`needUpdatePassword=true`、properties 正确;用初始密码 password grant 失败(停用中)。
  2. `handle_action("acct_confirm", "ou_test_alice", ...)`(先把 alice 设为负责人:建号时 `sponsor_email="alice@e.com"`)→ 账号启用;网页登录返回 `RequiredUpdatePassword`;管理员 `set_password` 后对服务账号 password grant 能拿到 token。
  3. 用该账号的 id_token 调 `/request-access`(`fastapi.testclient.TestClient(build_app())`,`APP_CLIENT_IDS` 配 demo-app)→ 200 建待批;`decision.handle` 批准 → 加组成功。
  4. 自助注册一个陌生账号(先把 demo-app 的 `disableSelfSignup` 改回 false 并放宽注册项,做完再改回 true)→ 用它的 id_token 调 `/request-access` 返回 403;`access_report.build` 报 `unregistered_*`。
  5. 把账号的 `evauth_expires` 改成昨天 → `account_lifecycle.sweep` → 账号停用、准入组清空、审批记录 disabled。
  6. `renew` → 账号启用、审批记录变 `expired`、再调 `/request-access` 能重新建待批。
  7. `offboard.apply("ou_test_alice", ...)` → 名下账号连带停用,捕获到给 `ou_root` 的汇总。
  8. 非负责人点按钮被拒;`access_report` CLI(`--anomalies`)和函数结果一致。
  跑通后把结果追加到 `docs/casdoor-findings.md` §9 末尾("端到端(2026-11-18)"小段,列 8 项结论)。

- [ ] **Step 7: 全量测试 + 提交文档**

```bash
$PYTEST
git add README.md deploy/casdoor/README.md deploy/casdoor/gov.env.example docs/integration-guide.md docs/design.md docs/casdoor-findings.md
git commit -m "M4a docs: managed account operations, disableSelfSignup requirement, end-to-end results"
```

- [ ] **Step 8: 清理临时环境** —— `docker compose -p evauth-m4a -f deploy/casdoor/docker-compose.yml down -v`;删掉 `$SP/m4a/creds.json`、`$SP/m4a/adminpw`。镜像 `casbin/casdoor:v4.29.0` 和 `postgres:16` 保留与否问用户。
