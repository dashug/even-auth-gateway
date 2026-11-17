"""迁移:把 CS Hub allowed_emails 里的操作员,按邮箱匹配 Casdoor 用户并加入 cs-hub 的准入组。
未匹配(飞书还没登录过、Casdoor 无此用户)的记入 unmatched,待其首登后走正常审批。
端点见 casdoor-findings.md §3(get-user?owner=<org>&email=<email> + clientId/secret 鉴权)。

APP-AWARE(设计评审 #2 #16):这批操作员全部是 cs-hub 试点的老用户,组名跟随
settings.default_app() 走(f"approved-{app}"),与 decision.py 现在用的 per-app
组命名保持一致 —— 不再写死 "approved-operators",否则迁移进去的组会跟
新用户走的审批流程分道扬镳,变成一个孤儿组。

用法(先演练、再真跑;环境变量同治理服务:CASDOOR_ENDPOINT/ORG/CLIENT_ID/CLIENT_SECRET/DEFAULT_APP):
    python -m even_auth_gov.migrate_emails data/admin_auth.json --dry-run
    python -m even_auth_gov.migrate_emails data/admin_auth.json
    python -m even_auth_gov.migrate_emails data/admin_auth.json --role cs-hub-operator   # 应用分了角色时
"""
from __future__ import annotations
import argparse, asyncio, json, logging, sys
from pathlib import Path
from even_auth_gov import casdoor_admin as ca, settings

logger = logging.getLogger(__name__)

def _approved_group() -> str:
    return f"approved-{settings.default_app()}"

async def find_user_by_email(client, email: str) -> str | None:
    """按邮箱查 Casdoor 用户,返回其飞书 open_id(add_to_group 的入参)或 None。"""
    return await ca.find_open_id_by_email(client, email)

async def _check_role(client, role: str) -> None:
    """--role 必须是该应用当前启用的角色;读不到角色清单也不放行(宁可不迁,不迁成"有准入没角色")。"""
    app = settings.default_app()
    roles = await ca.list_app_roles(client, app, settings.known_apps())
    if roles is None:
        raise ValueError(f"读取 {app} 的角色清单失败,无法核对角色 {role},未迁移任何人")
    if role not in {r["name"] for r in roles}:
        raise ValueError(f"{role} 不是 {app} 当前启用的角色(现有: {', '.join(r['name'] for r in roles) or '无'}),未迁移任何人")

async def _grant(client, uid: str, group: str, role: str) -> bool:
    """与审批批准同序:先赋角色,再开准入组 —— 赋角失败就不开门。"""
    if role and not await ca.assign_role(client, user_id=uid, role=role):
        return False
    return await ca.add_to_group(client, user_id=uid, group=group)

async def run(auth_file: str, client, dry_run: bool = False, role: str = "") -> dict:
    """dry_run=True 只查匹配、不写 Casdoor:migrated 表示"会被迁移"的邮箱。
    role:分了角色的应用(角色强制)给迁移进来的人一并赋的角色;不是该应用启用的角色则抛 ValueError、一个人都不动。"""
    p = Path(auth_file)
    if not p.exists():
        return {"migrated": [], "unmatched": []}
    try:
        emails = json.loads(p.read_text(encoding="utf-8")).get("allowed_emails", [])
    except Exception:
        return {"migrated": [], "unmatched": []}
    if role:
        await _check_role(client, role)
    migrated, unmatched = [], []
    group = _approved_group()
    for email in emails:
        uid = await find_user_by_email(client, email)
        if uid and (dry_run or await _grant(client, uid, group, role)):
            migrated.append(email)
        else:
            unmatched.append(email)
    logger.info("Email migration%s: %d migrated, %d unmatched",
                " (dry-run)" if dry_run else "", len(migrated), len(unmatched))
    return {"migrated": migrated, "unmatched": unmatched}

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CS Hub allowed_emails → Casdoor approved-<DEFAULT_APP> 组")
    ap.add_argument("auth_file", help="CS Hub 的 admin_auth.json(含 allowed_emails)")
    ap.add_argument("--dry-run", action="store_true", help="只查匹配,不改 Casdoor")
    ap.add_argument("--role", default="", help="该应用分了角色时必填:迁移进来的人一并赋此角色(如 cs-hub-operator)")
    args = ap.parse_args(argv)
    if not Path(args.auth_file).exists():
        print(f"文件不存在: {args.auth_file}", file=sys.stderr)
        return 2
    import httpx
    from even_auth_gov import logging_setup
    logging_setup.setup()

    async def _go():
        async with httpx.AsyncClient(base_url=settings.casdoor_endpoint()) as client:
            return await run(args.auth_file, client, dry_run=args.dry_run, role=args.role)

    try:
        report = asyncio.run(_go())
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    print(json.dumps({"group": _approved_group(), "role": args.role, "dry_run": args.dry_run, **report},
                     ensure_ascii=False, indent=2))
    return 0

if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
