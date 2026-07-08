"""审批工作流状态机(pending/approved/denied/disabled/disable_failed + 审计)。
Casdoor 是用户真理源;本 store 只记审批流转与审计,不存用户主数据。

APP-AWARE(设计评审 #2 #16):准入是按 (open_id, app) 记的 —— 同一用户可以
对应用 A 待批、对应用 B 已批,互不影响。内部 key = "<app> <open_id>",每条
记录同时存 open_id 和 app 字段,便于按用户聚合(离职用)。
离职是按**用户**的(禁用 Casdoor 账号 = 一次性关掉全部应用),见
records_for_open_id / mark_all_disabled / mark_disable_failed_all。

并发(#13):read-modify-write 同时持进程内锁 + **文件锁(flock)**,
多进程/多副本共享 store 文件时也不互相覆盖。flock 打在独立 .lock 文件上——
因为 _save 用 tmp+replace 换 inode,锁数据文件的 fd 会失效。
守卫转换(#15):mark_pending 不覆盖 approved/disabled/pending 终态或在途态,
只允许 denied → 重新申请。终态用户(离职 disabled)的 signup 重放不会被悄悄重置回 pending。
这条守卫现在按 (open_id, app) 独立生效。
"""
from __future__ import annotations
import contextlib
import fcntl
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.Lock()

# 允许 mark_pending 建/重置为 pending 的前置状态:无记录、或曾被拒(可重新申请)。
_REAPPLY_ALLOWED = {None, "denied"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _key(open_id: str, app: str) -> str:
    return f"{app} {open_id}"

def _file() -> Path:
    raw = os.getenv("APPROVAL_STORE_FILE", "").strip()
    return Path(raw).expanduser() if raw else Path("data/approvals.json")

def _load() -> dict:
    p = _file()
    if not p.exists():
        return {"records": {}, "updated_at": ""}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        d = {}
    records = d.get("records", {}) if isinstance(d, dict) else {}
    return {"records": records if isinstance(records, dict) else {}, "updated_at": ""}

def _save(d: dict) -> None:
    p = _file()
    p.parent.mkdir(parents=True, exist_ok=True)
    d["updated_at"] = _now()
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


@contextlib.contextmanager
def _mutate():
    """进程内锁 + 跨进程文件锁下的 read-modify-write。yield records dict,退出时落盘。"""
    with _LOCK:
        p = _file()
        p.parent.mkdir(parents=True, exist_ok=True)
        lockp = p.with_suffix(p.suffix + ".lock")
        with open(lockp, "w") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                d = _load()
                yield d["records"]
                _save(d)
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def get(open_id: str, app: str):
    with _LOCK:
        r = _load()["records"].get(_key(open_id, app))
    return dict(r) if r else None

def _set(open_id: str, app: str, **fields):
    with _mutate() as recs:
        key = _key(open_id, app)
        rec = recs.get(key, {"open_id": open_id, "app": app})
        rec.update(fields)
        recs[key] = rec

def mark_pending(open_id: str, app: str, profile: dict) -> bool:
    """返回是否需要发审批卡片(新建或允许的重新申请)。
    守卫(#15):approved/disabled/pending 不被重置,只有无记录或 denied 可转 pending。
    按 (open_id, app) 独立生效 —— 同一用户对另一个 app 的状态不受影响。"""
    with _mutate() as recs:
        key = _key(open_id, app)
        existing = recs.get(key)
        if existing and existing.get("status") not in _REAPPLY_ALLOWED:
            return False  # 在途/已批/已停用 → 不重置、不重发卡
        recs[key] = {
            "open_id": open_id, "app": app, "status": "pending", "applied_at": _now(), "notified_at": "",
            "name": profile.get("name", ""), "email": profile.get("email", ""),
        }
    return True

def mark_notified(open_id: str, app: str):
    """审批卡片已成功送达时留痕;reconcile 据此判断是否需要补发(#12)。"""
    _set(open_id, app, notified_at=_now())

def mark_approved(open_id: str, app: str, by: str):
    _set(open_id, app, status="approved", approved_by=by, approved_at=_now())

def mark_denied(open_id: str, app: str, by: str):
    _set(open_id, app, status="denied", denied_by=by, denied_at=_now())

def records_for_open_id(open_id: str) -> list[dict]:
    """该用户名下所有 app 的记录(离职时用来逐个移组)。"""
    with _LOCK:
        recs = _load()["records"]
        return [dict(r) for r in recs.values() if r.get("open_id") == open_id]

def mark_all_disabled(open_id: str, reason: str):
    """离职是按用户的:该用户名下**每一个** (open_id, app) 记录都置 disabled。"""
    with _mutate() as recs:
        for rec in recs.values():
            if rec.get("open_id") == open_id:
                rec["status"] = "disabled"
                rec["disabled_reason"] = reason
                rec["disabled_at"] = _now()

def mark_disable_failed_all(open_id: str, reason: str):
    """离职禁用重试用尽仍失败:该用户名下每个记录都留痕,供 reconcile 重扫 + 人工排查
    (安全攸关:绝不漏禁)。"""
    with _mutate() as recs:
        for rec in recs.values():
            if rec.get("open_id") == open_id:
                rec["status"] = "disable_failed"
                rec["disable_failed_reason"] = reason
                rec["disable_failed_at"] = _now()

def all_records() -> dict:
    """原始 {key: record} 映射(record 自带 open_id/app/status),供 reconcile 遍历去重。"""
    with _LOCK:
        return {k: dict(v) for k, v in _load()["records"].items()}
