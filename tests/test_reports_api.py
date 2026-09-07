"""Bench-free unit tests for the report engine proxy (``api/reports.py``).

Same harness as ``test_audit_api.py``: a stub ``frappe`` is injected into
``sys.modules`` for the duration of each test (auto-restored), the module is
re-imported fresh, and every frappe call is faked. The engine import inside
``run_report`` (``from frappe.desk.query_report import run``) resolves against
``frappe.desk.query_report`` stubbed here.

Plan reports-desk-free §6.1 — RP1..RP12.
"""

import importlib
import sys
import types

import pytest


# --------------------------------------------------------------------------- #
# Stub frappe
# --------------------------------------------------------------------------- #
class _FrappePermissionError(Exception):
    pass


class _FrappeValidationError(Exception):
    pass


def _build_stub_frappe():
    mod = types.ModuleType("frappe")
    mod._ = lambda s: s
    mod.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)
    mod.PermissionError = _FrappePermissionError
    mod.ValidationError = _FrappeValidationError

    def _throw(msg, exc=_FrappeValidationError):
        raise exc(msg)

    mod.throw = _throw

    class _S:
        pass

    mod.session = _S()
    mod.session.user = "hr.demo@gege.demo"
    # swapped per-test via monkeypatch on the module attribute
    mod.get_roles = lambda user=None: ("HR Manager",)

    db = _S()
    db.exists = lambda doctype, name: True
    mod.db = db
    # get_doc / get_all swapped per-test by the fake below
    mod.get_doc = None
    return mod


def _build_engine_stub(result=None, error=None):
    """Fake ``frappe.desk.query_report.run`` recording its call."""
    qr = types.ModuleType("frappe.desk.query_report")
    calls = []

    def _run(report_name=None, filters=None, **kw):
        calls.append({"report_name": report_name, "filters": filters})
        if error is not None:
            raise error
        return result if result is not None else {"result": [], "columns": []}

    qr.run = _run
    qr.calls = calls
    return qr


class _Role:
    def __init__(self, role):
        self.role = role


class _FilterRow(types.SimpleNamespace):
    pass


class FakeReportDoc:
    """Minimal Report doc: .get() over the payload + roles/filters children."""

    def __init__(
        self,
        name,
        ref_doctype,
        roles=("HR Manager", "HR User"),
        disabled=0,
        report_type="Script Report",
        filters=None,
        add_total_row=0,
    ):
        self.name = name
        self.report_name = name
        self.ref_doctype = ref_doctype
        self.module = "HR"
        self.report_type = report_type
        self.prepared_report = 0
        self.add_total_row = add_total_row
        self.disabled = disabled
        self.roles = [_Role(r) for r in roles]
        self.filters = filters or []

    def get(self, key):
        return getattr(self, key, None)


ALLOWLISTED_DOC = FakeReportDoc(
    "Monthly Attendance Sheet",
    "Attendance",
    filters=[
        {
            "fieldname": "company",
            "label": "Company",
            "fieldtype": "Link",
            "options": "Company",
            "default": None,
            "reqd": 1,
            "depends_on": None,
        },
        {
            "fieldname": "month",
            "label": "Month",
            "fieldtype": "Select",
            "options": "\nJanuary\nFebruary",
            "default": "January",
            "reqd": 0,
            "depends_on": None,
        },
    ],
)


@pytest.fixture
def harness(monkeypatch):
    """Install the stub + engine, import api.reports fresh, return handles."""
    mod = _build_stub_frappe()
    engine = _build_engine_stub()
    monkeypatch.setitem(sys.modules, "frappe", mod)
    monkeypatch.setitem(sys.modules, "frappe.desk.query_report", engine)

    reports = importlib.import_module("gege_hr.gege_hr.api.reports")
    reports = importlib.reload(reports)

    class _H:
        pass

    h = _H()
    h.mod = mod
    h.engine = engine
    h.api = reports
    h.doc = ALLOWLISTED_DOC
    # default: every allowlisted report resolves to a permissive fake doc;
    # the flagship entry keeps its rich fake (filters child table) for meta tests
    h.docs = {"Monthly Attendance Sheet": ALLOWLISTED_DOC}
    h.exists = set(reports.REPORT_ALLOWLIST)

    def _get_doc(doctype, name):
        if doctype != "Report":
            raise AssertionError(f"unexpected get_doc doctype {doctype}")
        # per-name default so every allowlisted entry surfaces under its own name
        return h.docs.get(name) or FakeReportDoc(name, reports.REPORT_ALLOWLIST[name])

    mod.get_doc = _get_doc
    mod.db.exists = lambda doctype, name: name in h.exists
    return h


# --------------------------------------------------------------------------- #
# RP1 — list_reports: HR Manager sees the whole allowlist
# --------------------------------------------------------------------------- #
def test_rp1_list_reports_hr_manager(harness):
    out = harness.api.list_reports()
    names = [r["name"] for r in out]
    assert set(names) == set(harness.api.REPORT_ALLOWLIST)
    assert names == sorted(names)
    sample = next(r for r in out if r["name"] == "Monthly Attendance Sheet")
    assert sample["ref_doctype"] == "Attendance"
    assert sample["report_type"] == "Script Report"


# RP2 — non-HR user is rejected before any report metadata is read
def test_rp2_list_reports_requires_hr(harness, monkeypatch):
    monkeypatch.setattr(harness.mod, "get_roles", lambda user=None: ("Employee",))
    with pytest.raises(_FrappePermissionError, match="chỉ dành cho HR"):
        harness.api.list_reports()


# RP11 — missing/renamed reports are skipped silently, not fatal
def test_rp11_list_reports_skips_missing(harness):
    harness.exists = {"Monthly Attendance Sheet"}
    out = harness.api.list_reports()
    assert [r["name"] for r in out] == ["Monthly Attendance Sheet"]


# roles declared on the report filter the catalog (System-Manager-only report)
def test_rp2b_list_reports_respects_report_roles(harness):
    harness.docs = {
        "Monthly Attendance Sheet": FakeReportDoc(
            "Monthly Attendance Sheet", "Attendance", roles=("System Manager",)
        ),
    }
    out = harness.api.list_reports()
    assert "Monthly Attendance Sheet" not in [r["name"] for r in out]


# --------------------------------------------------------------------------- #
# RP3 — run_report proxies the engine envelope untouched
# --------------------------------------------------------------------------- #
def test_rp3_run_report_passthrough(harness):
    payload = {
        "result": [{"employee": "HR-EMP-0001", "present_days": 20.0}],
        "columns": [{"label": "Mã NV", "fieldname": "employee", "fieldtype": "Link"}],
        "add_total_row": True,
    }
    rec = []
    harness.engine.run = lambda **kw: (rec.append(kw), payload)[1]
    out = harness.api.run_report("Monthly Attendance Sheet", filters={"company": "GeGe Vietnam"})
    assert out is payload
    assert rec[-1]["report_name"] == "Monthly Attendance Sheet"
    assert '"company"' in rec[-1]["filters"]  # dict → JSON string


# filters already a JSON string pass through unchanged (no double encoding)
def test_rp3b_run_report_string_filters(harness):
    rec = []
    harness.engine.run = lambda **kw: (rec.append(kw), {})[1]
    harness.api.run_report("Monthly Attendance Sheet", filters='{"company":"X"}')
    assert rec[-1]["filters"] == '{"company":"X"}'


# --------------------------------------------------------------------------- #
# RP4 — non-allowlisted report is refused (General Ledger)
# --------------------------------------------------------------------------- #
def test_rp4_run_report_rejects_outside_allowlist(harness):
    with pytest.raises(_FrappeValidationError, match="không khả dụng"):
        harness.api.run_report("General Ledger", filters={})


# RP9 — an allowlisted NAME whose ref_doctype drifted is refused
def test_rp9_run_report_ref_doctype_mismatch(harness):
    harness.docs = {
        "Monthly Attendance Sheet": FakeReportDoc("Monthly Attendance Sheet", "Employee"),
    }
    with pytest.raises(_FrappeValidationError, match="không khả dụng"):
        harness.api.run_report("Monthly Attendance Sheet", filters={})


# RP8 — a disabled report is refused
def test_rp8_run_report_disabled(harness):
    harness.docs = {
        "Monthly Attendance Sheet": FakeReportDoc("Monthly Attendance Sheet", "Attendance", disabled=1),
    }
    with pytest.raises(_FrappeValidationError, match="vô hiệu hóa"):
        harness.api.run_report("Monthly Attendance Sheet", filters={})


# --------------------------------------------------------------------------- #
# RP6 — the engine's permission errors are translated to Vietnamese
# --------------------------------------------------------------------------- #
def test_rp6_run_report_translates_permission_error(harness):
    monkey_engine = _build_engine_stub(
        error=_FrappePermissionError("Must have report permission to access this report.")
    )
    harness_mod_engine = sys.modules["frappe.desk.query_report"]
    harness_mod_engine.run = monkey_engine.run
    with pytest.raises(_FrappePermissionError, match="Bạn không có quyền chạy báo cáo này"):
        harness.api.run_report("Monthly Attendance Sheet", filters={})


# filter-link permission errors are translated too (validate_filters_permissions)
def test_rp6b_run_report_translates_link_filter_error(harness):
    harness_mod_engine = sys.modules["frappe.desk.query_report"]
    harness_mod_engine.run = _build_engine_stub(
        error=_FrappeValidationError("You do not have permission to access Company: GeGe Vietnam.")
    ).run
    with pytest.raises(_FrappePermissionError, match="Bạn không có quyền chạy báo cáo này"):
        harness.api.run_report("Monthly Attendance Sheet", filters={})


# --------------------------------------------------------------------------- #
# RP7 — real report validation errors propagate untranslated
# --------------------------------------------------------------------------- #
def test_rp7_run_report_propagates_validation(harness):
    harness_mod_engine = sys.modules["frappe.desk.query_report"]
    harness_mod_engine.run = _build_engine_stub(error=_FrappeValidationError("Please select company.")).run
    with pytest.raises(_FrappeValidationError, match="Please select company."):
        harness.api.run_report("Monthly Attendance Sheet", filters={})


# --------------------------------------------------------------------------- #
# RP5 — report_meta mirrors the Report.filters child table
# --------------------------------------------------------------------------- #
def test_rp5_report_meta_filters_shape(harness):
    meta = harness.api.report_meta("Monthly Attendance Sheet")
    assert meta["name"] == "Monthly Attendance Sheet"
    assert meta["ref_doctype"] == "Attendance"
    assert [f["fieldname"] for f in meta["filters"]] == ["company", "month"]
    assert meta["filters"][0] == {
        "fieldname": "company",
        "label": "Company",
        "fieldtype": "Link",
        "options": "Company",
        "default": None,
        "reqd": 1,
        "depends_on": None,
    }


# report_meta also refuses non-allowlisted names
def test_rp5b_report_meta_rejects_unknown(harness):
    with pytest.raises(_FrappeValidationError):
        harness.api.report_meta("Profit and Loss Statement")


# --------------------------------------------------------------------------- #
# RP10 — Report.roles gate on run too (System-Manager-only report vs HR User)
# --------------------------------------------------------------------------- #
def test_rp10_run_report_role_gate(harness, monkeypatch):
    monkeypatch.setattr(harness.mod, "get_roles", lambda user=None: ("HR User",))
    harness.docs = {
        "Monthly Attendance Sheet": FakeReportDoc(
            "Monthly Attendance Sheet", "Attendance", roles=("System Manager",)
        ),
    }
    with pytest.raises(_FrappePermissionError, match="Bạn không có quyền chạy báo cáo này"):
        harness.api.run_report("Monthly Attendance Sheet", filters={})
