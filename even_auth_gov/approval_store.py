"""审批工作流状态机(pending/approved/denied/disabled + 审计)。
Casdoor 是用户真理源;本 store 只记审批流转与审计,不存用户主数据。
"""
from __future__ import annotations
import json, os, threading
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.Lock()

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

def get(open_id: str):
    with _LOCK:
        r = _load()["records"].get(open_id)
    return dict(r) if r else None

def _set(open_id: str, **fields):
    with _LOCK:
        d = _load()
        rec = d["records"].get(open_id, {"open_id": open_id})
        rec.update(fields)
        d["records"][open_id] = rec
        _save(d)

def mark_pending(open_id: str, profile: dict) -> bool:
    """返回是否为新建(需发审批卡片)。已 pending 的记录不重发。"""
    with _LOCK:
        d = _load()
        existing = d["records"].get(open_id)
        if existing and existing.get("status") == "pending":
            return False
        d["records"][open_id] = {
            "open_id": open_id, "status": "pending", "applied_at": _now(),
            "name": profile.get("name", ""), "email": profile.get("email", ""),
        }
        _save(d)
    return True

def mark_approved(open_id: str, by: str):
    _set(open_id, status="approved", approved_by=by, approved_at=_now())

def mark_denied(open_id: str, by: str):
    _set(open_id, status="denied", denied_by=by, denied_at=_now())

def mark_disabled(open_id: str, reason: str):
    _set(open_id, status="disabled", disabled_reason=reason, disabled_at=_now())

def mark_disable_failed(open_id: str, reason: str):
    """离职禁用重试用尽仍失败:留痕供 reconcile 重扫 + 人工排查(安全攸关:绝不漏禁)。"""
    _set(open_id, status="disable_failed", disable_failed_reason=reason, disable_failed_at=_now())
