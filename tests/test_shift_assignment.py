"""Bench-free unit tests for the Shift Assignment parity (P1) logic in
``api/admin.py`` — mirrors the stub-frappe harness of ``test_hardening.py`` /
``test_pagination.py`` (``monkeypatch.setitem(sys.modules, "frappe", stub)``).

Scope (plans/shift-assignment-parity.md §3.1):
  * display lifecycle Active/Expired/Cancelled (G3/G4 — replaces dead "Completed")
  * 4-bucket summary over the full filtered set
  * timing-aware overlap respecting HR Settings ``allow_multiple`` (G5)
  * list filter-building incl. work_location / department / company (G1/G2)
  * cancel safeguard against linked Checkin/Attendance (G6)
  * options endpoint shape
"""

from __future__ import annotations

import datetime
import importlib
import sys
import types

import pytest

ADMIN_API = "gege_hr.gege_hr.api.admin"


def _dt(h, m=0):
    return datetime.datetime(2026, 1, 1, h, m)


class _DotDict(dict):
    """Mirrors frappe._dict — attribute access on a dict (get_all/get_value rows)."""

    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            return None

    def __setattr__(self, k, v):
        self[k] = v


class _FakeDoc:
    def __init__(self, payload, name="NEW-0001"):
        if isinstance(payload, dict):
            self.__dict__.update(payload)
            self.name = payload.get("name", name)
        else:
            self.name = name or "NEW-0001"
        self.inserted = self.saved = self.submitted = self.cancelled = False

    def insert(self, **k):
        self.inserted = True
        return self

    def submit(self):
        self.submitted = True
        return self

    def save(self, **k):
        self.saved = True
        return self

    def cancel(self):
        self.cancelled = True
        return self


class _FakeMeta:
    def __init__(self, fields):
        self._f = set(fields)

    def has_field(self, name):
        return name in self._f


class _FakeDB:
    def __init__(self):
        self.rows = {}  # doctype -> list[dict | str]
        self.values = {}  # (doctype, name) -> dict
        self.settings = {}  # (setting, key) -> value
        self.exists_set = set()  # {(doctype, name)}
        self.calls = []

    def get_all(self, doctype, **kw):
        self.calls.append({"doctype": doctype, **kw})
        base = list(self.rows.get(doctype, []))
        # Apply simple equality filters (["field","=",val]) so overlap queries
        # scoped by employee resolve correctly (parity G5 bulk partial test).
        filters = kw.get("filters")
        if isinstance(filters, list):
            eq = {
                f[0]: f[2]
                for f in filters
                if isinstance(f, (list, tuple)) and len(f) == 3 and f[1] == "="
            }
            base = [
                r for r in base if not isinstance(r, dict) or all(r.get(k) == v for k, v in eq.items())
            ]
        pluck = kw.get("pluck")
        if pluck:
            return [r[pluck] if isinstance(r, dict) else r for r in base]
        lpl = int(kw.get("limit_page_length") or 0)
        ls = int(kw.get("limit_start") or 0)
        if lpl:
            base = base[ls : ls + lpl]
        return [_DotDict(r) if isinstance(r, dict) else r for r in base]

    def get_value(self, doctype, name, fields=None, as_dict=False):
        v = self.values.get((doctype, name))
        if v is None:
            return None
        if as_dict:
            return _DotDict(v)
        if isinstance(fields, str):
            return v.get(fields)
        return v

    def get_single_value(self, setting, key):
        return self.settings.get((setting, key))

    def exists(self, doctype, name):
        return (doctype, name) in self.exists_set


class _Stub:
    def __init__(self, db, meta_fields=None, doc_map=None):
        self._ = lambda s: s
        self.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)
        self.only_for = lambda roles: None  # allow (HR admin gate satisfied)
        self.log_error = lambda *a, **k: None
        self.db = db
        self.get_all = db.get_all
        self.get_value = db.get_value
        self._meta = meta_fields or {}
        self.get_meta = lambda dt: _FakeMeta(self._meta.get(dt, []))
        self._doc_map = doc_map or {}
        self.created = []
        self.session = types.SimpleNamespace(user="hr.manager@gege.test")

    def get_doc(self, payload, name=None):
        if isinstance(payload, str) and (payload, name) in self._doc_map:
            return self._doc_map[(payload, name)]
        doc = _FakeDoc(payload, name or "NEW-0001")
        self.created.append(doc)
        return doc

    def throw(self, msg, exc=Exception, *a, **k):
        raise exc(msg)


def _utils():
    utils = types.ModuleType("frappe.utils")
    utils.getdate = lambda v=None: (
        datetime.date.today() if v in (None, "") else datetime.date.fromisoformat(str(v)[:10])
    )
    return utils


@pytest.fixture
def admin(monkeypatch):
    db = _FakeDB()
    stub = _Stub(db)
    utils = _utils()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    # Drop any cached binding so the import resolves against our stub.
    sys.modules.pop(ADMIN_API, None)
    mod = importlib.import_module(ADMIN_API)
    monkeypatch.setattr(mod, "frappe", stub)
    # Audit + company helpers are out of scope here — record/no-op them.
    monkeypatch.setattr(mod, "_audit_admin", lambda *a, **k: None)
    monkeypatch.setattr(mod, "_company_for_employee", lambda employee: "CO-1")
    return mod, stub, db


# --------------------------------------------------------------------------- #
# G3/G4 — display lifecycle + summary buckets (replaces dead "Completed")
# --------------------------------------------------------------------------- #
def test_display_status_active_expired_cancelled(admin):
    mod, _, _ = admin
    assert mod._shift_display_status({"docstatus": 1, "status": "Active"}) == "Active"
    assert mod._shift_display_status({"docstatus": 1, "status": "Inactive"}) == "Expired"
    assert mod._shift_display_status({"docstatus": 2, "status": "Inactive"}) == "Cancelled"
    assert mod._shift_display_status({"docstatus": 2, "status": "Active"}) == "Cancelled"
    assert mod._shift_display_status({}) == ""
    # Native never sets "Completed" — it must map to nothing special / cancelled.
    assert mod._shift_display_status({"docstatus": 1, "status": "Completed"}) == "Active"


def test_summary_four_buckets(admin):
    mod, _, _ = admin
    rows = [
        {"docstatus": 1, "status": "Active"},
        {"docstatus": 1, "status": "Active"},
        {"docstatus": 1, "status": "Inactive"},  # expired via scheduler
        {"docstatus": 2, "status": "Inactive"},  # HR ended (cancel)
        {"docstatus": 2, "status": "Inactive"},
    ]
    s = mod._shift_assignment_summary(rows)
    assert s == {"total": 5, "active": 2, "expired": 1, "cancelled": 2}
    # "completed" bucket must no longer exist (G4).
    assert "completed" not in s


def test_augment_row(admin):
    mod, _, _ = admin
    r = mod._shift_assignment_row({"name": "SA-1", "docstatus": 2, "status": "Inactive"})
    assert r["display_status"] == "Cancelled"
    assert r["is_cancelled"] is True
    assert r["work_location"] == ""


# --------------------------------------------------------------------------- #
# G5 — timing-aware overlap respecting HR Settings allow_multiple
# --------------------------------------------------------------------------- #
def test_timings_overlap_identical_and_disjoint(admin):
    mod, stub, db = admin
    db.values[("Shift Type", "Day")] = {"start_time": _dt(9), "end_time": _dt(17)}
    db.values[("Shift Type", "Morning")] = {"start_time": _dt(8), "end_time": _dt(12)}
    db.values[("Shift Type", "Evening")] = {"start_time": _dt(13), "end_time": _dt(17)}
    # identical window → overlap
    assert mod._shifts_have_overlapping_timings("Day", "Day") is True
    # disjoint morning vs evening → NOT overlapping (allowed)
    assert mod._shifts_have_overlapping_timings("Morning", "Evening") is False


def test_create_blocks_only_when_timings_overlap(admin):
    mod, stub, db = admin
    db.exists_set = {("Employee", "E-1"), ("Shift Type", "Day"), ("Shift Type", "Evening")}
    db.settings[("HR Settings", "allow_multiple_shift_assignments")] = 0
    db.values[("Shift Type", "Day")] = {"start_time": _dt(9), "end_time": _dt(17)}
    db.values[("Shift Type", "Evening")] = {"start_time": _dt(18), "end_time": _dt(22)}
    # Existing Active assignment with OVERLAPPING timing → blocked.
    db.rows["Shift Assignment"] = [
        {
            "name": "SA-X",
            "employee": "E-1",
            "status": "Active",
            "docstatus": 1,
            "shift_type": "Day",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
        }
    ]
    with pytest.raises(Exception):
        mod.create_shift_assignment(
            employee="E-1", shift_type="Day", start_date="2026-06-01"
        )
    # Same-date overlap but DISJOINT timing (Evening) → allowed (no throw).
    db.rows["Shift Assignment"] = [
        {
            "name": "SA-Y",
            "employee": "E-1",
            "status": "Active",
            "docstatus": 1,
            "shift_type": "Day",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
        }
    ]
    doc = mod.create_shift_assignment(
        employee="E-1", shift_type="Evening", start_date="2026-06-01"
    )
    assert doc["name"]  # created + submitted


def test_create_allows_when_hr_settings_allow_multiple(admin):
    mod, stub, db = admin
    db.exists_set = {("Employee", "E-1"), ("Shift Type", "Day")}
    db.settings[("HR Settings", "allow_multiple_shift_assignments")] = 1
    db.values[("Shift Type", "Day")] = {"start_time": _dt(9), "end_time": _dt(17)}
    db.rows["Shift Assignment"] = [
        {"name": "SA-X", "shift_type": "Day", "start_date": "2026-01-01", "end_date": "2026-12-31"}
    ]
    # Even fully overlapping → allowed because HR Settings says so (native parity).
    doc = mod.create_shift_assignment(employee="E-1", shift_type="Day", start_date="2026-06-01")
    assert doc["name"]


# --------------------------------------------------------------------------- #
# G3/G4/G1/G2 — list filter-building (display_status + work_location/department/company)
# --------------------------------------------------------------------------- #
def _last_filters(db, doctype="Shift Assignment"):
    calls = [c for c in db.calls if c["doctype"] == doctype]
    assert calls, f"no get_all call for {doctype}"
    return calls[-1].get("filters") or []


def _has_filter(filters, cond):
    return list(cond) in [list(f) for f in filters]


def test_list_default_shows_submitted_and_cancelled(admin):
    mod, stub, db = admin
    mod.list_shift_assignments()
    filters = _last_filters(db)
    # History visible: submitted + cancelled both included by default (G3).
    assert _has_filter(filters, ["docstatus", "in", [1, 2]])


def test_list_display_status_cancelled_filter(admin):
    mod, stub, db = admin
    mod.list_shift_assignments(display_status="Cancelled")
    filters = _last_filters(db)
    assert ["docstatus", "=", 2] in filters or ["docstatus", "=", 2] in [list(f) for f in filters]


def test_list_work_location_department_company_filters(admin):
    mod, stub, db = admin
    stub._meta["Shift Assignment"] = ["vn_work_location"]
    mod.list_shift_assignments(
        work_location="HQ",
        department="Engineering",
        company="CO-1",
        shift_type="Day",
        display_status="Active",
    )
    filters = _last_filters(db)
    normalized = [list(f) for f in filters]
    assert ["vn_work_location", "=", "HQ"] in normalized
    assert ["department", "=", "Engineering"] in normalized
    assert ["company", "=", "CO-1"] in normalized
    assert ["shift_type", "=", "Day"] in normalized


def test_list_broad_search_covers_department_and_work_location(admin):
    mod, stub, db = admin
    stub._meta["Shift Assignment"] = ["vn_work_location"]
    mod.list_shift_assignments(search="engineering")
    call = [c for c in db.calls if c["doctype"] == "Shift Assignment"][-1]
    or_filters = [list(o) for o in (call.get("or_filters") or [])]
    assert ["department", "like", "%engineering%"] in or_filters
    assert ["vn_work_location", "like", "%engineering%"] in or_filters


# --------------------------------------------------------------------------- #
# G6 — cancel safeguard against linked Checkin / Attendance
# --------------------------------------------------------------------------- #
def test_end_blocked_when_attendance_linked(admin):
    mod, stub, db = admin
    db.exists_set = {("Shift Assignment", "SA-1")}
    doc = _FakeDoc("Shift Assignment", name="SA-1")
    doc.docstatus = 1
    doc.start_date = datetime.date(2026, 1, 1)
    doc.end_date = datetime.date(2026, 6, 30)
    doc.employee = "E-1"
    doc.shift_type = "Day"
    doc.company = "CO-1"
    stub._doc_map[("Shift Assignment", "SA-1")] = doc
    db.rows["Attendance"] = [{"name": "ATT-1"}]  # linked attendance present
    db.rows["Employee Checkin"] = []
    with pytest.raises(Exception):
        mod.end_shift_assignment("SA-1", "2026-06-15")
    assert doc.cancelled is False  # not cancelled (blocked)


def test_end_cancels_when_no_links(admin):
    mod, stub, db = admin
    db.exists_set = {("Shift Assignment", "SA-2")}
    doc = _FakeDoc("Shift Assignment", name="SA-2")
    doc.docstatus = 1
    doc.start_date = datetime.date(2026, 1, 1)
    doc.end_date = None
    doc.employee = "E-1"
    doc.shift_type = "Day"
    doc.company = "CO-1"
    stub._doc_map[("Shift Assignment", "SA-2")] = doc
    db.rows["Attendance"] = []
    db.rows["Employee Checkin"] = []
    mod.end_shift_assignment("SA-2", "2026-06-15")
    assert doc.saved is True
    assert doc.cancelled is True  # now cancelled (and stays visible in the list)


# --------------------------------------------------------------------------- #
# G1/G2/G4 — options endpoint shape (auto-fetched popover dropdowns)
# --------------------------------------------------------------------------- #
def test_options_shape_and_work_location_gated(admin):
    mod, stub, db = admin
    stub._meta["Shift Assignment"] = []  # vn_work_location NOT migrated
    db.rows["Shift Type"] = [{"name": "Day"}, {"name": "Night"}]
    db.rows["Department"] = [{"name": "Engineering"}]
    db.rows["Company"] = [{"name": "CO-1"}]
    opts = mod.get_shift_assignment_options()
    assert [s["value"] for s in opts["statuses"]] == ["Active", "Expired", "Cancelled"]
    assert opts["work_locations"] == []  # gated — field not present
    stub._meta["Shift Assignment"] = ["vn_work_location"]
    db.rows["VN Work Location"] = [{"name": "HQ"}]
    opts2 = mod.get_shift_assignment_options()
    assert opts2["work_locations"] == [{"value": "HQ", "label": "HQ"}]


# --------------------------------------------------------------------------- #
# G7 — bulk-assign (one shift → many employees, partial-safe)
# --------------------------------------------------------------------------- #
def test_bulk_creates_all_when_no_conflicts(admin):
    mod, stub, db = admin
    db.exists_set = {
        ("Employee", "E-1"),
        ("Employee", "E-2"),
        ("Employee", "E-3"),
        ("Shift Type", "Day"),
    }
    db.settings[("HR Settings", "allow_multiple_shift_assignments")] = 0
    db.values[("Shift Type", "Day")] = {"start_time": _dt(9), "end_time": _dt(17)}
    db.rows["Shift Assignment"] = []  # no existing active assignment
    res = mod.bulk_create_shift_assignments(
        employees=["E-1", "E-2", "E-3"], shift_type="Day", start_date="2026-06-01"
    )
    assert len(res["created"]) == 3
    assert res["failed"] == []


def test_bulk_partial_failure_isolated(admin):
    mod, stub, db = admin
    db.exists_set = {("Employee", "E-1"), ("Employee", "E-2"), ("Shift Type", "Day")}
    db.settings[("HR Settings", "allow_multiple_shift_assignments")] = 0
    db.values[("Shift Type", "Day")] = {"start_time": _dt(9), "end_time": _dt(17)}
    # E-1 already has an overlapping Active assignment → conflict; E-2 is clean.
    db.rows["Shift Assignment"] = [
        {
            "name": "SA-X",
            "employee": "E-1",
            "shift_type": "Day",
            "start_date": "2026-01-01",
            "end_date": "2026-12-31",
            "status": "Active",
            "docstatus": 1,
        }
    ]
    res = mod.bulk_create_shift_assignments(
        employees=["E-1", "E-2"], shift_type="Day", start_date="2026-06-01"
    )
    assert len(res["created"]) == 1  # only E-2
    assert len(res["failed"]) == 1
    assert res["failed"][0]["employee"] == "E-1"
    assert res["failed"][0]["reason"]  # carries the overlap message


def test_bulk_requires_employees_and_shift(admin):
    mod, stub, db = admin
    with pytest.raises(Exception):
        mod.bulk_create_shift_assignments(employees=[], shift_type="Day", start_date="2026-06-01")
    with pytest.raises(Exception):
        mod.bulk_create_shift_assignments(
            employees=["E-1"], shift_type="", start_date="2026-06-01"
        )


# --------------------------------------------------------------------------- #
# G9 — Shift Request (reuse native hrms "Shift Request" doctype)
# --------------------------------------------------------------------------- #
def test_create_shift_request_inserts_draft(admin):
    mod, stub, db = admin
    db.exists_set = {("Employee", "E-1"), ("Shift Type", "Day")}
    res = mod.create_shift_request(employee="E-1", shift_type="Day", from_date="2026-06-01")
    assert res["name"]
    reqs = [d for d in stub.created if getattr(d, "doctype", None) == "Shift Request"]
    assert len(reqs) == 1
    assert reqs[0].status == "Draft"


def test_approve_shift_request_creates_linked_assignment(admin):
    mod, stub, db = admin
    db.exists_set = {
        ("Shift Request", "SR-1"),
        ("Employee", "E-1"),
        ("Shift Type", "Day"),
    }
    db.settings[("HR Settings", "allow_multiple_shift_assignments")] = 0
    db.values[("Shift Type", "Day")] = {"start_time": _dt(9), "end_time": _dt(17)}
    req = _FakeDoc(
        {
            "doctype": "Shift Request",
            "name": "SR-1",
            "employee": "E-1",
            "shift_type": "Day",
            "from_date": datetime.date(2026, 6, 1),
            "to_date": None,
            "status": "Draft",
            "company": "CO-1",
        }
    )
    stub._doc_map[("Shift Request", "SR-1")] = req
    res = mod.approve_shift_request("SR-1")
    assert req.status == "Approved"
    assert res["shift_assignment"]  # an assignment was created
    sa = [d for d in stub.created if getattr(d, "doctype", None) == "Shift Assignment"]
    assert sa and getattr(sa[-1], "shift_request", None) == "SR-1"  # back-linked


def test_reject_shift_request_creates_no_assignment(admin):
    mod, stub, db = admin
    db.exists_set = {("Shift Request", "SR-2")}
    req = _FakeDoc(
        {
            "doctype": "Shift Request",
            "name": "SR-2",
            "employee": "E-1",
            "shift_type": "Day",
            "from_date": datetime.date(2026, 6, 1),
            "status": "Draft",
            "company": "CO-1",
        }
    )
    stub._doc_map[("Shift Request", "SR-2")] = req
    mod.reject_shift_request("SR-2", "overlap")
    assert req.status == "Rejected"
    assert not [d for d in stub.created if getattr(d, "doctype", None) == "Shift Assignment"]


def test_approve_already_approved_throws(admin):
    mod, stub, db = admin
    db.exists_set = {
        ("Shift Request", "SR-3"),
        ("Employee", "E-1"),
        ("Shift Type", "Day"),
    }
    req = _FakeDoc(
        {
            "doctype": "Shift Request",
            "name": "SR-3",
            "employee": "E-1",
            "shift_type": "Day",
            "from_date": datetime.date(2026, 6, 1),
            "status": "Approved",
            "company": "CO-1",
        }
    )
    stub._doc_map[("Shift Request", "SR-3")] = req
    with pytest.raises(Exception):
        mod.approve_shift_request("SR-3")


# --------------------------------------------------------------------------- #
# G8 — recurring shift schedule generator (self-contained; native schedule
# doctype absent in this hrms version)
# --------------------------------------------------------------------------- #
def test_recurring_continuous_creates_one_run_per_employee(admin):
    mod, stub, db = admin
    db.exists_set = {("Employee", "E-1"), ("Employee", "E-2"), ("Shift Type", "Day")}
    db.settings[("HR Settings", "allow_multiple_shift_assignments")] = 0
    db.values[("Shift Type", "Day")] = {"start_time": _dt(9), "end_time": _dt(17)}
    res = mod.generate_recurring_shift_assignments(
        employees=["E-1", "E-2"], shift_type="Day", from_date="2026-06-01", to_date="2026-06-30"
    )
    assert len(res["runs"]) == 1  # continuous range = single run
    assert len(res["created"]) == 2  # one assignment per employee
    assert res["failed"] == []


def test_recurring_weekday_subset_splits_runs(admin):
    mod, stub, db = admin
    db.exists_set = {("Employee", "E-1"), ("Shift Type", "Day")}
    db.settings[("HR Settings", "allow_multiple_shift_assignments")] = 0
    db.values[("Shift Type", "Day")] = {"start_time": _dt(9), "end_time": _dt(17)}
    # 2026-06-01 = Mon ... 2026-06-07 = Sun. Mon(0) & Wed(2) → 2 single-day runs.
    res = mod.generate_recurring_shift_assignments(
        employees=["E-1"],
        shift_type="Day",
        from_date="2026-06-01",
        to_date="2026-06-07",
        weekdays=[0, 2],
    )
    assert len(res["runs"]) == 2
    assert res["runs"][0] == {"from_date": "2026-06-01", "to_date": "2026-06-01"}
    assert res["runs"][1] == {"from_date": "2026-06-03", "to_date": "2026-06-03"}
    assert len(res["created"]) == 2


# --------------------------------------------------------------------------- #
# Status toggle (Ngưng / Kích hoạt lại) — reversible, does NOT cancel
# --------------------------------------------------------------------------- #
def test_set_shift_assignment_status_toggles(admin):
    mod, stub, db = admin
    db.exists_set = {("Shift Assignment", "SA-1")}
    doc = _FakeDoc(
        {
            "doctype": "Shift Assignment",
            "name": "SA-1",
            "docstatus": 1,
            "status": "Active",
            "employee": "E-1",
            "shift_type": "Day",
            "company": "CO-1",
        }
    )
    stub._doc_map[("Shift Assignment", "SA-1")] = doc
    res = mod.set_shift_assignment_status("SA-1", "Inactive")
    assert doc.status == "Inactive"
    assert doc.saved is True  # kept submitted, NOT cancelled
    assert doc.cancelled is False
    assert res["status"] == "Inactive"
    # invalid status rejected (no "Completed" etc.)
    with pytest.raises(Exception):
        mod.set_shift_assignment_status("SA-1", "Completed")
