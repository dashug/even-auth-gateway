"""网关配置 — 全部走环境变量,不依赖 cshub 的 config。"""
from __future__ import annotations

import os


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


def casdoor_endpoint() -> str:
    return os.getenv("CASDOOR_ENDPOINT", "http://127.0.0.1:8000")


def is_testing() -> bool:
    return bool(os.getenv("TESTING", ""))
