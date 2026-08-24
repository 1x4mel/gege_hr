"""WP-QA-SSA — bulk Salary Structure Assignment (Quick-Assign) unit tests.

Covers ``api/payroll_master.ssa_status`` + ``bulk_assign_salary_structure``:

  U1  2 NV chưa có SSA, from_date=""        → mỗi NV nhận date_of_joining riêng
  U2  1 NV overlap SSA cũ                   → 1 assigned + 1 skipped (lý do engine), không rollback
  U3  Structure inactive / draft            → throw chặn cả batch, không gán ai
  U4  ssa_status: đủ / draft-only / đã có   → assignable + reason (OK/DRAFT_ONLY/HAS_SSA)
  U5  Idempotent: bulk 2 lần                → lần 2 toàn skipped
  U6  Structure có 2 fixed earnings         → fixed_allowance_count=2
  U7  1 NV lỗi hệ thống                     → vào failed + log_error, NV khác vẫn assign

Bench-free: stub ``frappe`` injected into ``sys.modules`` (same harness pattern
as tests/test_payroll_slip_generate.py).
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

    def get(self, key, default=None):
        return dict.get(self, key, default)


class FakeSSADoc:
    """Salary Structure Assignment double created by assign_salary_structure."""

    def __init__(self, payload, stub, name=None):
        self._stub = stub
        for k, v in (payload or {}).items():
            setattr(self, k, v)
        self.name = name or f"SSA-NEW-{len(stub.created_ssas) + 1}"
        self.docstatus = getattr(self, "docstatus", 0)
        self.inserted = False
        self.submitted = False
        self.cancelled = False

    def insert(self):
        self.inserted = True
        return self

    def submit(self):
        # Overlap engine sees the assignment the moment it is (re)queried —
        # simulate by registering it in the stub's submitted set.
        self.submitted = True
        self.docstatus = 1
        self._stub.created_ssas.append(self)
        return self

    def cancel(self):
        if self.docstatus != 1:
            raise RuntimeError("cannot cancel a non-submitted SSA")
        self.cancelled = True
        self.docstatus = 2
        return self


class FakeStructureDoc:
    def __init__(self, name, earnings):
        self.name = name
        self.earnings = [
            NSDict({"amount": amt, "amount_based_on_formula": formula}) for amt, formula in (earnings or [])
        ]


class StubFrappe:
    """Configurable frappe stub for the payroll_master bulk flow."""

    def __init__(
        self,
        *,
        employees,
        structures,
        submitted_ssas=None,
        draft_ssas=None,
        raise_on_create_for=(),
    ):
        # employees: {id: {employee_name, date_of_joining}}
        self.employees = employees
        # structures: {name: {"docstatus":1,"is_active":"Yes","earnings":[(amt,formula)]}}
        self.structures = structures
        # submitted_ssas: {emp: [{name, from_date, to_date}]}
        self.submitted_ssas = {k: [dict(r) for r in v] for k, v in (submitted_ssas or {}).items()}
        # draft_ssas: {emp: [name, ...]}
        self.draft_ssas = {k: list(v) for k, v in (draft_ssas or {}).items()}
        self.raise_on_create_for = set(raise_on_create_for)
        self.created_ssas: list[FakeSSADoc] = []
        self.log_errors: list[str] = []
        self.commits = 0

    # ---- helpers -----------------------------------------------------------

    def _existing_submitted(self, emp):
        return self.submitted_ssas.get(emp, []) + [
            {"name": d.name, "from_date": str(d.from_date), "to_date": None}
            for d in self.created_ssas
            if d.employee == emp
        ]

    # ---- frappe surface -----------------------------------------------------

    def _build_db(self):
        outer = self

        class _DB:
            def exists(inner, doctype, name):
                if doctype == "Employee":
                    return name in outer.employees
                if doctype == "Salary Structure":
                    return name in outer.structures
                return False

            def get_value(inner, doctype, name, fieldname=None, as_dict=False, **_kw):
                if doctype == "Employee":
                    emp = outer.employees.get(name) or {}
                    if isinstance(fieldname, (list, tuple)):
                        return NSDict({f: emp.get(f) for f in fieldname})
                    return emp.get(fieldname)
                if doctype == "Salary Structure":
                    st = outer.structures.get(name) or {}
                    if isinstance(fieldname, (list, tuple)):
                        return NSDict({f: st.get(f) for f in fieldname})
                    return st.get(fieldname)
                return None

            def commit(inner):
                outer.commits += 1
                return None

        return _DB()

    def get_all(self, doctype, filters=None, fields=None, **_kw):
        if doctype != "Salary Structure Assignment":
            return []
        # filters arrive either as dict (ssa_status) or list-of-lists (overlap).
        if isinstance(filters, dict):
            emp = filters.get("employee")
            docstatus = filters.get("docstatus")
        else:
            emp = None
            docstatus = None
            for cond in filters or []:
                if cond[0] == "employee":
                    emp = cond[2]
                elif cond[0] == "docstatus":
                    docstatus = cond[2]
        if docstatus == 1:
            rows = self._existing_submitted(emp or "")
        elif docstatus == 2:
            rows = [
                {"name": d.name, "from_date": str(d.from_date), "salary_structure": d.salary_structure}
                for d in self.existing_ssa_docs.values()
                if d.docstatus == 2 and d.employee == (emp or "")
            ]
        else:
            rows = [{"name": n} for n in self.draft_ssas.get(emp or "", [])]
        if fields:
            out = []
            for r in rows:
                out.append({f: r.get(f) for f in fields if f in r})
            return out
        return rows

    def get_doc(self, arg, name=None, **_kw):
        if isinstance(arg, dict):
            emp = arg.get("employee")
            if emp in self.raise_on_create_for:
                raise RuntimeError(f"simulated insert failure for {emp}")
            return FakeSSADoc(arg, self)
        if arg == "Salary Structure":
            st = self.structures.get(name) or {}
            return FakeStructureDoc(name, st.get("earnings"))
        if arg == "Salary Structure Assignment":
            if name not in self.existing_ssa_docs:
                raise FrappeError(f"SSA {name} does not exist")
            return self.existing_ssa_docs[name]
        raise FrappeError(f"unexpected get_doc {arg} {name}")

    def log_error(self, title=None, message=None, *_a, **_kw):
        self.log_errors.append(f"{title}: {message}")

    def throw(self, msg, exc=None):
        raise FrappeError(msg)


def _emp(eid, doj="2025-03-15", name="NV"):
    return eid, {"employee_name": name, "date_of_joining": doj}


_ST = {
    "Lương CB VN": {"docstatus": 1, "is_active": "Yes", "earnings": []},
    "E2E ST Allowance": {
        "docstatus": 1,
        "is_active": "Yes",
        "earnings": [(100000, False), (200000, False), (None, True)],
    },
    "ST-Inactive": {"docstatus": 1, "is_active": "No", "earnings": []},
}


@pytest.fixture()
def api(monkeypatch):
    def _make(**kw):
        stub = StubFrappe(**kw)
        stub.db = stub._build_db()

        utils = types.ModuleType("frappe.utils")
        utils.getdate = lambda v=None: (
            __import__("datetime").date.fromisoformat(str(v))
            if v
            else __import__("datetime").date(2026, 8, 19)
        )
        utils.flt = lambda v, p=None: round(float(v or 0), p if p is not None else 2)

        frappe_mod = types.ModuleType("frappe")
        frappe_mod.db = stub.db
        frappe_mod.get_all = stub.get_all
        frappe_mod.get_doc = stub.get_doc
        frappe_mod.log_error = stub.log_error
        frappe_mod.throw = stub.throw
        frappe_mod._ = lambda s: s
        frappe_mod.whitelist = lambda *a, **k: a[0] if a and callable(a[0]) else (lambda f: f)
        frappe_mod.utils = utils
        frappe_mod.get_traceback = lambda: "tb"

        monkeypatch.setitem(sys.modules, "frappe", frappe_mod)
        monkeypatch.setitem(sys.modules, "frappe.utils", utils)

        fake_admin = types.ModuleType("gege_hr.gege_hr.api.admin")
        fake_admin._require_hr_admin = lambda: None
        fake_admin._audit_admin = lambda *a, **kw: None
        fake_admin._company_for_employee = lambda e: "GeGe Esport"
        fake_admin._default_company = lambda: "GeGe Esport"
        monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.api.admin", fake_admin)

        mod = importlib.reload(importlib.import_module("gege_hr.gege_hr.api.payroll_master"))
        return stub, mod

    return _make


# --------------------------------------------------------------------------- #
# U1 — per-employee date_of_joining when from_date is empty
# --------------------------------------------------------------------------- #
def test_u1_uses_date_of_joining_per_employee(api):
    stub, mod = api(
        employees=dict([_emp("E1", doj="2025-01-10"), _emp("E2", doj="2026-06-20")]),
        structures=_ST,
    )
    res = mod.bulk_assign_salary_structure(["E1", "E2"], "Lương CB VN")

    assert res["assigned"] == ["E1", "E2"]
    assert res["skipped"] == []
    assert res["failed"] == []
    assert res["fixed_allowance_count"] == 0
    by_emp = {d.employee: d for d in stub.created_ssas}
    assert str(by_emp["E1"].from_date) == "2025-01-10"
    assert str(by_emp["E2"].from_date) == "2026-06-20"


# --------------------------------------------------------------------------- #
# U2 — overlap lands in skipped (Vietnamese engine reason), batch continues
# --------------------------------------------------------------------------- #
def test_u2_overlap_skipped_not_rollback(api):
    stub, mod = api(
        employees=dict([_emp("E1"), _emp("E2")]),
        structures=_ST,
        submitted_ssas={"E2": [{"name": "SSA-OLD", "from_date": "2025-01-01", "to_date": None}]},
    )
    res = mod.bulk_assign_salary_structure(["E1", "E2"], "Lương CB VN")

    assert res["assigned"] == ["E1"]
    assert len(res["skipped"]) == 1
    assert res["skipped"][0]["employee"] == "E2"
    assert "đã có" in res["skipped"][0]["reason"]
    assert res["failed"] == []
    assert stub.commits == 1  # committed despite the skip


# --------------------------------------------------------------------------- #
# U3 — inactive structure blocks the whole batch
# --------------------------------------------------------------------------- #
def test_u3_inactive_structure_blocked(api):
    stub, mod = api(employees=dict([_emp("E1")]), structures=_ST)
    with pytest.raises(FrappeError) as ei:
        mod.bulk_assign_salary_structure(["E1"], "ST-Inactive")
    assert "ngừng hoạt động" in str(ei.value)
    assert stub.created_ssas == []


# --------------------------------------------------------------------------- #
# U4 — ssa_status classification (OK / DRAFT_ONLY / HAS_SSA)
# --------------------------------------------------------------------------- #
def test_u4_ssa_status_classification(api):
    _, mod = api(
        employees=dict([_emp("E1"), _emp("E2"), _emp("E3")]),
        structures=_ST,
        submitted_ssas={"E2": [{"name": "SSA-2", "from_date": "2025-01-01", "to_date": None}]},
        draft_ssas={"E3": ["SSA-D-3"]},
    )
    res = mod.ssa_status(["E1", "E2", "E3"])
    by_emp = {i["employee"]: i for i in res["items"]}

    assert by_emp["E1"]["assignable"] is True and by_emp["E1"]["reason"] == "OK"
    assert by_emp["E2"]["assignable"] is False and by_emp["E2"]["reason"] == "HAS_SSA"
    assert by_emp["E2"]["structure"] == "Lương CB VN" or by_emp["E2"]["structure"] is None
    assert by_emp["E3"]["assignable"] is False and by_emp["E3"]["reason"] == "DRAFT_ONLY"


# --------------------------------------------------------------------------- #
# U5 — idempotent: second bulk run is all skips
# --------------------------------------------------------------------------- #
def test_u5_idempotent_second_run_all_skipped(api):
    stub, mod = api(employees=dict([_emp("E1")]), structures=_ST)
    first = mod.bulk_assign_salary_structure(["E1"], "Lương CB VN")
    second = mod.bulk_assign_salary_structure(["E1"], "Lương CB VN")

    assert first["assigned"] == ["E1"]
    assert second["assigned"] == []
    assert second["skipped"][0]["employee"] == "E1"
    assert len(stub.created_ssas) == 1  # no duplicate SSA


# --------------------------------------------------------------------------- #
# U6 — fixed_allowance_count counts only fixed (non-formula) earnings
# --------------------------------------------------------------------------- #
def test_u6_fixed_allowance_count(api):
    stub, mod = api(employees=dict([_emp("E1")]), structures=_ST)
    res = mod.bulk_assign_salary_structure(["E1"], "E2E ST Allowance")

    assert res["assigned"] == ["E1"]
    assert res["fixed_allowance_count"] == 2  # 2 fixed, 1 formula ignored


# --------------------------------------------------------------------------- #
# U7 — unexpected per-employee error → failed bucket + log_error, others OK
# --------------------------------------------------------------------------- #
def test_u7_system_error_failed_bucket(api):
    stub, mod = api(
        employees=dict([_emp("E1"), _emp("E2")]),
        structures=_ST,
        raise_on_create_for={"E2"},
    )
    res = mod.bulk_assign_salary_structure(["E1", "E2"], "Lương CB VN")

    assert res["assigned"] == ["E1"]
    assert res["failed"][0]["employee"] == "E2"
    assert "simulated insert failure" in res["failed"][0]["reason"]
    assert any("bulk assign SSA failed" in e for e in stub.log_errors)
