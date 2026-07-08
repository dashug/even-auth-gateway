"""审批工作流状态机(pending/approved/denied/disabled/disable_failed + 审计)。
Casdoor 是用户真理源;本 store 只记审批流转与审计,不存用户主数据。

并发(#13):read-modify-write 同时持进程内锁 + **文件锁(flock)**,
多进程/多副本共享 store 文件时也不互相覆盖。flock 打在独立 .lock 文件上——
因为 _save 用 tmp+replace 换 inode,锁数据文件的 fd 会失效。
守卫转换(#15):mark_pending 不覆盖 approved/disabled/pending 终态或在途态,
只允许 denied → 重新申请。终态用户(离职 disabled)的 signup 重放不会被悄悄重置回 pending。
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


def get(open_id: str):
    with _LOCK:
        r = _load()["records"].get(open_id)
    return dict(r) if r else None

def _set(open_id: str, **fields):
    with _mutate() as recs:
        rec = recs.get(open_id, {"open_id": open_id})
        rec.update(fields)
        recs[open_id] = rec

def mark_pending(open_id: str, profile: dict) -> bool:
    """返回是否需要发审批卡片(新建或允许的重新申请)。
    守卫(#15):approved/disabled/pending 不被重置,只有无记录或 denied 可转 pending。"""
    with _mutate() as recs:
        existing = recs.get(open_id)
        if existing and existing.get("status") not in _REAPPLY_ALLOWED:
            return False  # 在途/已批/已停用 → 不重置、不重发卡
        recs[open_id] = {
            "open_id": open_id, "status": "pending", "applied_at": _now(), "notified_at": "",
            "name": profile.get("name", ""), "email": profile.get("email", ""),
        }
    return True

def mark_notified(open_id: str):
    """审批卡片已成功送达时留痕;reconcile 据此判断是否需要补发(#12)。"""
    _set(open_id, notified_at=_now())

def mark_approved(open_id: str, by: str):
    _set(open_id, status="approved", approved_by=by, approved_at=_now())

def mark_denied(open_id: str, by: str):
    _set(open_id, status="denied", denied_by=by, denied_at=_now())

def mark_disabled(open_id: str, reason: str):
    _set(open_id, status="disabled", disabled_reason=reason, disabled_at=_now())

def mark_disable_failed(open_id: str, reason: str):
    """离职禁用重试用尽仍失败:留痕供 reconcile 重扫 + 人工排查(安全攸关:绝不漏禁)。"""
    _set(open_id, status="disable_failed", disable_failed_reason=reason, disable_failed_at=_now())
