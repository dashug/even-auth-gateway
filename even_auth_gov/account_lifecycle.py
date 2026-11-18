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
