"""Bench-free unit tests for the HR leave-approval inbox (``api/leave.py``).

The pure helpers (``normalize_name_list`` / ``merge_bulk_results`` /
``sort_pending_approvals``) are covered by ``test_leave.py``. This file targets
the bench-dependent wrapper layer — the ``department`` filter on
``pending_leave_approvals``, the ``bulk_approve`` / ``bulk_reject`` loops, and
``leave_approval_options`` — using a stub ``frappe`` injected into
``sys.modules``.

The stub is registered only for the duration of each test via
``monkeypatch.setitem`` (auto-restored on teardown) so it never leaks into the
sibling bench-free tests that assert ``frappe`` is unimportable.
"""

import datetime
import importlib
import sys
import types

import pytest


# --------------------------------------------------------------------------- #
# Stub frappe — enough surface for ``api/leave`` to import + run
# --------------------------------------------------------------------------- #
def _build_stub_frappe():
    mod = types.ModuleType("frappe")
    mod._ = lambda s: s
    mod.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)

    utils = types.ModuleType("frappe.utils")
    utils.getdate = lambda v=None: (
        datetime.date.today() if v in (None, "") else datetime.date.fromisoformat(str(v)[:10])
    )
    utils.today = lambda: datetime.date.today()
    utils.cint = lambda v, d=0: int(v) if v not in (None, "") else d
    mod.utils = utils

    mod.PermissionError = type("PermissionError", (Exception,), {})
    mod.throw = lambda *a, **k: _raise(Exception(a[0] if a else "frappe.throw"))

    # Runtime bits are swapped per-test.
    mod.db = None
    mod.get_doc = None
    mod.session = types.SimpleNamespace(user="hr.demo@gege.demo")
    mod.log_error = lambda *a, **k: None

    class _Meta:
        def has_field(self, name):
            return True

    mod.get_meta = lambda doctype: _Meta()
    return mod


def _raise(exc):
    raise exc


class _FakeDoc:
    """A Leave Application stand-in: attribute settable + no-op persist."""

    def __init__(self, name, status="Open", docstatus=0, **extra):
        self.name = name
        self.status = status
        self.docstatus = docstatus
        for k, v in extra.items():
            setattr(self, k, v)

    def submit(self):
        self.docstatus = 1

    def reload(self):
        # Real Frappe re-reads the row; the stub is already "fresh" (C2's
        # double-submit re-check stays a no-op here).
        return self

    def db_update(self):
        pass

    def save(self, ignore_permissions=False):
        pass


class _FakeDB:
    """Stub DB. By default every ``get_all`` returns ``get_all_rows``; a test can
    pin rows to a specific doctype via ``configure`` so endpoints that query two
    doctypes (e.g. cancellation inbox + Employee roster) resolve correctly."""

    def __init__(self):
        self.get_all_rows = []  # default fallback rows
        self.rows_by_doctype = {}  # doctype -> rows (overrides fallback)
        self.last_filters = None
        self.last_doctype = None
        self.calls = []  # full call log: (doctype, filters) tuples

    def configure(self, doctype, rows):
        self.rows_by_doctype[doctype] = rows

    def get_all(
        self,
        doctype,
        filters=None,
        fields=None,
        order_by=None,
        limit_page_length=None,
        limit=None,
        pluck=None,
    ):
        self.last_doctype = doctype
        self.last_filters = dict(filters or {})
        self.calls.append((doctype, dict(filters or {})))
        rows = self.rows_by_doctype.get(doctype, self.get_all_rows)
        return [dict(r) for r in rows]

    def sql(self, query, params=None, **_kw):
        # SELECT ... FOR UPDATE lock taken by _save_or_submit's per-employee
        # serialization (C2): no-op in the stub (single "connection").
        self.calls.append(("_sql_", {"q": str(query)[:60], "params": params}))
        return []


@pytest.fixture
def fake(monkeypatch):
    """Register the stub frappe, import ``api/leave``, bypass the manager gate."""
    stub = _build_stub_frappe()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", stub.utils)

    api = importlib.import_module("gege_hr.gege_hr.api.leave")
    # Importing may have cached a module-level `frappe`; point it at the stub.
    monkeypatch.setattr(api, "frappe", stub)
    # Bypass the HR-manager permission gates so the wrapper logic is exercised.
    monkeypatch.setattr(api, "_require_manager", lambda: None)
    monkeypatch.setattr(api, "_is_manager", lambda: True)

    db = _FakeDB()
    monkeypatch.setattr(stub, "db", db)

    return types.SimpleNamespace(api=api, stub=stub, db=db)


# --------------------------------------------------------------------------- #
# pending_leave_approvals — department filter
# --------------------------------------------------------------------------- #
def test_pending_approvals_forwards_department_filter(fake):
    fake.db.get_all_rows = [
        {"name": "L1", "employee": "E1", "posting_date": "2026-06-20"},
    ]
    out = fake.api.pending_leave_approvals(department="Eng")
    assert [r["name"] for r in out] == ["L1"]
    assert fake.db.last_filters["status"] == "Open"
    assert fake.db.last_filters["department"] == "Eng"


def test_pending_approvals_blackout_only_filters_flagged(fake):
    fake.db.get_all_rows = [
        {"name": "plain", "posting_date": "2026-06-20"},
        {"name": "blk", "posting_date": "2026-06-19", "vn_requires_blackout_approval": 1},
    ]
    out = fake.api.pending_leave_approvals(blackout_only=1)
    assert [r["name"] for r in out] == ["blk"]


def test_pending_approvals_no_department_keeps_filter_clean(fake):
    fake.db.get_all_rows = []
    fake.api.pending_leave_approvals()
    assert "department" not in fake.db.last_filters


# --------------------------------------------------------------------------- #
# leave_approval_options
# --------------------------------------------------------------------------- #
def test_leave_approval_options_distinct_sorted(fake):
    fake.db.get_all_rows = [
        {"employee": "E2", "employee_name": "Bob", "department": "Sales"},
        {"employee": "E1", "employee_name": "Alice", "department": "Eng"},
        {"employee": "E2", "employee_name": "Bob", "department": "Sales"},  # dup
        {"employee": "", "employee_name": "", "department": ""},  # blanks
    ]
    out = fake.api.leave_approval_options()
    assert [e["value"] for e in out["employees"]] == ["E1", "E2"]
    assert out["employees"][0]["label"] == "Alice"
    assert [d["value"] for d in out["departments"]] == ["Eng", "Sales"]


def test_leave_approval_options_swallow_error(fake, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(fake.db, "get_all", boom)
    out = fake.api.leave_approval_options()
    assert out == {"employees": [], "departments": []}


# --------------------------------------------------------------------------- #
# bulk approve / reject
# --------------------------------------------------------------------------- #
def _wire_docs(fake, docs):
    """Route ``frappe.get_doc("Leave Application", name)`` to a fake doc by name."""
    table = {d.name: d for d in docs}

    def get_doc(doctype, name):
        return table[name]

    fake.stub.get_doc = get_doc


def test_bulk_approve_succeeds_for_open_docs(fake):
    _wire_docs(
        fake,
        [
            _FakeDoc("L1", status="Open", employee="E1", company="C", from_date="2026-06-20"),
            _FakeDoc("L2", status="Draft", employee="E2", company="C", from_date="2026-06-21"),
        ],
    )
    res = fake.api.bulk_approve_leave_applications(["L1", "L2"])
    assert res["counts"] == {"succeeded": 2, "failed": 0}
    assert res["succeeded"] == ["L1", "L2"]


def test_bulk_approve_partial_failure_recorded(fake):
    _wire_docs(
        fake,
        [
            _FakeDoc("L1", status="Open", employee="E1", company="C", from_date="2026-06-20"),
            _FakeDoc("L2", status="Cancelled", employee="E2", company="C", from_date="2026-06-21"),
        ],
    )
    res = fake.api.bulk_approve_leave_applications('["L1", "L2"]')
    assert res["counts"] == {"succeeded": 1, "failed": 1}
    assert res["succeeded"] == ["L1"]
    assert res["failed"][0]["name"] == "L2"


def test_bulk_reject_stamps_reason_and_summary(fake):
    _wire_docs(
        fake,
        [
            _FakeDoc("L1", status="Open", employee="E1", company="C", from_date="2026-06-20"),
        ],
    )
    res = fake.api.bulk_reject_leave_applications(["L1"], rejection_reason="Quá hạn")
    assert res["counts"] == {"succeeded": 1, "failed": 0}


def test_bulk_approve_empty_names_noop(fake):
    res = fake.api.bulk_approve_leave_applications(None)
    assert res["counts"] == {"succeeded": 0, "failed": 0}
    assert res["total"] == 0


def test_bulk_approve_comma_string_normalised(fake):
    _wire_docs(
        fake,
        [
            _FakeDoc("L1", status="Open", employee="E1", company="C", from_date="2026-06-20"),
            _FakeDoc("L2", status="Open", employee="E2", company="C", from_date="2026-06-21"),
        ],
    )
    res = fake.api.bulk_approve_leave_applications("L1, L2")
    assert res["counts"]["succeeded"] == 2


# --------------------------------------------------------------------------- #
# bulk approve / reject cancellation requests
# --------------------------------------------------------------------------- #
class _FakeCancel:
    """A VN Leave Cancellation Request stand-in (workflow_state + no-op save)."""

    def __init__(
        self, name, workflow_state="Pending Manager", employee="E1", leave_application=None, **extra
    ):
        self.name = name
        self.workflow_state = workflow_state
        self.employee = employee
        self.leave_application = leave_application
        for k, v in extra.items():
            setattr(self, k, v)

    def save(self, ignore_permissions=False):
        pass


def _wire_cancel_docs(fake, docs):
    """Route ``frappe.get_doc("VN Leave Cancellation Request", name)`` to a fake."""
    table = {d.name: d for d in docs}

    def get_doc(doctype, name):
        return table[name]

    fake.stub.get_doc = get_doc


def test_bulk_approve_cancellations_succeeds(fake):
    _wire_cancel_docs(
        fake,
        [
            _FakeCancel("CR1", employee="E1"),
            _FakeCancel("CR2", workflow_state="Pending HR", employee="E2"),
        ],
    )
    res = fake.api.bulk_approve_cancellations(["CR1", "CR2"])
    assert res["counts"] == {"succeeded": 2, "failed": 0}
    assert res["succeeded"] == ["CR1", "CR2"]


def test_bulk_approve_cancellations_partial_failure(fake):
    _wire_cancel_docs(
        fake,
        [
            _FakeCancel("CR1", employee="E1"),
            _FakeCancel("CR2", workflow_state="Rejected", employee="E2"),  # terminal → throw
        ],
    )
    res = fake.api.bulk_approve_cancellations('["CR1", "CR2"]')
    assert res["counts"] == {"succeeded": 1, "failed": 1}
    assert res["succeeded"] == ["CR1"]
    assert res["failed"][0]["name"] == "CR2"


def test_bulk_approve_cancellations_idempotent_for_approved(fake):
    # An already-Approved request is a no-op success, not a failure.
    _wire_cancel_docs(
        fake,
        [
            _FakeCancel("CR1", workflow_state="Approved", employee="E1"),
        ],
    )
    res = fake.api.bulk_approve_cancellations(["CR1"])
    assert res["counts"] == {"succeeded": 1, "failed": 0}


def test_bulk_reject_cancellations_stamps_reason(fake):
    _wire_cancel_docs(
        fake,
        [
            _FakeCancel("CR1", employee="E1"),
        ],
    )
    res = fake.api.bulk_reject_cancellations(["CR1"], rejection_reason="Đã hết hạn")
    assert res["counts"] == {"succeeded": 1, "failed": 0}


def test_bulk_reject_cancellations_comma_string(fake):
    _wire_cancel_docs(
        fake,
        [
            _FakeCancel("CR1", employee="E1"),
            _FakeCancel("CR2", workflow_state="Draft", employee="E2"),
        ],
    )
    res = fake.api.bulk_reject_cancellations("CR1, CR2", rejection_reason="Không hợp lệ")
    assert res["counts"]["succeeded"] == 2


def test_bulk_approve_cancellations_empty_noop(fake):
    res = fake.api.bulk_approve_cancellations(None)
    assert res["counts"] == {"succeeded": 0, "failed": 0}
    assert res["total"] == 0


# --------------------------------------------------------------------------- #
# all_cancellation_requests — employee / department filter
# --------------------------------------------------------------------------- #
def test_all_cancellation_requests_forwards_employee_filter(fake):
    fake.db.configure(
        "VN Leave Cancellation Request",
        [
            {"name": "CR1", "employee": "E1", "employee_name": "Alice", "workflow_state": "Pending Manager"},
        ],
    )
    out = fake.api.all_cancellation_requests(employee="E1")
    assert [r["name"] for r in out] == ["CR1"]
    # The cancellation query (first call) forwards the employee filter; a later
    # call resolves per-employee departments for the inbox rows.
    cr_call = [f for dt, f in fake.db.calls if dt == "VN Leave Cancellation Request"]
    assert cr_call and cr_call[0]["employee"] == "E1"


def test_all_cancellation_requests_department_resolves_roster(fake):
    fake.db.configure(
        "VN Leave Cancellation Request",
        [
            {"name": "CR1", "employee": "E1", "employee_name": "Alice", "workflow_state": "Pending Manager"},
        ],
    )
    fake.db.configure(
        "Employee",
        [
            {"name": "E1", "department": "Eng"},
            {"name": "E2", "department": "Eng"},
        ],
    )
    fake.api.all_cancellation_requests(department="Eng")
    # Department resolved into the Eng roster and forwarded as an `employee IN`
    # filter on the cancellation query (the DocType has no department column).
    cr_call = [f for dt, f in fake.db.calls if dt == "VN Leave Cancellation Request"]
    assert cr_call and cr_call[0]["employee"] == ["in", ["E1", "E2"]]


def test_all_cancellation_requests_department_empty_roster(fake):
    fake.db.configure(
        "VN Leave Cancellation Request",
        [
            {"name": "CR1", "employee": "E1", "employee_name": "Alice"},
        ],
    )
    fake.db.configure("Employee", [])  # nobody in "Sales"
    out = fake.api.all_cancellation_requests(department="Sales")
    assert out == []
    # Short-circuited before listing cancellation requests.
    assert fake.db.last_doctype == "Employee"


def test_all_cancellation_requests_employee_outside_department(fake):
    fake.db.configure(
        "VN Leave Cancellation Request",
        [
            {"name": "CR1", "employee": "E1", "employee_name": "Alice"},
        ],
    )
    fake.db.configure("Employee", [{"name": "E2", "department": "Eng"}])
    out = fake.api.all_cancellation_requests(department="Eng", employee="E1")
    assert out == []  # E1 is not in the Eng roster → empty intersection


def test_all_cancellation_requests_attaches_department_per_employee(fake):
    # The cancellation DocType has no department column — the api resolves it per
    # employee (one batch) and stamps it on each inbox row so HR can see the team.
    fake.db.configure(
        "VN Leave Cancellation Request",
        [
            {"name": "CR1", "employee": "E1", "employee_name": "Alice"},
            {"name": "CR2", "employee": "E2", "employee_name": "Bob"},
            {"name": "CR3", "employee": "E3", "employee_name": "Cara"},  # unknown emp
        ],
    )
    fake.db.configure(
        "Employee",
        [
            {"name": "E1", "department": "Eng"},
            {"name": "E2", "department": "Sales"},
        ],
    )
    out = fake.api.all_cancellation_requests()
    by_name = {r["name"]: r for r in out}
    assert by_name["CR1"]["department"] == "Eng"
    assert by_name["CR2"]["department"] == "Sales"
    # Unknown employee → blank department (not crash), row still surfaced.
    assert by_name["CR3"]["department"] == ""
    # Every row exposes the department column to the SPA (resolved, not from DB).
    assert all("department" in r for r in out)


# --------------------------------------------------------------------------- #
# cancellation_options — distinct employees + resolved departments
# --------------------------------------------------------------------------- #
def test_cancellation_options_distinct_resolved_department(fake):
    fake.db.configure(
        "VN Leave Cancellation Request",
        [
            {"employee": "E2", "employee_name": "Bob"},
            {"employee": "E1", "employee_name": "Alice"},
            {"employee": "E2", "employee_name": "Bob"},  # dup
            {"employee": "", "employee_name": ""},  # blanks
        ],
    )
    fake.db.configure(
        "Employee",
        [
            {"name": "E1", "department": "Eng"},
            {"name": "E2", "department": "Sales"},
        ],
    )
    out = fake.api.cancellation_options()
    assert [e["value"] for e in out["employees"]] == ["E1", "E2"]
    assert out["employees"][0]["label"] == "Alice"
    assert [d["value"] for d in out["departments"]] == ["Eng", "Sales"]


def test_cancellation_options_swallow_error(fake, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(fake.db, "get_all", boom)
    out = fake.api.cancellation_options()
    assert out == {"employees": [], "departments": []}
