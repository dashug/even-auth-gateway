"""审计导出(PRD F-5.8.2):把审批库里每条 (open_id, app) 的准入/拒绝/停用留痕导出成 CSV。

用法(读 APPROVAL_STORE_FILE,与治理服务同一份):
    python -m even_auth_gov.audit_export -o audit.csv
    python -m even_auth_gov.audit_export --app cs-hub --status approved
    docker compose exec even-auth-gov python -m even_auth_gov.audit_export > audit.csv
    python -m even_auth_gov.audit_export --accounts -o accounts.csv   # 受管账号变更历史(里程碑 4a)

注意:这里只覆盖经网关审批流转过的记录。迁移/在 Casdoor 后台手工加组的授权不经过本库,
要看"此刻谁能访问什么",以 Casdoor 组成员为准。
"""
from __future__ import annotations
import argparse
import csv
import json
import sys
from typing import IO

from even_auth_gov import approval_store, settings

COLUMNS = [
    "open_id", "app", "status", "name", "email",
    "applied_at", "first_notified_at", "notified_at", "escalated_to", "escalated_at",
    "approved_by", "approved_at", "role",
    "denied_by", "denied_at", "denied_reason",
    "disabled_reason", "disabled_at", "reopened_at",
    "disable_failed_reason", "disable_failed_at",
]


def export_csv(out: IO[str], app: str = "", status: str = "") -> int:
    """写 CSV 到 out,返回行数。按 (app, open_id) 排序,结果可复现、便于 diff。"""
    rows = [r for r in approval_store.all_records().values()
            if (not app or r.get("app") == app) and (not status or r.get("status") == status)]
    rows.sort(key=lambda r: (r.get("app", ""), r.get("open_id", "")))
    w = csv.DictWriter(out, fieldnames=COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in COLUMNS})
    return len(rows)


EVENT_COLUMNS = ["ts", "event", "user_id", "name", "email", "kind", "sponsor", "expires", "actor", "detail"]


def export_account_events(out: IO[str]) -> int:
    """受管账号变更历史(account_events.jsonl,里程碑 4a)导出为 CSV,返回行数。坏行(如进程被杀留下的半行)跳过。"""
    path = settings.account_events_file()
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                rows.append(rec)
    w = csv.DictWriter(out, fieldnames=EVENT_COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in EVENT_COLUMNS})
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="导出审批/停用审计记录为 CSV")
    ap.add_argument("-o", "--output", help="输出文件(默认 stdout)")
    ap.add_argument("--app", default="", help="只导出某个应用")
    ap.add_argument("--status", default="", help="只导出某个状态(pending/approved/denied/disabled/disable_failed/expired)")
    ap.add_argument("--accounts", action="store_true", help="导出受管账号变更历史(account_events.jsonl)")
    args = ap.parse_args(argv)
    if args.accounts:
        export = export_account_events
    else:
        def export(out):
            return export_csv(out, app=args.app, status=args.status)
    if args.output:
        # utf-8-sig:Excel 直接打开中文不乱码
        with open(args.output, "w", encoding="utf-8-sig", newline="") as f:
            n = export(f)
        print(f"exported {n} rows -> {args.output}", file=sys.stderr)
    else:
        export(sys.stdout)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
