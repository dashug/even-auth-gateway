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


# ── 闸门 ──

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


def test_gate_reasons():
    far = (acc.today() + timedelta(days=30)).isoformat()
    good = _ok(evauth_expires=far)
    assert _gate("x_1", good) == ""
    assert _gate("x_1", _u({})) == "unregistered"
    assert _gate("x_1", {**good, "isForbidden": True}) == "disabled"
    assert _gate("x_1", None) == "lookup_failed"                # 查不到
    assert _gate("x_1", None, status="error") == "lookup_failed"
