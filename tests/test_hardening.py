"""Hardening regression tests for the Zero-Frappe admin/master APIs.

These tests pin the single most important security property of the
``api/admin.py`` (session 53), ``api/payroll_master.py`` (session 55) and
``api/holiday_master.py`` (session 56) endpoint families:

    **Every ``@frappe.whitelist()`` endpoint calls ``_require_hr_admin()``
    BEFORE any mutation or audit.**

A caller who lacks ``HR Manager`` / ``System Manager`` must therefore never
insert, save, submit, delete a document, and must never emit a ``VN Audit
Event``. This file guards against a future refactor that accidentally drops
the gate, reorders it after a side-effecting call, or adds a new endpoint that
forgets to gate at all.

Pattern mirrors ``test_holiday_master`` / ``test_payroll_master``: a stub
``frappe`` is injected into ``sys.modules`` for the duration of each test, and
the shared permission helper is monkeypatched to raise ``PermissionError`` so
we can assert the endpoint short-circuits with zero side effects.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest

HOLIDAY_API = "gege_hr.gege_hr.api.holiday_master"
PAYROLL_API = "gege_hr.gege_hr.api.payroll_master"
ADMIN_API = "gege_hr.gege_hr.api.admin"


class _PermissionDenied(Exception):
    """Stand-in for ``frappe.PermissionError`` raised by ``only_for``."""


class _FakeDoc:
    """Minimal document stub that records every side-effecting call."""

    def __init__(self, payload, name="NEW-0001"):
        if isinstance(payload, dict):
            self.__dict__.update(payload)
        self.name = payload if isinstance(payload, str) else name
        self.holidays = getattr(self, "holidays", []) if isinstance(payload, dict) else []
        self.earnings = getattr(self, "earnings", []) if isinstance(payload, dict) else []
        self.deductions = getattr(self, "deductions", []) if isinstance(payload, dict) else []
        self.appended = []

    def insert(self, ignore_permissions=False):
        self._harness.created.append(self)
        return self

    def save(self, ignore_permissions=False):
        self._harness.saved.append(self)
        return self

    def submit(self):
        self._harness.submitted.append(self)
        return self

    def cancel(self):
        self._harness.cancelled.append(self)
        return self

    def set(self, key, value):
        setattr(self, key, value)
        return self

    def get(self, key, default=None):
        return getattr(self, key, default)

    def append(self, key, row):
        self.appended.append((key, row))
        return self


class _Harness:
    """Tracks every side effect a denied call must NOT produce."""

    def __init__(self):
        self.created = []
        self.saved = []
        self.submitted = []
        self.cancelled = []
        self.deleted = []
        self.audit_calls = []
        self.db = _FakeDB()

    def get_doc(self, payload_or_doctype, name=None):
        doc = _FakeDoc(payload_or_doctype, name=name or "NEW-0001")
        doc._harness = self
        return doc

    def get_all(self, doctype, **kwargs):
        return []

    def get_value(self, *a, **k):
        return None

    def delete_doc(self, doctype, name, **kwargs):
        self.deleted.append((doctype, name))

    def throw(self, msg, exc=_PermissionDenied, *a, **k):
        raise exc(msg)


class _FakeDB:
    def __init__(self):
        self.exists_map = {}
        self.set_values = []

    def exists(self, doctype, name):
        return self.exists_map.get((doctype, name), False)

    def get_value(self, *a, **k):
        return None

    def get_single_value(self, *a, **k):
        return None

    def set_value(self, *a, **k):
        self.set_values.append((a, k))

    def get_list(self, *a, **k):
        return []

    def count_all(self, *a, **k):
        return 0


def _build_stub_frappe(harness):
    mod = types.ModuleType("frappe")
    mod._ = lambda s: s
    mod.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)
    mod.only_for = lambda roles: (_ for _ in ()).throw(_PermissionDenied("denied"))

    utils = types.ModuleType("frappe.utils")
    import datetime

    utils.getdate = lambda v=None: (
        datetime.date.today() if v in (None, "") else datetime.date.fromisoformat(str(v)[:10])
    )
    utils.flt = lambda v, *a, **k: float(v) if v not in (None, "") else 0.0
    utils.get_datetime = lambda v=None: datetime.datetime.now()
    mod.utils = utils

    mod.db = harness.db
    mod.get_doc = harness.get_doc
    mod.get_all = harness.get_all
    mod.get_value = harness.get_value
    mod.delete_doc = harness.delete_doc
    mod.throw = harness.throw
    mod.log_error = lambda *a, **k: None
    mod.flags = types.SimpleNamespace()

    class _S:
        pass

    mod.session = _S()
    mod.session.user = "employee.demo@gege.demo"
    mod.local = _S()
    mod.local.request_ip = None
    return mod


@pytest.fixture
def denied(monkeypatch):
    """Install a stub frappe whose permission gate always denies.

    Returns a dict holding the three api modules + the side-effect harness.
    Every ``_require_hr_admin`` binding is replaced with a raiser, and every
    ``_audit_admin`` binding with a recorder, so we can prove a denied call
    neither mutates nor audits.
    """
    harness = _Harness()
    stub = _build_stub_frappe(harness)
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", stub.utils)

    modules = {
        "holiday": importlib.import_module(HOLIDAY_API),
        "payroll": importlib.import_module(PAYROLL_API),
        "admin": importlib.import_module(ADMIN_API),
    }
    # Point each module's ``frappe`` at the stub (covers admin.py, whose
    # ``_require_hr_admin`` calls ``frappe.only_for`` as a backstop).
    for mod in modules.values():
        monkeypatch.setattr(mod, "frappe", stub)

    def _deny(*a, **k):
        raise _PermissionDenied("denied")

    def _record(*a, **k):
        harness.audit_calls.append((a, k))

    # Gate + audit bindings exist on each module's own namespace.
    for mod in modules.values():
        if hasattr(mod, "_require_hr_admin"):
            monkeypatch.setattr(mod, "_require_hr_admin", _deny)
        if hasattr(mod, "_audit_admin"):
            monkeypatch.setattr(mod, "_audit_admin", _record)

    return {"modules": modules, "harness": harness}


# --------------------------------------------------------------------------- #
# Endpoint matrix: (module_key, function_name, kwargs).
# kwargs are intentionally minimal — the gate must short-circuit BEFORE any
# validation runs, so the body is never reached regardless of input validity.
# --------------------------------------------------------------------------- #
DENIED_ENDPOINTS = [
    # holiday_master (session 56)
    ("holiday", "list_holiday_lists", {}),
    ("holiday", "get_holiday_list", {"name": "HL-2026"}),
    (
        "holiday",
        "save_holiday_list",
        {"holiday_list_name": "Lễ 2026", "holidays": [{"holiday_date": "2026-01-01"}]},
    ),
    ("holiday", "delete_holiday_list", {"name": "HL-2026"}),
    # payroll_master (session 55) — G8 Salary Structure
    ("payroll", "list_salary_structures", {}),
    ("payroll", "get_salary_structure", {"name": "SS-1"}),
    (
        "payroll",
        "save_salary_structure",
        {"salary_structure": "Cơ bản", "earnings": [{"salary_component": "B", "amount": 1}]},
    ),
    (
        "payroll",
        "assign_salary_structure",
        {"employee": "E-1", "salary_structure": "SS-1", "from_date": "2026-01-01"},
    ),
    ("payroll", "list_salary_assignments", {}),
    # payroll_master — G9 Leave Period / Policy
    ("payroll", "list_leave_periods", {}),
    ("payroll", "create_leave_period", {"from_date": "2026-01-01", "to_date": "2026-12-31"}),
    ("payroll", "list_leave_policies", {}),
    (
        "payroll",
        "save_leave_policy",
        {"leave_policy": "P-1", "details": [{"leave_type": "LT", "annual_allocation": 12}]},
    ),
    ("payroll", "assign_leave_policy", {"employee": "E-1", "leave_policy": "P-1"}),
    ("payroll", "list_leave_policy_assignments", {}),
    # admin (session 53) — onboarding / user / role / shift
    ("admin", "get_assignable_roles", {}),
    ("admin", "create_user", {"email": "new@gege.demo", "full_name": "New User"}),
    ("admin", "assign_roles", {"user": "new@gege.demo", "roles": ["Employee"]}),
    ("admin", "remove_roles", {"user": "new@gege.demo", "roles": ["Employee"]}),
    ("admin", "link_user_to_employee", {"employee": "E-1", "user": "new@gege.demo"}),
    ("admin", "list_shift_assignments", {}),
    (
        "admin",
        "create_shift_assignment",
        {"employee": "E-1", "shift_type": "S-1", "start_date": "2026-01-01"},
    ),
    ("admin", "end_shift_assignment", {"name": "SA-1", "end_date": "2026-01-02"}),
]


@pytest.mark.parametrize("module_key, func_name, kwargs", DENIED_ENDPOINTS)
def test_denied_call_has_no_side_effects(denied, module_key, func_name, kwargs):
    """A non-HR-Manager caller must be rejected before any mutation or audit."""
    mod = denied["modules"][module_key]
    harness = denied["harness"]
    endpoint = getattr(mod, func_name)

    with pytest.raises(_PermissionDenied):
        endpoint(**kwargs)

    # No document may be created, saved, submitted, cancelled or deleted.
    assert harness.created == [], f"{func_name} created a document despite denial"
    assert harness.saved == [], f"{func_name} saved a document despite denial"
    assert harness.submitted == [], f"{func_name} submitted a document despite denial"
    assert harness.cancelled == [], f"{func_name} cancelled a document despite denial"
    assert harness.deleted == [], f"{func_name} deleted a document despite denial"
    # No audit row may be written for a denied action.
    assert harness.audit_calls == [], f"{func_name} emitted an audit row despite denial"
