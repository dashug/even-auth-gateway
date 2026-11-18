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
