"""Bench-free unit tests for ``api/employee_services.py`` (NEW-4, hr-gap-audit 🟥).

Extended by plan-test-complete-hr-extra §3.2 (SV-B01→B11): HRMS field mapping
(``raised_by``/``date``/``resolution_detail``), G2 date-order validation, G5
``reject_travel_request`` + reason persistence, vn_* travel projection, and
best-effort notifications.
"""

import importlib
import sys
import types

import pytest


class _Doc:
    def __init__(self, doctype, store):
        self.doctype = doctype
        self._store = store
        self.name = None
        self.status = "Open"

    def insert(self, ignore_permissions=False):
        self.name = self.name or f"{self.doctype.replace(' ', '-')}-{len(self._store) + 1:04d}"
        self._store[(self.doctype, self.name)] = self
        return self

    def save(self, *a, **k):
        return self

    def get(self, key, default=None):
        return getattr(self, key, default)


class _Frappe:
    def __init__(self):
        self.store = {}
        self.list_rows = {}
        self.employee_for_user = "HR-EMP-1"
        self.roles = {"HR Manager"}

    def whitelist(self, fn=None, **k):
        return fn if fn is not None else (lambda f: f)

    def _(self, s):
        return s

    def log_error(self, *a, **k):
        return None

    def throw(self, msg, *a, **k):
        raise Exception(msg)

    @property
    def session(self):
        return types.SimpleNamespace(user="hr@gege.local")

    def get_roles(self, user):
        return set(self.roles)

    class _DB:
        def __init__(self, fr):
            self.fr = fr

        def get_value(self, doctype, key, field=None, as_dict=False):
            if doctype == "Employee" and isinstance(key, dict):
                return self.fr.employee_for_user
            return None

    @property
    def db(self):
        return self._DB(self)

    def new_doc(self, doctype):
        return _Doc(doctype, self.store)

    def get_doc(self, doctype, name):
        return self.store.get((doctype, name))

    def get_all(
        self,
        doctype,
        filters=None,
        or_filters=None,
        fields=None,
        order_by=None,
        limit_start=0,
        limit_page_length=0,
        pluck=None,
        **k,
    ):
        rows = list(self.list_rows.get(doctype, []))

        def _match(r, cond):
            op, val = cond[1], cond[2]
            got = r.get(cond[0])
            if op == "like":
                return str(got or "").find(str(val).strip("%")) >= 0
            if op == ">=":
                return got is not None and str(got) >= str(val)
            if op == "<=":
                return got is not None and str(got) <= str(val)
            return got == val

        def keep(r):
            if filters:
                conds = [[k2, "=", v] for k2, v in filters.items()] if isinstance(filters, dict) else filters
                for cond in conds:
                    if not _match(r, cond):
                        return False
            if or_filters:
                if not any(_match(r, c) for c in or_filters):
                    return False
            return True

        rows = [r for r in rows if keep(r)]
        if limit_page_length:
            rows = rows[int(limit_start or 0) : int(limit_start or 0) + int(limit_page_length)]
        if pluck:
            return [r.get(pluck) for r in rows]
        if fields:
            return [{f: r.get(f) for f in fields} for r in rows]
        return rows


@pytest.fixture
def mod(monkeypatch):
    stub = _Frappe()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    m = importlib.import_module("gege_hr.gege_hr.api.employee_services")
    importlib.reload(m)
    return m, stub


# ── pure projections ────────────────────────────────────────────────────────
def test_project_grievance_row(mod):
    m, _ = mod
    row = m.project_grievance_row({"name": "G1", "raised_by": "E1", "date": "2026-08-19"})
    assert row["employee"] == "E1"
    assert row["raised_on"] == "2026-08-19"
    assert "raised_by" not in row and "date" not in row


def test_project_travel_row(mod):
    m, _ = mod
    row = m.project_travel_row(
        {
            "name": "T1",
            "vn_purpose": "Đi KH",
            "vn_from_date": "2026-08-10",
            "vn_to_date": "2026-08-12",
            "vn_total_cost": 2000000,
            "vn_status": None,
            "docstatus": 1,
        }
    )
    assert row["status"] == "Approved"  # docstatus fallback
    assert row["purpose_of_travel"] == "Đi KH"
    assert row["from_date"] == "2026-08-10"
    assert row["to_date"] == "2026-08-12"
    assert row["total_travel_cost"] == 2000000


def test_date_order_ok(mod):
    m, _ = mod
    assert m.date_order_ok("2026-08-10", "2026-08-12") is True
    assert m.date_order_ok("2026-08-12", "2026-08-10") is False


# ── grievance ───────────────────────────────────────────────────────────────
def test_my_grievances_filters_own(mod):  # uses raised_by (HRMS column)
    m, stub = mod
    stub.list_rows["Employee Grievance"] = [
        {"name": "G-1", "raised_by": "HR-EMP-1", "subject": "A"},
        {"name": "G-2", "raised_by": "HR-EMP-2", "subject": "B"},
    ]
    res = m.my_grievances()
    assert res["total"] == 1
    assert res["data"][0]["name"] == "G-1"
    assert res["data"][0]["employee"] == "HR-EMP-1"  # projected key


def test_submit_grievance_requires_subject(mod):  # SV-B02
    m, _ = mod
    with pytest.raises(Exception):
        m.submit_grievance(employee="HR-EMP-1", subject="   ")


def test_submit_grievance_fills_hrms_mandatory(mod):  # SV-B01
    m, stub = mod
    res = m.submit_grievance(employee="HR-EMP-1", grievance_type="Lương", subject="Sai lương")
    doc = stub.store[("Employee Grievance", res["name"])]
    assert doc.subject == "Sai lương"
    assert doc.raised_by == "HR-EMP-1"
    assert doc.grievance_against_party == "Employee"  # HRMS mandatory
    assert doc.grievance_against == "HR-EMP-1"
    assert doc.grievance_type == "Lương"
    assert doc.description == "Sai lương"  # falls back to subject (mandatory)
    assert doc.date  # set when absent


def test_submit_grievance_seeds_default_type(mod):
    m, stub = mod
    stub.list_rows["Grievance Type"] = []
    m.submit_grievance(employee="HR-EMP-1", subject="X")
    seeded = [k for k in stub.store if k[0] == "Grievance Type"]
    assert seeded, "a default Grievance Type master must be seeded on first use"


def test_resolve_grievance_sets_status(mod):  # SV-B06
    m, _ = mod
    created = m.submit_grievance(employee="HR-EMP-1", subject="X")
    res = m.resolve_grievance(created["name"], resolution="Đã giải quyết")
    assert res["status"] == "Resolved"
    doc = m.frappe.store[("Employee Grievance", created["name"])]
    assert doc.resolution_detail == "Đã giải quyết"  # HRMS field
    assert doc.resolved_by == "hr@gege.local"


def test_resolve_grievance_requires_manager(mod):  # SV-B07
    m, stub = mod
    created = m.submit_grievance(employee="HR-EMP-1", subject="X")
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.resolve_grievance(created["name"])


def test_all_grievances_requires_manager(mod):
    m, stub = mod
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.all_grievances()


# ── travel ──────────────────────────────────────────────────────────────────
def test_submit_travel_validates(mod):  # SV-B04 + missing fields
    m, _ = mod
    with pytest.raises(Exception):
        m.submit_travel_request(employee="HR-EMP-1", purpose_of_travel="KH", from_date=None, to_date=None)
    with pytest.raises(Exception):
        m.submit_travel_request(
            employee="HR-EMP-1", purpose_of_travel="  ", from_date="2026-08-10", to_date="2026-08-12"
        )


def test_submit_travel_rejects_inverted_dates(mod):  # SV-B04 (G2)
    m, _ = mod
    with pytest.raises(Exception) as ei:
        m.submit_travel_request(
            employee="HR-EMP-1", purpose_of_travel="Đi khách", from_date="2026-08-12", to_date="2026-08-10"
        )
    assert "Ngày về không được trước ngày đi" in str(ei.value)


def test_submit_travel_bad_cost_is_ignored(mod):  # SV-B05
    m, stub = mod
    res = m.submit_travel_request(
        employee="HR-EMP-1",
        purpose_of_travel="Đi khách",
        from_date="2026-08-10",
        to_date="2026-08-12",
        estimated_cost="không-phải-số",
    )
    doc = stub.store[("Travel Request", res["name"])]
    assert doc.vn_purpose == "Đi khách"
    assert not getattr(doc, "vn_total_cost", None)  # skipped, no crash
    assert doc.travel_type == "Domestic"  # HRMS mandatory default


def test_approve_travel_sets_status(mod):  # SV-B08
    m, stub = mod
    created = m.submit_travel_request(
        employee="HR-EMP-1", purpose_of_travel="Đi khách", from_date="2026-08-10", to_date="2026-08-12"
    )
    res = m.approve_travel_request(created["name"])
    assert res["status"] == "Approved"
    doc = stub.store[("Travel Request", created["name"])]
    assert doc.vn_status == "Approved"
    stub.list_rows["Travel Request"] = [
        {"name": created["name"], "employee": "HR-EMP-1", "vn_status": "Approved", "docstatus": 0}
    ]
    rows = m.my_travel_requests(status="Approved")
    assert rows["total"] == 1 and rows["data"][0]["status"] == "Approved"


def test_reject_travel_request(mod):  # SV-B03 (G5)
    m, stub = mod
    created = m.submit_travel_request(
        employee="HR-EMP-1", purpose_of_travel="Công tác SG", from_date="2026-09-01", to_date="2026-09-03"
    )
    res = m.reject_travel_request(created["name"], reason="Hết ngân sách")
    assert res["status"] == "Rejected"
    assert "Từ chối: Hết ngân sách" in res["note"]
    doc = stub.store[("Travel Request", created["name"])]
    assert doc.vn_status == "Rejected"
    assert "Từ chối: Hết ngân sách" in doc.vn_note


def test_reject_travel_requires_manager(mod):
    m, stub = mod
    created = m.submit_travel_request(
        employee="HR-EMP-1", purpose_of_travel="X", from_date="2026-08-10", to_date="2026-08-12"
    )
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.reject_travel_request(created["name"])


def test_all_travel_requires_manager(mod):
    m, stub = mod
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.all_travel_requests()


# ── options / notify ────────────────────────────────────────────────────────
def test_grievance_options_empty_masters(mod):  # SV-B09
    m, _ = mod
    opts = m.grievance_options()
    assert opts["grievance_types"] == []
    assert set(("Draft", "Approved", "Rejected")) <= set(opts["travel_statuses"])


def test_notify_failure_never_breaks_resolve(mod, monkeypatch):  # SV-B11 (G6)
    m, _ = mod

    def _boom(**k):
        raise Exception("notify down")

    # monkeypatch (not raw assignment) — notify is a SHARED module; a bare
    # assignment leaks the stub into sibling tests (test_notify).
    monkeypatch.setattr(m.notify, "push_notification", _boom)
    created = m.submit_grievance(employee="HR-EMP-1", subject="X")
    res = m.resolve_grievance(created["name"], resolution="ok")  # must NOT raise
    assert res["status"] == "Resolved"
