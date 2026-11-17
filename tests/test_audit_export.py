import csv, io
from even_auth_gov import approval_store, audit_export


def _seed(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    approval_store.mark_pending("ou_a", "app-a", {"name": "甲", "email": "a@e.com"})
    approval_store.mark_approved("ou_a", "app-a", "ou_boss")
    approval_store.mark_pending("ou_b", "app-b", {"name": "乙"})
    approval_store.mark_denied("ou_b", "app-b", "ou_boss2")
    approval_store.mark_pending("ou_c", "app-a", {"name": "丙"})
    approval_store.mark_all_disabled("ou_c", "offboard_event")


def _rows(text):
    return list(csv.DictReader(io.StringIO(text)))


def test_export_all_records_with_audit_fields(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    out = io.StringIO()
    n = audit_export.export_csv(out)
    rows = _rows(out.getvalue())
    assert n == 3 == len(rows)
    by = {(r["open_id"], r["app"]): r for r in rows}
    assert by[("ou_a", "app-a")]["status"] == "approved"
    assert by[("ou_a", "app-a")]["approved_by"] == "ou_boss" and by[("ou_a", "app-a")]["approved_at"]
    assert by[("ou_a", "app-a")]["email"] == "a@e.com"
    assert by[("ou_b", "app-b")]["denied_by"] == "ou_boss2"
    assert by[("ou_c", "app-a")]["disabled_reason"] == "offboard_event"


def test_export_filters_by_app_and_status(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    out = io.StringIO()
    audit_export.export_csv(out, app="app-a")
    assert {r["open_id"] for r in _rows(out.getvalue())} == {"ou_a", "ou_c"}
    out = io.StringIO()
    audit_export.export_csv(out, status="denied")
    assert [r["open_id"] for r in _rows(out.getvalue())] == ["ou_b"]


def test_main_writes_csv_file(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    dest = tmp_path / "audit.csv"
    assert audit_export.main(["-o", str(dest), "--app", "app-b"]) == 0
    rows = _rows(dest.read_text(encoding="utf-8-sig"))
    assert [(r["open_id"], r["status"]) for r in rows] == [("ou_b", "denied")]


def test_export_includes_role_granted_at_approval(monkeypatch, tmp_path):
    """F-5.8.2:审批时分配的角色也要可查可导出;没分配角色的记录该列为空。"""
    _seed(monkeypatch, tmp_path)
    approval_store.mark_pending("ou_d", "app-a", {"name": "丁"})
    approval_store.mark_approved("ou_d", "app-a", "ou_boss", role="app-a-editor")
    out = io.StringIO()
    audit_export.export_csv(out)
    by = {(r["open_id"], r["app"]): r for r in _rows(out.getvalue())}
    assert by[("ou_d", "app-a")]["role"] == "app-a-editor"
    assert by[("ou_a", "app-a")]["role"] == ""


def test_export_includes_escalation_trail(monkeypatch, tmp_path):
    monkeypatch.setenv("APPROVAL_STORE_FILE", str(tmp_path / "a.json"))
    approval_store.mark_pending("ou_a", "app-a", {"name": "甲"})
    approval_store.mark_escalated("ou_a", "app-a", "ou_esc")
    approval_store.mark_approved("ou_a", "app-a", "ou_esc")
    buf = io.StringIO()
    audit_export.export_csv(buf)
    row = _rows(buf.getvalue())[0]
    assert row["escalated_to"] == "ou_esc" and row["escalated_at"] and row["approved_by"] == "ou_esc"
