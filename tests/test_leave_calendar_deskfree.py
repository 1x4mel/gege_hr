"""Bench-free unit tests for the desk-free leave calendar API layer
(``api/leave_calendar.py`` + the ``api/leave.py`` touch wiring —
plans/plan-leave-calendar-desk-free.md §7a).

Stub-frappe harness pattern shared with ``test_blackout_api.py`` /
``test_blackout_admin.py``. Covers:

  * read gate (D2)                → LC1
  * v2 payload multi-status       → LC2 / LC12
  * read-time status filter       → LC3 (+ same cache key)
  * employee / leave_type filter  → LC4
  * bogus status validation       → LC6
  * holiday overlay               → LC7
  * touch_leave_calendar          → LC8 (4 scopes × months + realtime)
  * hook / decision wiring        → LC9 / LC10
  * employee options              → LC11
  * legacy key never served       → LC13
"""

from __future__ import annotations

import datetime as dt
import importlib
import sys
import types

import pytest

from gege_hr.gege_hr.utils import leave_calendar as cal_utils

DOCTYPE = "VN Leave Calendar Cache"
COMPANY = "Gege Demo"


class _ValidationError(Exception):
    pass


class _PermissionError(Exception):
    pass


class _Meta:
    def has_field(self, name):
        return True


class _Doc:
    """Minimal document wrapper the stub ``get_doc`` returns."""

    def __init__(self, data, db):
        self.__dict__.update(data)
        self._db = db

    def insert(self, **_kw):
        self._db.cache_rows[self.cache_key] = dict(self.__dict__)
        return self

    def save(self, **_kw):
        if getattr(self, "cache_key", None):
            self._db.cache_rows[self.cache_key] = dict(self.__dict__)
        return self


class FakeDB:
    def __init__(self):
        self.leave_rows = []
        self.holiday_rows = []
        self.holiday_lists = []  # [{name}]
        self.employee_rows = []
        self.company_default_hl = {}  # company → holiday list
        self.cache_rows = {}  # cache_key → field dict
        self.deleted = []  # (doctype, name)

    # -- frappe.db.* -------------------------------------------------------- #
    def table_exists(self, doctype):
        return True

    def get_value(self, doctype, name=None, fieldname=None, as_dict=False, **_kw):
        if doctype == "Company":
            return self.company_default_hl.get(name)
        if doctype == DOCTYPE:
            row = self.cache_rows.get(name)
            if not row:
                return None
            if isinstance(fieldname, (list, tuple)):
                return {f: row.get(f) for f in fieldname} if as_dict else row.get(fieldname[0])
            return row.get(fieldname)
        if doctype == "Employee":
            for r in self.employee_rows:
                if r.get("name") == name:
                    return {f: r.get(f) for f in fieldname} if as_dict else r.get(fieldname)
            return None
        if doctype == "Leave Application":
            for r in self.leave_rows:
                if r.get("name") == name:
                    return {f: r.get(f) for f in fieldname} if as_dict else r.get(fieldname)
            return None
        return None

    def get_all(self, doctype, filters=None, fields=None, order_by=None,
                pluck=None, limit_page_length=None, **_kw):
        filters = filters or {}
        if doctype == "Leave Application":
            statuses = dict(filters).get("status")
            in_statuses = statuses[1] if isinstance(statuses, tuple) else None
            rows = [
                r for r in self.leave_rows
                if r.get("docstatus", 0) < 2
                and (not in_statuses or r.get("status") in in_statuses)
                and str(r.get("from_date")) <= str(filters.get("from_date")[1])
                and str(r.get("to_date")) >= str(filters.get("to_date")[1])
            ]
            return [dict(r) for r in rows]
        if doctype == "Holiday":
            lo, hi = filters.get("holiday_date")[1]
            rows = [
                h for h in self.holiday_rows
                if h.get("parent") == filters.get("parent") and lo <= str(h.get("holiday_date")) <= hi
            ]
            return [{k: h.get(k) for k in (fields or [])} for h in rows]
        if doctype == "Holiday List":
            names = [h["name"] for h in self.holiday_lists]
            return names if pluck else [{"name": n} for n in names]
        if doctype == "Employee":
            rows = [
                r for r in self.employee_rows
                if r.get("status") == filters.get("status", "Active")
                and (
                    "employee_name" not in filters
                    or filters["employee_name"][1].strip("%").lower() in str(r.get("employee_name", "")).lower()
                )
            ]
            if limit_page_length:
                rows = rows[:limit_page_length]
            return [dict(r) for r in rows]
        return []


def make_fake(monkeypatch, roles=("HR Manager",)):
    """Register the stub frappe + import the api modules with it rebound."""
    db = FakeDB()

    frappe_stub = types.ModuleType("frappe")
    frappe_stub.db = db
    frappe_stub._ = lambda s: s
    frappe_stub.ValidationError = _ValidationError
    frappe_stub.PermissionError = _PermissionError
    frappe_stub.session = types.SimpleNamespace(user="hr@test.local")
    frappe_stub.get_roles = lambda user=None: tuple(roles)
    frappe_stub.whitelist = lambda *a, **k: (lambda f: f)

    def _throw(msg, exc=None):
        raise (exc or Exception)(msg)

    frappe_stub.throw = _throw
    frappe_stub.get_meta = lambda doctype: _Meta()
    frappe_stub.log_error = lambda *a, **k: None

    published = []
    frappe_stub.publish_realtime = lambda event, payload=None, **kw: published.append((event, payload))

    frappe_stub.get_all = db.get_all
    frappe_stub.get_doc = lambda arg, *a: _Doc(dict(arg), db) if isinstance(arg, dict) else _Doc(
        dict(db.cache_rows.get(arg, {"name": arg, "cache_key": arg})), db
    )

    def _delete_doc(doctype, name, **_kw):
        db.deleted.append((doctype, name))
        for key in list(db.cache_rows):
            if db.cache_rows[key].get("name") == name or key == name:
                db.cache_rows.pop(key, None)

    frappe_stub.delete_doc = _delete_doc

    frappe_stub.utils = types.ModuleType("frappe.utils")
    frappe_stub.utils.now = lambda: "2026-08-29 12:00:00"
    frappe_stub.utils.getdate = lambda v=None: dt.date.today() if v in (None, "") else dt.date.fromisoformat(str(v)[:10])
    frappe_stub.utils.cint = lambda v, default=0: int(v) if v not in (None, "") else default
    frappe_stub.utils.today = lambda: dt.date.today().isoformat()

    monkeypatch.setitem(sys.modules, "frappe", frappe_stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", frappe_stub.utils)

    api = importlib.import_module("gege_hr.gege_hr.api.leave_calendar")
    monkeypatch.setattr(api, "frappe", frappe_stub)
    leave_mod = importlib.import_module("gege_hr.gege_hr.api.leave")
    monkeypatch.setattr(leave_mod, "frappe", frappe_stub)

    fake = types.SimpleNamespace(
        db=db, api=api, leave=leave_mod, frappe=frappe_stub, published=published, roles=roles
    )
    return fake


def _leave(name, employee="HR-EMP-0001", status="Approved", frm="2026-09-07", to="2026-09-09",
           leave_type="Annual Leave", docstatus=None, **extra):
    row = {
        "name": name,
        "employee": employee,
        "employee_name": "Nguyễn Văn A",
        "leave_type": leave_type,
        "from_date": frm,
        "to_date": to,
        "total_leave_days": 3,
        "status": status,
        "half_day": 0,
        "half_day_date": None,
        "description": "Nghỉ gia đình",
        "posting_date": "2026-08-29",
        "department": "Sale",
        "branch": "HN",
        "owner": "a@test.local",
        "docstatus": docstatus if docstatus is not None else (1 if status == "Approved" else 0),
    }
    row.update(extra)
    return row


# --------------------------------------------------------------------------- #
# LC1 — read gate (D2)
# --------------------------------------------------------------------------- #
def test_lc1_read_gate_rejects_plain_employee(monkeypatch):
    fake = make_fake(monkeypatch, roles=("Employee",))
    with pytest.raises(Exception, match="quyền HR"):
        fake.api.get_leave_calendar(company=COMPANY, year=2026, month=9)


def test_lc1b_invalidate_gate_rejects_plain_employee(monkeypatch):
    fake = make_fake(monkeypatch, roles=("Employee",))
    with pytest.raises(Exception, match="quyền HR"):
        fake.api.invalidate_leave_calendar(company=COMPANY, year=2026, month=9)


# --------------------------------------------------------------------------- #
# LC2 / LC12 — v2 payload, multi-status, new fields, half-day
# --------------------------------------------------------------------------- #
def test_lc2_payload_v2_multi_status_with_holidays(monkeypatch):
    fake = make_fake(monkeypatch)
    fake.db.leave_rows = [
        _leave("LA-OPEN", status="Open"),
        _leave("LA-APPROVED", status="Approved"),
        _leave("LA-REJECTED", status="Rejected"),
        _leave("LA-CANCELLED", status="Cancelled", docstatus=2),
    ]
    fake.db.company_default_hl[COMPANY] = "HL-2026"
    fake.db.holiday_rows = [
        {"parent": "HL-2026", "holiday_date": "2026-09-02", "description": "Quốc khánh", "weekly_off": 0},
    ]
    out = fake.api.build_leave_calendar(company=COMPANY, year=2026, month=9)
    data = out["data"]
    assert set(data.keys()) == {"leaves", "holidays"}
    names = {r["name"] for r in data["leaves"]}
    assert names == {"LA-OPEN", "LA-APPROVED", "LA-REJECTED"}  # Cancelled excluded
    row = data["leaves"][0]
    for field in ("status", "half_day", "half_day_date", "description", "posting_date",
                  "department", "branch", "owner", "employee_name"):
        assert field in row  # LC12 — drawer/half-day fields ship
    assert data["holidays"][0]["description"] == "Quốc khánh"
    assert data["holidays"][0]["weekly_off"] == 0
    assert out["cache_key"].endswith("-v2")


# --------------------------------------------------------------------------- #
# LC3 — statuses is a read-time filter (same cache key, cached second read)
# --------------------------------------------------------------------------- #
def test_lc3_status_filter_is_read_time_not_cache_key(monkeypatch):
    fake = make_fake(monkeypatch)
    fake.db.leave_rows = [_leave("LA-OPEN", status="Open"), _leave("LA-APPROVED", status="Approved")]
    first = fake.api.get_leave_calendar(company=COMPANY, year=2026, month=9)
    assert {r["name"] for r in first["data"]["leaves"]} == {"LA-OPEN", "LA-APPROVED"}

    approved = fake.api.get_leave_calendar(company=COMPANY, year=2026, month=9, statuses="Approved")
    assert approved["from_cache"] is True  # served from the row built above
    assert approved["cache_key"] == first["cache_key"]  # filter never changes the key
    assert {r["name"] for r in approved["data"]["leaves"]} == {"LA-APPROVED"}

    both = fake.api.get_leave_calendar(company=COMPANY, year=2026, month=9, statuses="Open,Approved")
    assert len(both["data"]["leaves"]) == 2

    # The persisted cache row itself stays unfiltered.
    stored = fake.db.cache_rows[approved["cache_key"]]
    import json as _json

    stored_data = _json.loads(stored.get("data_json") or "{}")
    assert len(stored_data["leaves"]) == 2


# --------------------------------------------------------------------------- #
# LC4 — employee + leave_type read-time filters
# --------------------------------------------------------------------------- #
def test_lc4_employee_and_leave_type_filters(monkeypatch):
    fake = make_fake(monkeypatch)
    fake.db.leave_rows = [
        _leave("LA-A", employee="HR-EMP-0001", leave_type="Annual Leave"),
        _leave("LA-B", employee="HR-EMP-0002", leave_type="Annual Leave"),
        _leave("LA-C", employee="HR-EMP-0001", leave_type="Sick Leave"),
    ]
    out = fake.api.get_leave_calendar(
        company=COMPANY, year=2026, month=9, employee="HR-EMP-0001", leave_type="Annual Leave"
    )
    assert [r["name"] for r in out["data"]["leaves"]] == ["LA-A"]


# --------------------------------------------------------------------------- #
# LC6 — bogus status token
# --------------------------------------------------------------------------- #
def test_lc6_bogus_status_throws(monkeypatch):
    fake = make_fake(monkeypatch)
    with pytest.raises(Exception, match="không hợp lệ"):
        fake.api.get_leave_calendar(company=COMPANY, year=2026, month=9, statuses="Banned")


# --------------------------------------------------------------------------- #
# LC7 — holiday overlay source
# --------------------------------------------------------------------------- #
def test_lc7_holidays_from_company_default_list(monkeypatch):
    fake = make_fake(monkeypatch)
    fake.db.company_default_hl[COMPANY] = "HL-2026"
    fake.db.holiday_rows = [
        {"parent": "HL-2026", "holiday_date": "2026-09-06", "description": "Chủ nhật", "weekly_off": 1},
        {"parent": "HL-2026", "holiday_date": "2026-10-01", "description": "Ngoài tháng", "weekly_off": 0},
        {"parent": "HL-OTHER", "holiday_date": "2026-09-02", "description": "List khác", "weekly_off": 0},
    ]
    out = fake.api.build_leave_calendar(company=COMPANY, year=2026, month=9)
    hol = out["data"]["holidays"]
    assert len(hol) == 1  # đúng list + đúng tháng
    assert hol[0]["weekly_off"] == 1
    assert hol[0]["holiday_list"] == "HL-2026"


def test_lc7b_no_default_list_ambiguous_returns_empty(monkeypatch):
    fake = make_fake(monkeypatch)
    fake.db.holiday_lists = [{"name": "HL-A"}, {"name": "HL-B"}]  # 2 lists → không đoán
    out = fake.api.build_leave_calendar(company=COMPANY, year=2026, month=9)
    assert out["data"]["holidays"] == []


# --------------------------------------------------------------------------- #
# LC8 — touch_leave_calendar invalidates 4 scopes × months + publishes realtime
# --------------------------------------------------------------------------- #
def test_lc8_touch_deletes_all_scope_keys_and_publishes(monkeypatch):
    fake = make_fake(monkeypatch)
    fake.db.employee_rows = [
        {"name": "HR-EMP-0001", "employee_name": "Nguyễn Văn A", "status": "Active",
         "company": COMPANY, "branch": "HN", "department": "Sale"},
    ]
    # Plant a cache row for every (branch, dept) scope × 3 months.
    months = [("2026", "08"), ("2026", "09"), ("2026", "10")]
    scopes = [(None, None), ("HN", None), (None, "Sale"), ("HN", "Sale")]
    for (y, m) in months:
        for b, d in scopes:
            key = cal_utils.build_cache_key(company=COMPANY, branch=b, department=d, year=y, month=m)
            fake.db.cache_rows[key] = {
                "name": key, "cache_key": key, "data_json": "{}",
                "generated_at": "2026-08-29 00:00:00",
                "expires_at": "2100-01-01 00:00:00",
            }
    doc = types.SimpleNamespace(employee="HR-EMP-0001", from_date="2026-08-15", to_date="2026-10-05")
    fake.api.touch_leave_calendar(doc)

    assert len(fake.db.deleted) == 12  # 3 tháng × 4 scope
    events = [e for (e, _p) in fake.published]
    assert "leave_calendar_updated" in events


def test_lc8b_touch_without_employee_scope_is_noop(monkeypatch):
    fake = make_fake(monkeypatch)
    doc = types.SimpleNamespace(employee="GHOST", from_date="2026-08-15", to_date="2026-10-05")
    fake.api.touch_leave_calendar(doc)  # không raise
    assert fake.db.deleted == []


# --------------------------------------------------------------------------- #
# LC9 / LC10 — leave.py wiring (hooks + decision path call _touch_calendar)
# --------------------------------------------------------------------------- #
def test_lc9_hooks_call_touch(monkeypatch):
    fake = make_fake(monkeypatch)
    calls = []
    monkeypatch.setattr(fake.leave, "_touch_calendar", lambda doc: calls.append(doc))
    doc = types.SimpleNamespace(employee="HR-EMP-0001", from_date="2026-09-01", to_date="2026-09-02")
    fake.leave.on_leave_submit(doc, "on_submit")
    fake.leave.on_leave_cancel(doc, "on_cancel")
    assert calls == [doc, doc]


def test_lc10_after_decision_calls_touch(monkeypatch):
    fake = make_fake(monkeypatch)
    calls = []
    monkeypatch.setattr(fake.leave, "_touch_calendar", lambda doc: calls.append(doc))
    doc = types.SimpleNamespace(
        employee="HR-EMP-0001", name="LA-1", company=COMPANY,
        from_date="2026-09-01", to_date="2026-09-02",
    )
    fake.leave._after_leave_decision(doc, approved=False, reason="thiếu người")
    assert calls == [doc]
    fake.leave._after_leave_decision(doc, approved=True)
    assert calls == [doc, doc]


# --------------------------------------------------------------------------- #
# LC11 — calendar_employee_options
# --------------------------------------------------------------------------- #
def test_lc11_employee_options_active_search_and_gate(monkeypatch):
    fake = make_fake(monkeypatch)
    fake.db.employee_rows = [
        {"name": "HR-EMP-0001", "employee_name": "Nguyễn Văn A", "status": "Active",
         "department": "Sale", "branch": "HN"},
        {"name": "HR-EMP-0002", "employee_name": "Trần Thị B", "status": "Active",
         "department": None, "branch": "HCM"},
        {"name": "HR-EMP-0003", "employee_name": "Nguyễn C", "status": "Left",
         "department": None, "branch": None},
    ]
    out = fake.api.calendar_employee_options(search="ngu")
    assert [o["value"] for o in out] == ["HR-EMP-0001"]  # Active + tên khớp
    assert out[0]["description"] == "Sale · HN"

    capped = make_fake(monkeypatch)
    capped.db.employee_rows = [
        {"name": f"HR-EMP-{i:04d}", "employee_name": f"NV {i}", "status": "Active",
         "department": None, "branch": None} for i in range(250)
    ]
    assert len(capped.api.calendar_employee_options(limit=999)) == 200  # cap

    denied = make_fake(monkeypatch, roles=("Employee",))
    with pytest.raises(Exception, match="quyền HR"):
        denied.api.calendar_employee_options()


# --------------------------------------------------------------------------- #
# LC13 — legacy (unsuffixed) cache row is never served
# --------------------------------------------------------------------------- #
def test_lc13_legacy_key_row_never_served(monkeypatch):
    fake = make_fake(monkeypatch)
    legacy_key = "Gege Demo-ALL-ALL-2026-09"  # pre-v2 shape (flat array)
    fake.db.cache_rows[legacy_key] = {
        "name": legacy_key, "cache_key": legacy_key,
        "data_json": '[{"name": "LA-OLD"}]',
        "generated_at": "2026-08-29 00:00:00",
        "expires_at": "2100-01-01 00:00:00",
    }
    fake.db.leave_rows = [_leave("LA-NEW")]
    out = fake.api.get_leave_calendar(company=COMPANY, year=2026, month=9)
    assert out["from_cache"] is False  # legacy row ignored → rebuilt live
    assert {r["name"] for r in out["data"]["leaves"]} == {"LA-NEW"}
    assert isinstance(out["data"], dict)  # v2 shape even khi row cũ tồn tại


def test_lc13b_legacy_array_payload_normalized_by_read_filters(monkeypatch):
    """A v1 array served (defensively) still comes back shaped v2 + filtered."""
    fake = make_fake(monkeypatch)
    key = cal_utils.build_cache_key(company=COMPANY, year=2026, month=9)
    fake.db.cache_rows[key] = {
        "name": key, "cache_key": key,
        "data_json": '[{"name": "LA-OLD", "status": "Open", "employee": "HR-EMP-0001", "leave_type": "Annual Leave"}]',
        "generated_at": "2026-08-29 00:00:00",
        "expires_at": "2100-01-01 00:00:00",
    }
    out = fake.api.get_leave_calendar(company=COMPANY, year=2026, month=9, statuses="Approved")
    assert out["from_cache"] is True
    assert out["data"]["leaves"] == []  # Open row lọc bỏ theo statuses
    assert out["data"]["holidays"] == []
