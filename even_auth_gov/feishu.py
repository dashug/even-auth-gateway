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


def build_sso_approval_card(open_id: str, name: str, email: str = "", app: str = "",
                            roles: list[dict] | None = None, note: str = "", identity: str = "") -> dict:
    """Build the admin-approval card for a Feishu SSO login application.

    `app` identifies which downstream application this applicant is requesting
    access to (design-review #2/#16: the card must carry app identity so an
    approval for app A can never be mistaken for — or silently grant — access
    to app B). It rides along in every button `value` payload so
    feishu_ws._on_card / decision.handle can act on the right (open_id, app)
    pair, and is surfaced in the card text for the human approver.

    `roles`(F-5.5.3 审批即赋角):该应用在 Casdoor 建好的角色 [{"name", "display"}]。
    有角色时每个角色一个"批准为「X」"按钮(value 带 role),点一下 = 批准 + 定角色;
    没有则保持单个"批准"按钮。

    `note`:卡片顶部的一行说明(如超时升级"已等待 N 小时未处理"),没有则不显示。

    `identity`(里程碑 4a):非飞书受管账号的身份行(类型 · 负责人 · 有效期),飞书账号为空、不显示。
    """
    note_line = f"⏰ {note}\n\n" if note else ""
    email_line = f"\n**企业邮箱:** {email}" if email else ""
    identity_line = f"\n**账号类型:** {identity}" if identity else ""
    app_line = f"\n**申请接入应用:** {app}" if app else ""
    title = f"🔐 {app} 接入申请" if app else "🔐 应用接入申请"
    base = {"sso_open_id": open_id, "app": app}
    if roles:
        hint = "批准时请选择角色,批准后对方重新登录即可进入该应用。"
        approve = [
            {
                "tag": "button",
                "text": {"tag": "plain_text", "content": f"批准为「{r['display']}」"},
                "type": "primary",
                "value": {"action": "sso_approve", **base, "role": r["name"]},
            }
            for r in roles
        ]
    else:
        hint = "批准后对方重新登录即可进入该应用。"
        approve = [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": "批准 ✅"},
            "type": "primary",
            "value": {"action": "sso_approve", **base},
        }]
    deny = {
        "tag": "button",
        "text": {"tag": "plain_text", "content": "拒绝"},
        "type": "danger",
        "value": {"action": "sso_deny", **base},
    }
    action = {"tag": "action", "actions": approve + [deny]}
    if roles:
        action["layout"] = "flow"   # 角色多时按钮自动换行
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "orange",
        },
        "elements": [
            {"tag": "markdown", "content": f"{note_line}**申请人:** {name}{email_line}{identity_line}{app_line}\n\n{hint}"},
            {"tag": "hr"},
            action,
        ],
    }


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
