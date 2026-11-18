"""受管账号的生命周期操作(会读写 Casdoor、发飞书)—— 里程碑 4a。

建号 create、纳管 register、负责人确认/拒绝/续期按钮(handle_action)、重发确认卡片、
到期扫描与续期提醒(sweep)、续期(renew)、负责人离职连带停用(cascade_sponsor / active_sponsors)、换负责人(transfer)、设密码、列表。
规则在 accounts.py;卡片样式在 feishu.py;卡片按钮由 feishu_ws 转到 handle_action。
停用一律走 offboard.apply(重试 + 告警 + 清准入组和角色)。变更历史追加写到 account_events.jsonl,写失败只记日志。
"""
from __future__ import annotations

import asyncio
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
    pending = acc.props(user).get(acc.P_SPONSOR_PENDING, "")
    if pending and operator_id == pending and action in ("acct_confirm", "acct_reject"):
        return await _decide_transfer(client, user, operator_id, accept=action == "acct_confirm")
    if operator_id != acc.props(user).get(acc.P_SPONSOR):
        return "只有该账号的负责人可以操作"
    if action == "acct_confirm":
        return await _confirm(client, user, operator_id)
    if action == "acct_reject":
        return await _reject(client, user, operator_id)
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
