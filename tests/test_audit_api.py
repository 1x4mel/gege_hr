"""Bench-free unit tests for the audit event producer (``api/audit.py``).

The pure helpers in ``utils/audit.py`` are covered by ``test_audit.py``. This
file targets the bench-dependent wrapper layer — :func:`record` and the
doc-aware :func:`log` — using a stub ``frappe`` injected into ``sys.modules``.

The stub is registered **only for the duration of each test** via
``monkeypatch.setitem`` (auto-restored on teardown) so it never leaks into the
sibling bench-free tests that assert ``frappe`` is unimportable
(``test_dashboard`` / ``test_device`` / ``test_notification`` /
``test_payroll_mapping``).
"""

import datetime
import importlib
import sys
import types

import pytest


# --------------------------------------------------------------------------- #
# Stub frappe — enough surface for ``api/audit`` to import + run
# --------------------------------------------------------------------------- #
def _build_stub_frappe():
    mod = types.ModuleType("frappe")
    mod._ = lambda s: s
    mod.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)

    utils = types.ModuleType("frappe.utils")
    utils.getdate = lambda v=None: (
        datetime.date.today() if v in (None, "") else datetime.date.fromisoformat(str(v)[:10])
    )
    mod.utils = utils

    # Runtime bits are swapped per-test; inert defaults here.
    mod.db = None
    mod.get_doc = None
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
    def __init__(self, payload):
        self.payload = payload
        self.name = "AUD-0001"
        self.inserted = False

    def insert(self, ignore_permissions=False):
        self.inserted = True
        return self


class _FakeDB:
    def __init__(self):
        self.table_exists_flag = True

    def table_exists(self, doctype):
        return self.table_exists_flag


class _FakeFrappe:
    def __init__(self):
        self.db = _FakeDB()
        self.docs_created = []
        self.last_error = None

    def get_doc(self, payload):
        doc = _FakeDoc(payload)
        self.docs_created.append(doc)
        return doc

    def log_error(self, title=None, message=None):
        self.last_error = (title, message)


@pytest.fixture
def fake(monkeypatch):
    """Register the stub frappe, import ``api/audit`` (cached), wire a fake."""
    stub = _build_stub_frappe()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", stub.utils)

    api_audit = importlib.import_module("gege_hr.gege_hr.api.audit")
    monkeypatch.setattr(api_audit, "frappe", stub)

    harness = _FakeFrappe()
    monkeypatch.setattr(stub, "db", harness.db)
    monkeypatch.setattr(stub, "get_doc", harness.get_doc)
    monkeypatch.setattr(stub, "log_error", harness.log_error)

    harness.api = api_audit
    harness.stub = stub
    return harness


# --------------------------------------------------------------------------- #
# APPROVE_AUDIT_TYPE map
# --------------------------------------------------------------------------- #
def test_approve_audit_type_map_covers_three_request_doctypes(fake):
    m = fake.api.APPROVE_AUDIT_TYPE
    assert m["Overtime Request"] == "OT Approve"
    assert m["Correction Request"] == "Correction Approve"
    assert m["Salary Advance Request"] == "Advance Approve"


# --------------------------------------------------------------------------- #
# log() — doc-aware resolution
# --------------------------------------------------------------------------- #
def test_log_resolves_fields_from_doc(fake):
    name = fake.api.log(
        "Leave Submit",
        doc={
            "doctype": "Leave Application",
            "name": "HR-LAP-0001",
            "company": "Gege Co",
            "employee": "HR-EMP-0001",
            "work_date": "2026-06-22",
        },
        description="status → Approved",
    )
    assert name == "AUD-0001"
    assert len(fake.docs_created) == 1
    payload = fake.docs_created[0].payload
    assert payload["company"] == "Gege Co"
    assert payload["employee"] == "HR-EMP-0001"
    assert payload["reference_doctype"] == "Leave Application"
    assert payload["reference_name"] == "HR-LAP-0001"
    assert payload["audit_type"] == "Leave Submit"
    # actor defaults to the session user
    assert payload["actor"] == "hr.demo@gege.demo"


def test_log_work_date_falls_back_to_posting_date(fake):
    fake.api.log(
        "Advance Submit",
        doc={
            "doctype": "VN Salary Advance Request",
            "name": "SAR-0001",
            "company": "Gege Co",
            "employee": "HR-EMP-0002",
            "posting_date": "2026-06-10",
        },
    )
    payload = fake.docs_created[0].payload
    # posting_date is the fallback when work_date is absent on the doc.
    assert str(payload["work_date"]) == "2026-06-10"


def test_log_explicit_company_overrides_doc(fake):
    fake.api.log(
        "Monthly Lock",
        doc={"doctype": "VN Monthly Attendance Period", "name": "MAP-1", "company": "Doc Co"},
        company="Explicit Co",
    )
    assert fake.docs_created[0].payload["company"] == "Explicit Co"


def test_log_skips_when_no_company(fake):
    name = fake.api.log("Leave Submit", doc={"doctype": "Leave Application", "name": "X"})
    assert name is None
    assert fake.docs_created == []


def test_log_skips_when_no_company_and_no_doc(fake):
    assert fake.api.log("Leave Submit") is None
    assert fake.docs_created == []


def test_log_skips_when_table_missing(fake):
    fake.db.table_exists_flag = False
    name = fake.api.log(
        "Leave Submit",
        doc={"company": "Gege Co", "employee": "HR-EMP-0001"},
    )
    assert name is None
    assert fake.docs_created == []


def test_log_swallows_insert_error(fake, monkeypatch):
    def boom(payload):
        raise RuntimeError("db down")

    monkeypatch.setattr(fake.stub, "get_doc", boom)
    name = fake.api.log(
        "Leave Submit",
        doc={"company": "Gege Co", "employee": "HR-EMP-0001"},
    )
    assert name is None  # best-effort: never re-raises
    assert fake.last_error is not None


# --------------------------------------------------------------------------- #
# record() — actor/IP stamping + payload-validation guard
# --------------------------------------------------------------------------- #
def test_record_defaults_actor_to_session_user(fake):
    name = fake.api.record(
        audit_type="Manual Override",
        company="Gege Co",
        employee="HR-EMP-0001",
    )
    assert name == "AUD-0001"
    payload = fake.docs_created[0].payload
    assert payload["actor"] == "hr.demo@gege.demo"
    # actor_ip falls back to frappe.local.request_ip (None here) → no key set.
    assert payload.get("actor_ip") in (None, "")


def test_record_returns_none_when_company_missing(fake):
    # audit_payload raises ValueError on a blank company; record() swallows it.
    name = fake.api.record(audit_type="Manual Override", company="")
    assert name is None
    assert fake.docs_created == []
