"""Bench-free lifecycle tests for the VN Payroll Review Period actions added
by plan payroll-periods-desk-free P2 (§5.2 — cases VR-01…VR-08).

Covers ``api/payroll.update_payroll_review`` (Draft-only edit) and
``api/payroll.cancel_payroll_review`` (reason guard, submitted-slip guard,
draft-slip cleanup). Same reload-with-stub harness style as test_payroll_api.
"""

import importlib
import sys
import types

import pytest


class _FrappeError(Exception):
    pass


class _Period:
    """Fake VN Payroll Review Period doc."""

    def __init__(self, **kw):
        self.name = kw.get("name", "PER-001")
        self.status = kw.get("status", "Draft")
        self.docstatus = kw.get("docstatus", 0)
        self.from_date = kw.get("from_date", "2026-09-01")
        self.to_date = kw.get("to_date", "2026-09-30")
        self.attendance_period = kw.get("attendance_period", "")
        self.saved = False
        self.cancelled = False

    def save(self, *a, **k):
        self.saved = True
        return self

    def cancel(self):
        self.cancelled = True
        self.docstatus = 2
        return self

    def as_dict(self):
        return {"name": self.name, "status": self.status, "docstatus": self.docstatus}


def _build_stub_frappe():
    mod = types.ModuleType("frappe")
    mod.__path__ = []
    mod._ = lambda s: s
    mod.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)
    mod.log_error = lambda *a, **k: None
    mod.throw = lambda msg, *a, **k: (_ for _ in ()).throw(_FrappeError(str(msg)))

    utils = types.ModuleType("frappe.utils")
    utils.getdate = lambda v=None: __import__("datetime").date.today()
    utils.today = lambda: __import__("datetime").date.today()
    utils.flt = lambda v, p=None: float(v or 0)
    utils.cint = lambda v, p=None: int(v or 0)
    mod.utils = utils

    class _DB:
        def __init__(self):
            self.rows_by_doctype = {}  # doctype -> list[dict]
            self.set_values = []  # [(doctype, name, field, value)]
            self.deleted = []  # [(doctype, name)] via db.delete
            self.commits = 0

        def get_all(self, doctype, filters=None, fields=None, **kwargs):
            # Real Frappe returns frappe._dict — attribute access must work.
            return [types.SimpleNamespace(**dict(r)) for r in self.rows_by_doctype.get(doctype, [])]

        def get_value(self, *a, **k):
            return None

        def exists(self, *a, **k):
            return False

        def set_value(self, doctype, name, field, value=None, **k):
            values = field if isinstance(field, dict) else {field: value}
            self.set_values.append((doctype, name, values))

        def delete(self, doctype, filters=None, **k):
            self.deleted.append((doctype, filters))

        def commit(self):
            self.commits += 1

    mod.db = _DB()
    mod.deleted_docs = []

    def _delete_doc(doctype, name, *a, **k):
        mod.deleted_docs.append((doctype, name))
        return name

    mod.delete_doc = _delete_doc
    return mod, utils


@pytest.fixture
def fake(monkeypatch):
    stub, utils = _build_stub_frappe()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    api = importlib.reload(importlib.import_module("gege_hr.gege_hr.api.payroll"))

    monkeypatch.setattr(api, "_assert_closer", lambda: None)
    audit_log = []
    monkeypatch.setattr(api.audit_api, "log", lambda *a, **k: audit_log.append((a, k)))
    monkeypatch.setattr(api.calc, "period_has_confirmed_slips", lambda name: 0)

    fake_state = types.SimpleNamespace(
        api=api,
        stub=stub,
        audit_log=audit_log,
        period=_Period(),
        set_period=lambda **kw: setattr(fake_state, "period", _Period(**kw)),
    )
    monkeypatch.setattr(api, "_get_period", lambda name: fake_state.period)
    fake_state.set_rows = lambda doctype, rows: stub.db.rows_by_doctype.__setitem__(doctype, rows)
    return fake_state


# --------------------------------------------------------------------------- #
# VR-01 — update Draft thành công
# --------------------------------------------------------------------------- #
def test_vr01_update_draft_ok(fake):
    fake.set_period(status="Draft", from_date="2026-09-01", to_date="2026-09-30")
    res = fake.api.update_payroll_review(
        name="PER-001", from_date="2026-08-25", to_date="2026-09-25", attendance_period="AT-09"
    )
    assert res["from_date"] == "2026-08-25"
    assert res["to_date"] == "2026-09-25"
    assert res["attendance_period"] == "AT-09"
    assert fake.period.saved is True
    assert fake.audit_log, "phải có audit sau khi sửa"


# --------------------------------------------------------------------------- #
# VR-02 — update kỳ Calculated → chặn
# --------------------------------------------------------------------------- #
def test_vr02_update_only_draft(fake):
    fake.set_period(status="Calculated")
    with pytest.raises(_FrappeError) as ei:
        fake.api.update_payroll_review(name="PER-001", to_date="2026-09-25")
    assert "Nháp" in str(ei.value)
    assert fake.period.saved is False


# --------------------------------------------------------------------------- #
# VR-03 — update to < from → chặn
# --------------------------------------------------------------------------- #
def test_vr03_update_rejects_inverted_range(fake):
    fake.set_period(status="Draft", from_date="2026-09-01", to_date="2026-09-30")
    with pytest.raises(_FrappeError):
        fake.api.update_payroll_review(name="PER-001", to_date="2026-08-01")
    assert fake.period.saved is False


# --------------------------------------------------------------------------- #
# VR-04 — cancel Draft với lý do hợp lệ
# --------------------------------------------------------------------------- #
def test_vr04_cancel_draft_ok(fake):
    fake.set_period(status="Draft")
    fake.set_rows("VN Payroll Review Line", [])
    res = fake.api.cancel_payroll_review(name="PER-001", reason="nhầm kỳ công")
    assert res["status"] == "Cancelled"
    assert res["removed_slips"] == 0
    status_writes = [w for w in fake.stub.db.set_values if w[2].get("status") == "Cancelled"]
    assert status_writes, "phải set status=Cancelled trên period"
    assert fake.audit_log and "nhầm kỳ công" in fake.audit_log[-1][1].get("description", "")


# --------------------------------------------------------------------------- #
# VR-05 — cancel thiếu lý do → chặn
# --------------------------------------------------------------------------- #
def test_vr05_cancel_requires_reason(fake):
    with pytest.raises(_FrappeError):
        fake.api.cancel_payroll_review(name="PER-001", reason="abc")
    assert fake.stub.db.set_values == []


# --------------------------------------------------------------------------- #
# VR-06 — cancel Published có slip submitted → chặn, hướng dẫn reopen
# --------------------------------------------------------------------------- #
def test_vr06_cancel_blocked_by_submitted_slip(fake):
    fake.set_period(status="Published")
    fake.set_rows("VN Payroll Review Line", [{"name": "LN-1", "salary_slip": "SLIP-1"}])
    fake.set_rows("Salary Slip", [{"name": "SLIP-1"}])  # query docstatus=1 → có
    with pytest.raises(_FrappeError) as ei:
        fake.api.cancel_payroll_review(name="PER-001", reason="kết thúc kỳ")
    assert "Mở lại" in str(ei.value)
    assert fake.stub.deleted_docs == []


# --------------------------------------------------------------------------- #
# VR-07 — cancel Slips Generated chỉ slip draft → xoá slip draft rồi huỷ
# --------------------------------------------------------------------------- #
def test_vr07_cancel_cleans_draft_slips(fake):
    fake.set_period(status="Slips Generated")
    fake.set_rows(
        "VN Payroll Review Line",
        [
            {"name": "LN-1", "salary_slip": "SLIP-1"},
            {"name": "LN-2", "salary_slip": "SLIP-2"},
            {"name": "LN-3", "salary_slip": None},
        ],
    )
    fake.set_rows("Salary Slip", [])  # không slip submitted
    res = fake.api.cancel_payroll_review(name="PER-001", reason="dừng phát hành")
    assert res["status"] == "Cancelled"
    assert res["removed_slips"] == 2
    assert ("Salary Slip", "SLIP-1") in fake.stub.deleted_docs
    assert ("Salary Slip", "SLIP-2") in fake.stub.deleted_docs
    # link slip trên line được gỡ
    unlinked = [w for w in fake.stub.db.set_values if w[0] == "VN Payroll Review Line"]
    assert {w[1] for w in unlinked} == {"LN-1", "LN-2"}


# --------------------------------------------------------------------------- #
# VR-08 — non-caller (thiếu role) → 403 cả update lẫn cancel
# --------------------------------------------------------------------------- #
def test_vr08_non_caller_blocked(fake, monkeypatch):
    def _deny():
        raise _FrappeError("no permission")

    monkeypatch.setattr(fake.api, "_assert_closer", _deny)
    with pytest.raises(_FrappeError):
        fake.api.update_payroll_review(name="PER-001", to_date="2026-09-25")
    with pytest.raises(_FrappeError):
        fake.api.cancel_payroll_review(name="PER-001", reason="không có quyền")
