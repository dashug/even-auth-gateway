"""权限视图 + 离职撤权核对(PRD F-5.8.3,里程碑 3)。

"谁此刻能访问什么"以 **Casdoor 实际状态**为准(准入组 approved-<app> / 角色 <app>-<role> /
账号是否停用),再合上审批库的审批留痕 —— 迁移、后台手工授权的人审批库里没有,也要看得见。

两种输出:
- 视图:每个"人 × 应用"一行(有准入组、有该应用角色、或审批库有记录的都算);
- 核对:需要处理的异常,按严重程度排序(见 KINDS)。非飞书账号:未登记的、已过期 / 负责人拒绝 /
  负责人已停用却仍启用的受管账号都会报出(里程碑 4a)。

用法(环境变量同治理服务;容器里用 docker compose exec 跑,读同一份审批库):
    python -m even_auth_gov.access_report -o access.csv              # 视图
    python -m even_auth_gov.access_report --app cs-hub -o cs-hub.csv
    python -m even_auth_gov.access_report --anomalies -o check.csv   # 核对
    python -m even_auth_gov.access_report --anomalies --check-feishu # 另逐个查飞书在职状态(慢,每个启用的飞书账号一次 API)

每日对账后会自动跑一遍核对,把高/中级异常推给全局审批人(scheduler,ACCESS_REPORT_ALERT=false 可关)。
"""
from __future__ import annotations
import argparse
import asyncio
import csv
import logging
import sys
from typing import IO

from even_auth_gov import accounts as acc, approval_store, casdoor_admin as ca, settings

logger = logging.getLogger(__name__)

COLUMNS = ["name", "open_id", "email", "app", "account", "kind", "sponsor", "expires", "has_access", "roles",
           "approval_status", "approved_by", "approved_at", "source"]
ANOMALY_COLUMNS = ["severity", "kind", "name", "open_id", "app", "detail"]

# kind → (严重程度, 说明)。high = 可能有离职的人仍能进;medium = 权限状态不干净、需要有人处理;
# info = 不一定是问题,核对时留意(不推送)。
KINDS = {
    "offboarded_but_enabled": ("high", "离职流程处理过(或禁用失败),但 Casdoor 账号是启用的"),
    "departed_but_enabled": ("high", "飞书显示已离职/冻结/已删除,但 Casdoor 账号是启用的"),
    "unregistered_with_access": ("high", "非飞书账号,没有登记负责人和到期日,却有准入或角色"),
    "lapsed_but_enabled": ("high", "受管账号已过期、登记信息异常或负责人拒绝,但还启用着"),
    "sponsor_gone_but_enabled": ("high", "受管账号的负责人已停用或已不在 Casdoor,但账号还启用着"),
    "disabled_with_access": ("medium", "账号已停用但仍挂着准入组/角色,一旦被重新启用会直接恢复访问"),
    "role_without_access": ("medium", "有该应用角色但没有准入组(残余角色)"),
    "access_without_role": ("medium", "该应用分了角色,此人有准入却没有角色"),
    "feishu_check_failed": ("medium", "查不到飞书在职状态(常见原因:通讯录权限范围没覆盖此人),对账会一直跳过他"),
    "unregistered_account": ("medium", "非飞书账号,没有登记负责人和到期日(启用中,暂无准入)"),
    "invalid_account_metadata": ("medium", "已停用的受管账号登记信息不完整或格式不对,续期前要先修正"),
    "access_without_approval": ("info", "有准入组但没有经网关审批的记录(迁移或后台手工授权)"),
    "approved_but_no_access": ("info", "审批库记为已批准,但 Casdoor 里没有准入组(被人手工移出?)"),
    "record_user_missing": ("info", "审批库有记录,但 Casdoor 里已没有这个用户"),
    "expiring_soon": ("info", "受管账号即将到期"),
    "awaiting_sponsor": ("info", "受管账号等负责人确认"),
}
_SEVERITY_ORDER = {"high": 0, "medium": 1, "info": 2}
ALERT_MAX_ITEMS = 10
_FEISHU_CONCURRENCY = 8


async def snapshot(client) -> dict:
    """读 Casdoor 用户与角色 + 审批库。Casdoor 读不到就抛错 —— 核对报表不能输出一张"看起来没问题"的空表。"""
    users = await ca.list_users(client)
    roles = await ca.list_roles(client)
    if users is None or roles is None:
        raise RuntimeError("读取 Casdoor 用户或角色失败,无法生成权限报表")
    return {"users": users, "roles": roles, "records": approval_store.all_records()}


def _app_of_role(name: str, apps: set[str]) -> str:
    """角色归属的应用:名字以 `<app>-` 开头的最长那个(`cs-hub-operator` 归 cs-hub 而不是 cs)。归不上返回 ""。"""
    owners = [a for a in apps if name.startswith(f"{a}-")]
    return max(owners, key=len) if owners else ""


def _anomaly(kind: str, name: str, open_id: str, app: str = "", extra: str = "") -> dict:
    severity, text = KINDS[kind]
    return {"severity": severity, "kind": kind, "name": name, "open_id": open_id, "app": app,
            "detail": f"{text}{f'({extra})' if extra else ''}"}


def _sorted(anomalies: list[dict]) -> list[dict]:
    return sorted(anomalies, key=lambda a: (_SEVERITY_ORDER[a["severity"]], a["kind"], a["open_id"], a["app"]))


def _account_anomalies(u: dict, name: str, oid: str, enabled: bool, has_any: bool, by_id: dict,
                       on, remind_max: int) -> list[dict]:
    """非飞书账号的核对(里程碑 4a)。飞书账号返回 []。"""
    if acc.is_feishu(u):
        return []
    if not acc.is_managed(u):
        if not enabled:
            return []
        return [_anomaly("unregistered_with_access" if has_any else "unregistered_account", name, oid)]
    p = acc.props(u)
    if not acc.metadata_ok(u):
        return [_anomaly("lapsed_but_enabled" if enabled else "invalid_account_metadata", name, oid, extra="登记信息异常")]
    out = []
    left = acc.days_left(u, on)
    if enabled and left < 0:
        out.append(_anomaly("lapsed_but_enabled", name, oid, extra=f"{p.get(acc.P_EXPIRES)} 到期"))
    elif enabled and p.get(acc.P_REJECTED):
        out.append(_anomaly("lapsed_but_enabled", name, oid, extra="负责人已拒绝"))
    sponsor = by_id.get(p.get(acc.P_SPONSOR))
    if enabled and (sponsor is None or sponsor.get("isForbidden")):
        out.append(_anomaly("sponsor_gone_but_enabled", name, oid, extra=p.get(acc.P_SPONSOR, "")))
    if not p.get(acc.P_CONFIRMED) and not p.get(acc.P_REJECTED):
        out.append(_anomaly("awaiting_sponsor", name, oid))
    elif enabled and 0 <= left <= remind_max:
        out.append(_anomaly("expiring_soon", name, oid, extra=f"{left} 天后到期"))
    return out


def _account_columns(u: dict, by_id: dict) -> dict:
    """视图里的类型 / 负责人 / 到期日三列(里程碑 4a)。"""
    if acc.is_feishu(u):
        return {"kind": "飞书员工", "sponsor": "", "expires": ""}
    if not acc.is_managed(u):
        return {"kind": "未登记", "sponsor": "", "expires": ""}
    p = acc.props(u)
    sponsor = by_id.get(p.get(acc.P_SPONSOR))
    return {"kind": acc.KINDS.get(p.get(acc.P_KIND), p.get(acc.P_KIND, "")),
            "sponsor": acc.display_name(sponsor) if sponsor else p.get(acc.P_SPONSOR, ""),
            "expires": p.get(acc.P_EXPIRES, "")}


def build(snap: dict, today=None) -> tuple[list[dict], list[dict]]:
    """(视图行, 异常)。纯函数,只看 snapshot;today 不传则取当天(按 SCHEDULER_TIMEZONE)。"""
    prefix = f"{ca._org()}/approved-"
    users, roles, records = snap["users"], snap["roles"], snap["records"]
    on = today or acc.today()
    remind_max = max(settings.account_remind_days())
    by_id = {u.get("id"): u for u in users}

    apps = set(settings.known_apps())
    for u in users:
        apps |= {g[len(prefix):] for g in (u.get("groups") or []) if (g or "").startswith(prefix)}
    apps |= {r.get("app") for r in records.values() if r.get("app")}

    # 角色成员是 <owner>/<name>;归到应用,记显示名。只有启用角色的应用才算"分了角色"。
    held: dict[str, dict[str, list[str]]] = {}
    apps_with_roles = set()
    for role in roles:
        app = _app_of_role(role.get("name") or "", apps)
        if not app:
            continue   # 不属于任何已知应用的角色(如 Casdoor 自用),不进视图
        if role.get("isEnabled") is not False:
            apps_with_roles.add(app)
        display = role.get("displayName") or role["name"][len(app) + 1:]
        for member in role.get("users") or []:
            held.setdefault(member, {}).setdefault(app, []).append(display)

    by_user: dict[str, dict[str, dict]] = {}
    for r in records.values():
        by_user.setdefault(r.get("open_id") or "", {})[r.get("app") or ""] = r

    rows, anomalies = [], []
    seen_ids = set()
    for u in users:
        oid = u.get("id") or ""
        seen_ids.add(oid)
        name = u.get("displayName") or u.get("name") or oid
        enabled = not u.get("isForbidden")
        group_apps = {g[len(prefix):] for g in (u.get("groups") or []) if (g or "").startswith(prefix)}
        role_map = held.get(f"{u.get('owner')}/{u.get('name')}", {})
        recs = by_user.get(oid, {})

        offboarded = sorted(f"{app}:{r.get('status')}" for app, r in recs.items()
                            if r.get("status") in ("disabled", "disable_failed"))
        if enabled and offboarded:
            anomalies.append(_anomaly("offboarded_but_enabled", name, oid, extra=", ".join(offboarded)))
        anomalies += _account_anomalies(u, name, oid, enabled, bool(group_apps or role_map), by_id, on, remind_max)
        account_cols = _account_columns(u, by_id)

        for app in sorted(group_apps | set(role_map) | set(recs)):
            has_access = app in group_apps
            role_names = sorted(role_map.get(app, []))
            rec = recs.get(app) or {}
            approved = rec.get("status") == "approved"
            rows.append({
                "name": name, "open_id": oid, "email": u.get("email") or "", "app": app,
                "account": "enabled" if enabled else "disabled",
                **account_cols,
                "has_access": "yes" if has_access else "no",
                "roles": ", ".join(role_names),
                "approval_status": rec.get("status", ""),
                "approved_by": rec.get("approved_by", ""), "approved_at": rec.get("approved_at", ""),
                "source": "approval" if approved else ("casdoor-only" if has_access else ""),
            })
            if not enabled:
                if has_access or role_names:
                    anomalies.append(_anomaly("disabled_with_access", name, oid, app))
                continue
            if role_names and not has_access:
                anomalies.append(_anomaly("role_without_access", name, oid, app, ", ".join(role_names)))
            if has_access and not role_names and app in apps_with_roles:
                anomalies.append(_anomaly("access_without_role", name, oid, app))
            if has_access and not approved:
                anomalies.append(_anomaly("access_without_approval", name, oid, app))
            if approved and not has_access:
                anomalies.append(_anomaly("approved_but_no_access", name, oid, app))

    for oid, recs in by_user.items():
        if oid in seen_ids:
            continue
        for app, r in sorted(recs.items()):
            if r.get("status") in ("approved", "pending", "disable_failed"):
                anomalies.append(_anomaly("record_user_missing", r.get("name") or oid, oid, app, r.get("status")))

    rows.sort(key=lambda r: (r["app"], r["name"], r["open_id"]))
    return rows, _sorted(anomalies)


async def check_feishu(users: list[dict]) -> list[dict]:
    """逐个查启用中的飞书账号(id 以 ou_ 开头)的在职状态。和对账不同:查不到状态不跳过,而是列出来。"""
    from even_auth_gov import offboard, reconcile
    sem = asyncio.Semaphore(_FEISHU_CONCURRENCY)

    async def one(u: dict) -> dict | None:
        oid, name = u.get("id") or "", u.get("displayName") or u.get("name") or ""
        async with sem:
            try:
                status = await reconcile.fetch_feishu_status(oid)
            except reconcile.FeishuUserNotFound:
                return _anomaly("departed_but_enabled", name, oid, extra="飞书已查无此人")
            except Exception as e:
                return _anomaly("feishu_check_failed", name, oid, extra=str(e))
        if offboard.offboard_flags(status):
            return _anomaly("departed_but_enabled", name, oid, extra="飞书状态为离职/冻结")
        return None

    targets = [u for u in users if not u.get("isForbidden") and (u.get("id") or "").startswith("ou_")]
    found = await asyncio.gather(*(one(u) for u in targets))
    return _sorted([a for a in found if a])


def _write(out: IO[str], columns: list[str], rows: list[dict]) -> None:
    w = csv.DictWriter(out, fieldnames=columns, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in columns})


def _summary(anomalies: list[dict]) -> str:
    counts = {s: sum(1 for a in anomalies if a["severity"] == s) for s in _SEVERITY_ORDER}
    return f"high={counts['high']} medium={counts['medium']} info={counts['info']}"


async def alert(client) -> int:
    """每日对账后调用:有高/中级异常就推一条飞书文本给全局审批人,返回推送的异常数(没推送为 0)。"""
    from even_auth_gov.feishu import send_text
    _, anomalies = build(await snapshot(client))
    actionable = [a for a in anomalies if a["severity"] in ("high", "medium")]
    if not actionable:
        logger.info("权限核对:无需处理的异常(%s)", _summary(anomalies))
        return 0
    approver = settings.approver_feishu_id()
    if not approver or approver == "ou_xxx":
        logger.warning("权限核对发现 %d 项需处理的异常,但未配置 APPROVER_FEISHU_ID,无法推送", len(actionable))
        return 0
    high = sum(1 for a in actionable if a["severity"] == "high")
    lines = [f"🔎 每日权限核对:{len(actionable)} 项需处理(高 {high} / 中 {len(actionable) - high})"]
    for a in actionable[:ALERT_MAX_ITEMS]:
        tag = "高" if a["severity"] == "high" else "中"
        lines.append(f"- [{tag}] {a['name']} ({a['open_id']}){' ' + a['app'] if a['app'] else ''}:{a['detail']}")
    if len(actionable) > ALERT_MAX_ITEMS:
        lines.append(f"…另有 {len(actionable) - ALERT_MAX_ITEMS} 项")
    lines.append("完整清单:python -m even_auth_gov.access_report --anomalies")
    await send_text(approver, "\n".join(lines))
    return len(actionable)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="权限视图(谁能访问什么)/ 离职撤权核对,输出 CSV")
    ap.add_argument("-o", "--output", help="输出文件(默认 stdout)")
    ap.add_argument("--app", default="", help="只看某个应用")
    ap.add_argument("--anomalies", action="store_true", help="输出核对异常清单而不是权限视图")
    ap.add_argument("--check-feishu", action="store_true", help="(配合 --anomalies)逐个查飞书在职状态")
    args = ap.parse_args(argv)

    import httpx
    from even_auth_gov import logging_setup
    logging_setup.setup()

    async def _go():
        async with httpx.AsyncClient(base_url=settings.casdoor_endpoint()) as client:
            snap = await snapshot(client)
        rows, anomalies = build(snap)
        if args.anomalies and args.check_feishu:
            anomalies = _sorted(anomalies + await check_feishu(snap["users"]))
        return rows, anomalies

    try:
        rows, anomalies = asyncio.run(_go())
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        return 2
    if args.app:
        rows = [r for r in rows if r["app"] == args.app]
        anomalies = [a for a in anomalies if a["app"] in (args.app, "")]
    columns, data = (ANOMALY_COLUMNS, anomalies) if args.anomalies else (COLUMNS, rows)
    if args.output:
        with open(args.output, "w", encoding="utf-8-sig", newline="") as f:   # Excel 直接打开中文不乱码
            _write(f, columns, data)
        print(f"{len(data)} rows -> {args.output}", file=sys.stderr)
    else:
        _write(sys.stdout, columns, data)
    print(f"anomalies: {_summary(anomalies)}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
