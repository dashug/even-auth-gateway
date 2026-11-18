"""网关配置 — 全部走环境变量,不依赖 cshub 的 config。"""
from __future__ import annotations

import os
from pathlib import Path


def approver_feishu_id() -> str:
    """审批人的飞书 open_id(收审批卡片、离职通知)。替代 cshub 的 escalation_owner.feishu_id。"""
    return os.getenv("APPROVER_FEISHU_ID", "")


def default_app() -> str:
    """未指定 app 时的默认应用名 —— 向后兼容现有的 cs-hub 试点(老 signup webhook 不带 app)。"""
    return os.getenv("DEFAULT_APP", "cs-hub")


def approver_for(app: str) -> str:
    """按应用定制审批人:APPROVER_<APP>(大写、'-'→'_')未配则回退全局 APPROVER_FEISHU_ID。"""
    env_name = f"APPROVER_{app.upper().replace('-', '_')}"
    return os.getenv(env_name, "") or approver_feishu_id()


def escalate_after_hours() -> float:
    """待批多少小时没人处理就升级(APPROVAL_ESCALATE_HOURS,默认 24;<=0 关闭)。从申请时间起算。"""
    try:
        return float(os.getenv("APPROVAL_ESCALATE_HOURS", "24"))
    except ValueError:
        return 24.0


def escalate_to() -> str:
    """超时升级给谁(APPROVAL_ESCALATE_TO,飞书 open_id);未配则用全局兜底审批人 APPROVER_FEISHU_ID。"""
    return os.getenv("APPROVAL_ESCALATE_TO", "") or approver_feishu_id()


def app_clients() -> dict[str, str]:
    """登记可以调 /request-access 的接入应用:APP_CLIENT_IDS="cs-hub:<client_id>,finance:<client_id>"
    → {client_id: app}。id_token 的 aud(= 该应用在 Casdoor 的 client_id)据此映射到应用名;
    没登记的应用一律拒绝(fail-closed)。"""
    out: dict[str, str] = {}
    for item in os.getenv("APP_CLIENT_IDS", "").split(","):
        app, sep, cid = item.strip().partition(":")
        if sep and app.strip() and cid.strip():
            out[cid.strip()] = app.strip()
    return out


def known_apps() -> set[str]:
    """网关知道的全部应用名(登记的接入应用 + 默认应用)。按 `<app>-` 前缀筛角色时用来排除
    名字更长的兄弟应用(如 app `cs` 不应认领 `cs-hub-operator`)。"""
    return set(app_clients().values()) | {default_app()}


def casdoor_issuer() -> str:
    """id_token 的 iss 期望值(Casdoor 对外地址,如 https://auth.example.com)。
    Casdoor 按签发请求的 Host 填 iss,网关走内网地址访问 Casdoor,所以不能拿 CASDOOR_ENDPOINT 比对;
    留空则不校验 iss(签名已由 Casdoor JWKS 保证)。"""
    return os.getenv("CASDOOR_ISSUER", "").strip()


def casdoor_endpoint() -> str:
    return os.getenv("CASDOOR_ENDPOINT", "http://127.0.0.1:8000")


def is_testing() -> bool:
    return bool(os.getenv("TESTING", ""))


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
