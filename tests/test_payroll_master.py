"""Bench-free unit tests for the Payroll/Leave Master API (``api/payroll_master.py``).

Targets the bench-dependent wrapper layer (G8 Salary Structure + G9 Leave
Period/Policy/Assignment) using a stub ``frappe`` injected into ``sys.modules``
for the duration of each test only, exactly like ``test_audit_api``.

The permission/audit helpers imported from ``api/admin`` are stubbed out so the
tests stay focused on payroll_master's own validation, coercion and branching
logic rather than re-testing admin.py.
"""

import datetime
import importlib
import sys
import types

import pytest


# --------------------------------------------------------------------------- #
# Stub frappe — enough surface for ``api/payroll_master`` + ``api/admin`` to
# import (module-level decorators + ``from frappe import _``) and run.
# --------------------------------------------------------------------------- #
class _FrappeError(Exception):
    """Stand-in for frappe.exceptions.ValidationError used by frappe.throw."""


def _build_stub_frappe():
    mod = types.ModuleType("frappe")
    mod._ = lambda s: s
    mod.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)

    utils = types.ModuleType("frappe.utils")
    utils.getdate = lambda v=None: (
        datetime.date.today() if v in (None, "") else datetime.date.fromisoformat(str(v)[:10])
    )
    utils.flt = lambda v, *a, **k: float(v) if v not in (None, "") else 0.0
    mod.utils = utils

    # Runtime bits swapped per-test; inert defaults here.
    mod.db = None
    mod.get_doc = None
    mod.get_all = lambda *a, **k: []
    mod.throw = lambda msg, exc=_FrappeError, *args, **kwargs: (_ for _ in ()).throw(exc(msg))
    mod.log_error = lambda *a, **k: None

    class _S:
        pass

    mod.session = _S()
    mod.session.user = "hr.demo@gege.demo"
    mod.local = _S()
    mod.local.request_ip = None
    return mod


# --------------------------------------------------------------------------- #
# Fake frappe harness (per-test)
# --------------------------------------------------------------------------- #
class _FakeDoc:
    """A minimal Frappe document stub supporting insert/submit/save/set."""

    def __init__(self, payload, name=None):
        self.__dict__.update(payload)
        self.name = payload.get("name", name or "NEW-0001")
        self.earnings = payload.get("earnings", [])
        self.deductions = payload.get("deductions", [])
        self.leave_policy_details = payload.get("leave_policy_details", [])
        self.inserted = False
        self.submitted = False
        self.saved = False

    def insert(self, ignore_permissions=False):
        self.inserted = True
        return self

    def submit(self):
        self.submitted = True
        return self

    def save(self, ignore_permissions=False):
        self.saved = True
        return self

    def set(self, key, value):
        setattr(self, key, value)
        return self

    def get(self, key, default=None):
        return getattr(self, key, default)


class _FakeDB:
    def __init__(self):
        self.exists_map = {}  # {(doctype, name): bool}
        self.table_exists_flag = True

    def exists(self, doctype, name):
        return self.exists_map.get((doctype, name), True)

    def table_exists(self, doctype):
        return self.table_exists_flag


class _FakeFrappe:
    def __init__(self):
        self.db = _FakeDB()
        self.docs_created = []
        self._next_id = 1
        self._existing = {}  # {(doctype,name): _FakeDoc} for get_doc(name)

    def get_doc(self, payload_or_doctype, name=None):
        if name is not None:
            return self._existing[(payload_or_doctype, name)]
        doc = _FakeDoc(payload_or_doctype, name=f"NEW-{self._next_id:04d}")
        self._next_id += 1
        self.docs_created.append(doc)
        return doc

    def get_all(self, doctype, **kwargs):
        return self._all_rows.get(doctype, [])

    def throw(self, msg, exc=_FrappeError, *a, **k):
        raise exc(msg)


@pytest.fixture
def fake(monkeypatch):
    """Register the stub frappe, import payroll_master, wire a fake + stub admin helpers."""
    stub = _build_stub_frappe()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", stub.utils)

    api = importlib.import_module("gege_hr.gege_hr.api.payroll_master")
    monkeypatch.setattr(api, "frappe", stub)

    harness = _FakeFrappe()
    harness._all_rows = {}
    monkeypatch.setattr(stub, "db", harness.db)
    monkeypatch.setattr(stub, "get_doc", harness.get_doc)
    monkeypatch.setattr(stub, "get_all", harness.get_all)
    monkeypatch.setattr(stub, "throw", harness.throw)

    # Stub the admin helpers so payroll_master logic is isolated.
    monkeypatch.setattr(api, "_require_hr_admin", lambda *a, **k: None)
    audit_calls = []
    monkeypatch.setattr(api, "_audit_admin", lambda *a, **k: audit_calls.append((a, k)))
    monkeypatch.setattr(api, "_default_company", lambda: "GEGE")
    monkeypatch.setattr(api, "_company_for_employee", lambda e: "GEGE")

    harness.api = api
    harness.stub = stub
    harness.audit_calls = audit_calls
    harness.set_rows = lambda doctype, rows: harness._all_rows.__setitem__(doctype, rows)
    return harness


# --------------------------------------------------------------------------- #
# _coerce_bool / _clean_salary_details helpers
# --------------------------------------------------------------------------- #
def test_coerce_bool_accepts_js_and_python_truthy(fake):
    api = fake.api
    assert api._coerce_bool(True) is True
    assert api._coerce_bool("true") is True
    assert api._coerce_bool("True") is True
    assert api._coerce_bool(1) is True
    assert api._coerce_bool("1") is True
    assert api._coerce_bool(False) is False
    assert api._coerce_bool(0) is False
    assert api._coerce_bool("no") is False
    assert api._coerce_bool(None) is False


def test_clean_salary_details_drops_blank_rows_and_meta(fake):
    api = fake.api
    rows = [
        {"salary_component": "Basic", "amount": "1000", "amount_based_on_formula": "true"},
        {"salary_component": "   ", "amount": 50},  # blank → dropped
        {"name": "row-meta", "salary_component": "Bonus", "amount": 200},  # meta kept-but-filtered
    ]
    out = api._clean_salary_details(rows)
    assert len(out) == 2
    assert out[0]["salary_component"] == "Basic"
    assert out[0]["amount"] == 1000.0
    assert out[0]["amount_based_on_formula"] is True
    # meta key not in allowed tuple → not present
    assert "name" not in out[1]
    assert out[1]["salary_component"] == "Bonus"


def test_clean_salary_details_casts_amount_and_coerces_bools(fake):
    api = fake.api
    out = api._clean_salary_details(
        [
            {"salary_component": "House Rent", "amount": "abc", "is_tax_applicable": "true"},
        ]
    )
    assert out[0]["amount"] == 0.0  # bad float → 0.0


# --------------------------------------------------------------------------- #
# save_salary_structure
# --------------------------------------------------------------------------- #
def test_save_salary_structure_requires_label(fake):
    with pytest.raises(_FrappeError):
        fake.api.save_salary_structure(
            salary_structure="", company="GEGE", earnings=[{"salary_component": "Basic"}]
        )


def test_save_salary_structure_requires_at_least_one_earning(fake):
    with pytest.raises(_FrappeError):
        fake.api.save_salary_structure(
            salary_structure="Lương CB", company="GEGE", earnings=[], deductions=[]
        )


def test_save_salary_structure_creates_new_and_audits(fake):
    res = fake.api.save_salary_structure(
        salary_structure="Lương CB",
        company="GEGE",
        earnings=[{"salary_component": "Basic", "amount": "5000000"}],
        deductions=[{"salary_component": "BHXH", "amount": 500000}],
    )
    assert res["name"].startswith("NEW-")
    doc = fake.docs_created[-1]
    assert doc.inserted is True
    assert len(doc.earnings) == 1
    assert doc.earnings[0]["amount"] == 5000000.0
    assert len(doc.deductions) == 1
    # audit emitted
    assert len(fake.audit_calls) == 1
    _args, kwargs = fake.audit_calls[0]
    assert kwargs["reference_doctype"] == "Salary Structure"
    assert kwargs["new_value"]["earnings"] == 1


def test_save_salary_structure_updates_existing(fake):
    existing = _FakeDoc({"doctype": "Salary Structure", "earnings": [], "deductions": []}, name="LUONG-001")
    fake._existing[("Salary Structure", "LUONG-001")] = existing
    res = fake.api.save_salary_structure(
        name="LUONG-001",
        salary_structure="Lương CB",
        company="GEGE",
        earnings=[{"salary_component": "Basic", "amount": 6000000}],
    )
    assert res["name"] == "LUONG-001"
    assert existing.saved is True
    assert len(existing.earnings) == 1


def test_save_salary_structure_update_unknown_raises(fake):
    # exists() returns False for unknown → throw
    fake.db.exists_map = {("Salary Structure", "NOPE"): False}
    with pytest.raises(_FrappeError):
        fake.api.save_salary_structure(
            name="NOPE",
            salary_structure="X",
            company="GEGE",
            earnings=[{"salary_component": "Basic"}],
        )


# --------------------------------------------------------------------------- #
# assign_salary_structure + overlap detection
# --------------------------------------------------------------------------- #
def test_assign_salary_structure_requires_existing_employee(fake):
    fake.db.exists_map = {("Employee", "EMP-1"): False}
    with pytest.raises(_FrappeError):
        fake.api.assign_salary_structure(employee="EMP-1", salary_structure="S", from_date="2026-01-01")


def test_assign_salary_structure_inserts_and_submits(fake):
    res = fake.api.assign_salary_structure(
        employee="EMP-1",
        salary_structure="S1",
        from_date="2026-01-01",
        base=5000000,
    )
    doc = fake.docs_created[-1]
    assert doc.submitted is True
    assert doc.employee == "EMP-1"
    assert doc.base == 5000000.0
    assert res["name"].startswith("NEW-")
    _args, kwargs = fake.audit_calls[0]
    assert kwargs["employee"] == "EMP-1"


def test_ensure_no_overlapping_assignment_raises_on_overlap(fake):
    fake.set_rows(
        "Salary Structure Assignment",
        [
            {"name": "ASSIGN-OLD", "from_date": "2026-01-01", "to_date": "2026-12-31"},
        ],
    )
    start = datetime.date(2026, 6, 1)
    with pytest.raises(_FrappeError):
        fake.api._ensure_no_overlapping_assignment("EMP-1", start, None)


def test_ensure_no_overlapping_assignment_passes_when_disjoint(fake):
    fake.set_rows(
        "Salary Structure Assignment",
        [
            {"name": "OLD", "from_date": "2026-01-01", "to_date": "2026-03-31"},
        ],
    )
    # new window starts after the old one ends → no overlap
    fake.api._ensure_no_overlapping_assignment("EMP-1", datetime.date(2026, 5, 1), datetime.date(2026, 6, 1))


# --------------------------------------------------------------------------- #
# create_leave_period
# --------------------------------------------------------------------------- #
def test_create_leave_period_requires_dates(fake):
    with pytest.raises(_FrappeError):
        fake.api.create_leave_period(from_date="", to_date="2026-12-31")


def test_create_leave_period_rejects_end_before_start(fake):
    with pytest.raises(_FrappeError):
        fake.api.create_leave_period(from_date="2026-12-31", to_date="2026-01-01")


def test_create_leave_period_creates_and_audits(fake):
    fake.api.create_leave_period(from_date="2026-01-01", to_date="2026-12-31", name="Kỳ 2026")
    doc = fake.docs_created[-1]
    assert doc.from_date == datetime.date(2026, 1, 1)
    assert doc.to_date == datetime.date(2026, 12, 31)
    assert doc.name == "Kỳ 2026"
    _args, kwargs = fake.audit_calls[0]
    assert kwargs["reference_doctype"] == "Leave Period"


# --------------------------------------------------------------------------- #
# save_leave_policy
# --------------------------------------------------------------------------- #
def test_save_leave_policy_requires_label(fake):
    with pytest.raises(_FrappeError):
        fake.api.save_leave_policy(
            leave_policy="", details=[{"leave_type": "Annual", "annual_allocation": 12}]
        )


def test_save_leave_policy_requires_positive_allocation(fake):
    with pytest.raises(_FrappeError):
        fake.api.save_leave_policy(
            leave_policy="P1", details=[{"leave_type": "Annual", "annual_allocation": 0}]
        )


def test_save_leave_policy_drops_zero_and_blank_rows(fake):
    fake.api.save_leave_policy(
        leave_policy="P1",
        details=[
            {"leave_type": "Annual", "annual_allocation": 12},
            {"leave_type": "", "annual_allocation": 5},  # blank → dropped
            {"leave_type": "Sick", "annual_allocation": 0},  # zero → dropped
        ],
    )
    doc = fake.docs_created[-1]
    assert len(doc.leave_policy_details) == 1
    assert doc.leave_policy_details[0]["leave_type"] == "Annual"


def test_save_leave_policy_creates_and_audits(fake):
    res = fake.api.save_leave_policy(
        leave_policy="P1", details=[{"leave_type": "Annual", "annual_allocation": 12}]
    )
    assert res["name"].startswith("NEW-")
    _args, kwargs = fake.audit_calls[0]
    assert kwargs["new_value"]["lines"] == 1


def test_save_leave_policy_updates_existing(fake):
    existing = _FakeDoc({"doctype": "Leave Policy", "leave_policy_details": []}, name="P1")
    fake._existing[("Leave Policy", "P1")] = existing
    res = fake.api.save_leave_policy(
        leave_policy="P1", name="P1", details=[{"leave_type": "Annual", "annual_allocation": 15}]
    )
    assert res["name"] == "P1"
    assert existing.saved is True
    assert len(existing.leave_policy_details) == 1


# --------------------------------------------------------------------------- #
# assign_leave_policy
# --------------------------------------------------------------------------- #
def test_assign_leave_policy_requires_leave_period_when_based_on_period(fake):
    with pytest.raises(_FrappeError):
        fake.api.assign_leave_policy(employee="EMP-1", leave_policy="P1", assignment_based_on="Leave Period")


def test_assign_leave_policy_requires_effective_from_when_based_on_joining(fake):
    with pytest.raises(_FrappeError):
        fake.api.assign_leave_policy(employee="EMP-1", leave_policy="P1", assignment_based_on="Joining Date")


def test_assign_leave_policy_submits_with_leave_period(fake):
    fake.api.assign_leave_policy(
        employee="EMP-1", leave_policy="P1", leave_period="LP-2026", assignment_based_on="Leave Period"
    )
    doc = fake.docs_created[-1]
    assert doc.submitted is True
    assert doc.leave_period == "LP-2026"
    assert doc.assignment_based_on == "Leave Period"
    _args, kwargs = fake.audit_calls[0]
    assert kwargs["employee"] == "EMP-1"


def test_assign_leave_policy_joining_date_branch(fake):
    fake.api.assign_leave_policy(
        employee="EMP-1",
        leave_policy="P1",
        effective_from="2026-01-15",
        effective_to="2026-12-31",
        assignment_based_on="Joining Date",
    )
    doc = fake.docs_created[-1]
    assert doc.submitted is True
    assert doc.effective_from == datetime.date(2026, 1, 15)
    assert doc.effective_to == datetime.date(2026, 12, 31)
    assert "leave_period" not in doc.__dict__ or not getattr(doc, "leave_period", None)


def test_assign_leave_policy_unknown_employee_raises(fake):
    fake.db.exists_map = {("Employee", "NOPE"): False, ("Leave Policy", "P1"): True}
    with pytest.raises(_FrappeError):
        fake.api.assign_leave_policy(employee="NOPE", leave_policy="P1", leave_period="LP-2026")
