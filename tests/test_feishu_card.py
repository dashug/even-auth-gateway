import json
from even_auth_gov.feishu import build_sso_approval_card


def test_card_names_the_app_instead_of_hardcoded_admin_backend():
    """原文案写死 cs-hub 的"后台登录申请/进入管理后台",接第二个应用后对审批人是误导。"""
    card = build_sso_approval_card("ou_x", "张三", "z@e.com", "finance")
    text = json.dumps(card, ensure_ascii=False)
    assert "管理后台" not in text and "后台登录" not in text
    assert "finance" in card["header"]["title"]["content"]


def test_card_buttons_carry_applicant_and_app():
    card = build_sso_approval_card("ou_x", "张三", "", "finance")
    buttons = card["elements"][-1]["actions"]
    assert [b["value"]["action"] for b in buttons] == ["sso_approve", "sso_deny"]
    assert all(b["value"]["sso_open_id"] == "ou_x" and b["value"]["app"] == "finance" for b in buttons)


ROLES = [{"name": "finance-accountant", "display": "会计"}, {"name": "finance-cashier", "display": "出纳"}]


def test_card_with_roles_has_one_approve_button_per_role_plus_deny():
    """审批即赋角:一个角色一个"批准为「X」"按钮,点一下 = 批准 + 定角色。"""
    card = build_sso_approval_card("ou_x", "张三", "", "finance", roles=ROLES)
    buttons = card["elements"][-1]["actions"]
    assert [b["value"]["action"] for b in buttons] == ["sso_approve", "sso_approve", "sso_deny"]
    assert [b["value"].get("role") for b in buttons] == ["finance-accountant", "finance-cashier", None]
    assert [b["text"]["content"] for b in buttons[:2]] == ["批准为「会计」", "批准为「出纳」"]
    assert all(b["value"]["sso_open_id"] == "ou_x" and b["value"]["app"] == "finance" for b in buttons)
    assert "选择角色" in card["elements"][0]["content"]


def test_card_without_roles_has_no_role_in_values():
    card = build_sso_approval_card("ou_x", "张三", "", "finance", roles=[])
    buttons = card["elements"][-1]["actions"]
    assert [b["text"]["content"] for b in buttons] == ["批准 ✅", "拒绝"]
    assert all("role" not in b["value"] for b in buttons)


def test_card_note_is_shown_above_applicant_info():
    """超时升级等场景:卡片顶部带一行说明(为什么发给你)。"""
    card = build_sso_approval_card("ou_a", "申请人", "a@e.com", "app-a", note="已等待 26 小时未处理,升级给你")
    text = card["elements"][0]["content"]
    assert text.startswith("⏰ 已等待 26 小时未处理,升级给你")
    assert "申请人" in text
    plain = build_sso_approval_card("ou_a", "申请人", "a@e.com", "app-a")
    assert "⏰" not in plain["elements"][0]["content"]


# ── 里程碑 4a ──

import pytest
from even_auth_gov import feishu

def test_card_shows_identity_line_for_managed_accounts():
    card = feishu.build_sso_approval_card("x_1", "张三", "z@p.com", "app-a",
                                          identity="外部合作方 · 负责人 李四 · 有效期至 2027-02-01")
    assert "**账号类型:** 外部合作方 · 负责人 李四 · 有效期至 2027-02-01" in card["elements"][0]["content"]


def test_card_has_no_identity_line_by_default():
    card = feishu.build_sso_approval_card("ou_1", "张三", "z@p.com", "app-a")
    assert "账号类型" not in card["elements"][0]["content"]
