"""飞书(Lark)集成 — 网关自带,不依赖 cshub。
提供:lark client 单例、发文本/卡片、审批卡片构建。
从 even-cs-hub 的 channels/feishu.py 抽取相关部分,独立成网关的一等公民。
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

logger = logging.getLogger(__name__)

_client: Optional[lark.Client] = None


def _is_testing() -> bool:
    return bool(os.getenv("TESTING", ""))


def get_client() -> lark.Client:
    """Get or create the singleton Feishu client."""
    global _client
    if _client is None:
        app_id = os.getenv("FEISHU_APP_ID", "")
        app_secret = os.getenv("FEISHU_APP_SECRET", "")
        if not app_id or not app_secret:
            raise RuntimeError("FEISHU_APP_ID and FEISHU_APP_SECRET must be set")
        _client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .log_level(lark.LogLevel.WARNING)
            .build()
        )
    return _client


async def send_text(receive_id: str, text: str, receive_id_type: str = "open_id") -> bool:
    """Send a plain text message to a user or chat."""
    if _is_testing():
        logger.info("TESTING=1 — suppressed Feishu send_text to %s", receive_id)
        return True
    client = get_client()
    body = CreateMessageRequestBody.builder() \
        .receive_id(receive_id) \
        .msg_type("text") \
        .content(json.dumps({"text": text})) \
        .build()
    request = CreateMessageRequest.builder() \
        .receive_id_type(receive_id_type) \
        .request_body(body) \
        .build()
    response = await client.im.v1.message.acreate(request)
    if not response.success():
        logger.error("Feishu send_text failed: %s %s", response.code, response.msg)
        return False
    return True


async def send_card(receive_id: str, card: dict, receive_id_type: str = "open_id") -> bool:
    """Send an interactive card message."""
    if _is_testing():
        logger.info("TESTING=1 — suppressed Feishu send_card to %s", receive_id)
        return True
    client = get_client()
    body = CreateMessageRequestBody.builder() \
        .receive_id(receive_id) \
        .msg_type("interactive") \
        .content(json.dumps(card)) \
        .build()
    request = CreateMessageRequest.builder() \
        .receive_id_type(receive_id_type) \
        .request_body(body) \
        .build()
    response = await client.im.v1.message.acreate(request)
    if not response.success():
        logger.error("Feishu send_card failed: %s %s", response.code, response.msg)
        return False
    return True


def build_sso_approval_card(open_id: str, name: str, email: str = "") -> dict:
    """Build the admin-approval card for a Feishu SSO login application."""
    email_line = f"\n**企业邮箱:** {email}" if email else ""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "🔐 后台登录申请"},
            "template": "orange",
        },
        "elements": [
            {
                "tag": "markdown",
                "content": f"**申请人:** {name}{email_line}\n\n批准后对方重新扫码即可进入管理后台。",
            },
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "批准 ✅"},
                        "type": "primary",
                        "value": {"action": "sso_approve", "sso_open_id": open_id},
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "拒绝"},
                        "type": "danger",
                        "value": {"action": "sso_deny", "sso_open_id": open_id},
                    },
                ],
            },
        ],
    }
