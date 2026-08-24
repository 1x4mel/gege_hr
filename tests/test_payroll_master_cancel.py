"""WP-QA-SSA Sprint 3 — cancel + timeline unit tests (bench-free stub).

Covers ``api/payroll_master.cancel_salary_structure_assignment``,
``bulk_cancel_assignments`` and ``ssa_timeline``:

  U8   Cancel đơn: reason rỗng → throw; hợp lệ → doc.cancel + audit + commit
  U9   Bulk cancel: 1 NV có SSA (cancel) + 1 NV không có (skipped) — không rollback
  U10  Timeline: trả mọi docstatus với state Draft/Submitted/Cancelled, mới nhất trước
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest


class FrappeError(Exception):
    pass


class NSDict(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as e:
            raise AttributeError(key) from e


class FakeSSA:
    def __init__(self, name, employee, structure, from_date, docstatus):
        self.name = name
        self.employee = employee
        self.salary_structure = structure
        self.from_date = from_date
        self.docstatus = docstatus
        self.cancelled = False

    def cancel(self):
        if self.docstatus != 1:
            raise FrappeError("cannot cancel non-submitted SSA")
        self.cancelled = True
        self.docstatus = 2
        return self


class Stub:
    def __init__(self, *, employees, ssa_docs):
        self.employees = employees  # {id: name}
        self.ssa_docs = ssa_docs  # {ssa_name: FakeSSA}
        self.commits = 0

    def db_exists(self, doctype, name):
        if doctype == "Employee":
            return name in self.employees
        if doctype == "Salary Structure Assignment":
            return name in self.ssa_docs
        return False

    def submitted_for(self, emp):
        return [
            {"name": d.name, "from_date": d.from_date}
            for d in self.ssa_docs.values()
            if d.employee == emp and d.docstatus == 1
        ]


@pytest.fixture()
def api(monkeypatch):
    def _make(**kw):
        stub = Stub(**kw)

        class _DB:
            exists = staticmethod(stub.db_exists)

            @staticmethod
            def get_value(doctype, name, fieldname=None, **_k):
                return None

            @staticmethod
            def commit():
                stub.commits += 1

        def get_all(doctype, filters=None, fields=None, order_by=None, **_k):
            if doctype != "Salary Structure Assignment":
                return []
            emp = filters.get("employee")
            ds = filters.get("docstatus")
            rows = [
                {
                    "name": d.name,
                    "employee": d.employee,
                    "salary_structure": d.salary_structure,
                    "from_date": d.from_date,
                    "base": 0,
                    "docstatus": d.docstatus,
                    "creation": "2026-08-19 10:00:00",
                }
                for d in stub.ssa_docs.values()
                if d.employee == emp and (ds is None or d.docstatus == ds)
            ]
            return [{f: r[f] for f in fields if f in r} for r in rows] if fields else rows

        def get_doc(doctype, name):
            if doctype == "Salary Structure Assignment" and name in stub.ssa_docs:
                return stub.ssa_docs[name]
            raise FrappeError(f"no doc {doctype} {name}")

        frappe_mod = types.ModuleType("frappe")
        frappe_mod.db = _DB()
        frappe_mod.get_all = get_all
        frappe_mod.get_doc = get_doc
        frappe_mod.log_error = lambda *a, **k: None
        frappe_mod.throw = lambda msg, exc=None: (_ for _ in ()).throw(FrappeError(msg))
        frappe_mod._ = lambda s: s
        frappe_mod.whitelist = lambda *a, **k: a[0] if a and callable(a[0]) else (lambda f: f)
        frappe_mod.get_traceback = lambda: "tb"
        frappe_mod.utils = types.SimpleNamespace(getdate=lambda v=None: v)
        monkeypatch.setitem(sys.modules, "frappe", frappe_mod)
        monkeypatch.setitem(sys.modules, "frappe.utils", frappe_mod.utils)

        fake_admin = types.ModuleType("gege_hr.gege_hr.api.admin")
        fake_admin._require_hr_admin = lambda: None
        fake_admin._audit_admin = lambda *a, **k: None
        fake_admin._company_for_employee = lambda e: "GeGe Esport"
        fake_admin._default_company = lambda: "GeGe Esport"
        monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.api.admin", fake_admin)

        fake_pagination = types.ModuleType("gege_hr.gege_hr.utils.pagination")
        fake_pagination.clamp_limit = lambda v, default=200: min(int(v or default), 500)
        monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.utils.pagination", fake_pagination)

        mod = importlib.reload(importlib.import_module("gege_hr.gege_hr.api.payroll_master"))
        return stub, mod

    return _make


# U8 — single cancel: mandatory reason, cancel + audit + commit
def test_u8_cancel_requires_reason_and_cancels(api):
    doc = FakeSSA("SSA-1", "E1", "Lương CB VN", "2024-01-01", 1)
    stub, mod = api(employees={"E1": "NV A"}, ssa_docs={"SSA-1": doc})

    with pytest.raises(FrappeError) as ei:
        mod.cancel_salary_structure_assignment("E1", "SSA-1", reason="  ")
    assert "Lý do hủy gán" in str(ei.value)

    res = mod.cancel_salary_structure_assignment("E1", "SSA-1", reason="Gán nhầm")
    assert res == {"name": "SSA-1", "cancelled": True}
    assert doc.cancelled is True and doc.docstatus == 2
    assert stub.commits == 1


# U9 — bulk cancel buckets (cancelled / skipped), no rollback
def test_u9_bulk_cancel_buckets(api):
    doc = FakeSSA("SSA-2", "E2", "Lương CB VN", "2024-01-01", 1)
    stub, mod = api(employees={"E1": "A", "E2": "B"}, ssa_docs={"SSA-2": doc})

    with pytest.raises(FrappeError):
        mod.bulk_cancel_assignments(["E2"], reason="")  # reason mandatory

    res = mod.bulk_cancel_assignments(["E1", "E2"], reason="Điều chỉnh lại")
    assert res["cancelled"] == ["E2"]
    assert res["skipped"][0]["employee"] == "E1"
    assert res["failed"] == []
    assert doc.cancelled is True


# U10 — timeline lists every state, newest first
def test_u10_timeline_states(api):
    docs = {
        "SSA-A": FakeSSA("SSA-A", "E1", "Lương CB VN", "2024-01-01", 2),  # cancelled
        "SSA-B": FakeSSA("SSA-B", "E1", "Lương CB VN", "2025-01-01", 1),  # submitted
        "SSA-C": FakeSSA("SSA-C", "E1", "Lương CB VN", "2026-01-01", 0),  # draft
    }
    _, mod = api(employees={"E1": "NV A"}, ssa_docs=docs)

    res = mod.ssa_timeline("E1")
    assert res["employee"] == "E1"
    states = {e["name"]: e["state"] for e in res["events"]}
    assert states == {"SSA-A": "Cancelled", "SSA-B": "Submitted", "SSA-C": "Draft"}
