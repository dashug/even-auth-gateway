"""网关配置 — 全部走环境变量,不依赖 cshub 的 config。"""
from __future__ import annotations

import os


def approver_feishu_id() -> str:
    """审批人的飞书 open_id(收审批卡片、离职通知)。替代 cshub 的 escalation_owner.feishu_id。"""
    return os.getenv("APPROVER_FEISHU_ID", "")


def casdoor_endpoint() -> str:
    return os.getenv("CASDOOR_ENDPOINT", "http://127.0.0.1:8000")


def is_testing() -> bool:
    return bool(os.getenv("TESTING", ""))
