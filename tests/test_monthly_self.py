"""Bench-free unit tests for the employee self-service month endpoints
``api/attendance.py::my_month_meta`` + ``my_day_detail``
(plans/plan-monthly-attendance-self-deskfree.md §5.1 — TC-B01…TC-B12).

Same stub-frappe harness as ``test_attendance_admin_ops.py`` /
``test_attendance_period_admin.py``: the stub is installed into
``sys.modules`` BEFORE importing ``gege_hr.gege_hr.api.attendance`` so the
module's top-level ``import frappe`` / ``from frappe.utils import …`` resolve
against the fake. ``emp_utils`` / ``tz_utils`` indirections are monkeypatched
on the module object (auto-restored).
"""

from __future__ import annotations

import calendar
import importlib
import sys
import types
from datetime import date, datetime

import pytest

PERIOD_DT = "VN Monthly Attendance Period"
WS_DT = "VN Attendance Work Session"
CR_DT = "VN Attendance Correction Request"
OT_DT = "VN Overtime Request"


# --------------------------------------------------------------------------- #
# frappe stub
# --------------------------------------------------------------------------- #
class _AttrDict(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            return None


class _PermErr(Exception):
    pass


class _ValidationErr(Exception):
    pass


class _Thrown(Exception):
    """Captures a frappe.throw call."""

    def __init__(self, message, kind):
        super().__init__(message)
        self.message = str(message)
        self.kind = kind


_EXC_KIND = {_PermErr: "Permission", _ValidationErr: "Validation"}


def _flt(v, precision=None):
    try:
        n = float(v or 0)
    except (TypeError, ValueError):
        n = 0.0
    return round(n, precision) if precision is not None else n


def _getdate(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


class _DB:
    """Minimal frappe.db double with dict + list filters (incl. between/!=)."""

    def __init__(self, fr):
        self.fr = fr

    def _store(self, doctype):
        return self.fr.stores.setdefault(doctype, {})

    @staticmethod
    def _match_dict(row, filters):
        for k, v in filters.items():
            rv = row.get(k)
            # date/datetime filter values compare in ISO-string space (mirrors
            # frappe serialising date filters against SQL strings).
            if isinstance(v, (date, datetime)) or isinstance(rv, (date, datetime)):
                if str(rv) != str(v):
                    return False
                continue
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                op, val = v[0], v[1]
                if op == "between":
                    lo, hi = val
                    if not (rv is not None and str(lo) <= str(rv) <= str(hi)):
                        return False
                elif op == "!=" and rv == val:
                    return False
                elif op == "<" and not (rv is not None and rv < val):
                    return False
                elif op == "<=" and not (rv is not None and rv <= val):
                    return False
                elif op == ">" and not (rv is not None and rv > val):
                    return False
                elif op == ">=" and not (rv is not None and rv >= val):
                    return False
            elif rv != v:
                return False
        return True

    @staticmethod
    def _match_list(row, filters):
        for cond in filters or []:
            field, op, value = cond[0], cond[1], cond[2]
            rv = row.get(field)
            if op == "=" and rv != value:
                return False
            if op == "!=" and rv == value:
                return False
            if op == ">=" and not (rv is not None and rv >= value):
                return False
            if op == "<" and not (rv is not None and rv < value):
                return False
            if op == "<=" and not (rv is not None and rv <= value):
                return False
        return True

    def get_value(self, doctype, key, fields=None, as_dict=False):
        store = self._store(doctype)
        row = None
        if isinstance(key, dict):
            for r in store.values():
                if self._match_dict(r, key):
                    row = r
                    break
        else:
            row = store.get(key)
        if row is None:
            return None
        if isinstance(fields, str):
            return row.get(fields)
        if isinstance(fields, list):
            d = {f: row.get(f) for f in fields}
            return _AttrDict(d) if as_dict else (d[fields[0]] if len(fields) == 1 else d)
        return _AttrDict(row) if as_dict else dict(row)

    def get_all(self, doctype, filters=None, fields=None, pluck=None, **kw):
        rows = list(self._store(doctype).values())
        if isinstance(filters, dict):
            rows = [r for r in rows if self._match_dict(r, filters)]
        elif isinstance(filters, list):
            rows = [r for r in rows if self._match_list(r, filters)]
        # order_by "creation desc" / "from_date desc" / "docstatus desc, …"
        order = kw.get("order_by")
        if order:
            parts = [p.strip() for p in order.split(",")]
            for p in reversed(parts):
                if " " in p:
                    fld, direction = p.rsplit(" ", 1)
                    rows = sorted(
                        rows, key=lambda r: (r.get(fld) is None, r.get(fld)), reverse=direction == "desc"
                    )
        if kw.get("limit_page_length"):
            rows = rows[: kw["limit_page_length"]]
        if pluck:
            return [r.get(pluck) for r in rows]
        if fields:
            return [_AttrDict({f: r.get(f) for f in fields}) for r in rows]
        return [_AttrDict(dict(r)) for r in rows]

    def exists(self, doctype, name):
        store = self._store(doctype)
        if isinstance(name, dict):
            return any(self._match_dict(r, name) for r in store.values()) or None
        return name if name in store else None

    def count(self, doctype, filters=None):
        rows = list(self._store(doctype).values())
        if isinstance(filters, dict):
            rows = [r for r in rows if self._match_dict(r, filters)]
        return len(rows)


def _build_frappe():
    mod = types.ModuleType("frappe")
    mod._ = lambda s, *a, **k: s.format(*a) if a else s
    mod.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)
    mod.PermissionError = _PermErr
    mod.ValidationError = _ValidationErr
    mod.throw = lambda message, exc=None: (_ for _ in ()).throw(
        _Thrown(message, _EXC_KIND.get(exc, "Generic"))
    )
    mod.only_for = lambda roles: None
    mod.log_error = lambda *a, **k: None
    mod.get_roles = lambda user=None: ("Employee",)
    mod.session = types.SimpleNamespace(user="emp@test.local")

    utils = types.ModuleType("frappe.utils")
    utils.flt = _flt
    utils.getdate = _getdate
    utils.now_datetime = lambda: datetime(2026, 9, 15, 10, 0, 0)
    utils.today = lambda: "2026-09-15"
    utils.cint = lambda v: int(v or 0)

    def _utils_getattr(name):
        def _generic(*_a, **_k):
            return None

        return _generic

    utils.__getattr__ = _utils_getattr
    mod.utils = utils

    # The fake frappe module IS the shared store surface (stores + db double).
    mod.stores = {}
    mod.db = _DB(mod)
    return mod, utils, mod


# --------------------------------------------------------------------------- #
# fixture
# --------------------------------------------------------------------------- #
@pytest.fixture
def env(monkeypatch):
    mod, utils, fr = _build_frappe()
    monkeypatch.setitem(sys.modules, "frappe", mod)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)

    att = importlib.import_module("gege_hr.gege_hr.api.attendance")
    importlib.reload(att)

    # Employee-resolution + clock indirections — deterministic, no bench.
    monkeypatch.setattr(
        att,
        "emp_utils",
        types.SimpleNamespace(
            get_employee_for_user=lambda: "E1",
            emp_name=lambda e: e,
            get_user_roles=lambda: [],
            HR_MANAGER_ROLES={"HR Manager", "System Manager"},
        ),
    )
    monkeypatch.setattr(
        att, "tz_utils", types.SimpleNamespace(now_in_portal=lambda: datetime(2026, 9, 15, 10, 0, 0))
    )
    return att, fr


def _seed_period(fr, status, from_date="2026-09-01", to_date="2026-09-30", name="MAP-2609-01"):
    fr.stores.setdefault(PERIOD_DT, {})[name] = {
        "name": name,
        "from_date": from_date,
        "to_date": to_date,
        "status": status,
        "docstatus": 1,
    }


def _seed_ws(fr, **over):
    row = {
        "name": "WS-1",
        "employee": "E1",
        "work_date": "2026-09-10",
        "shift_type": "Ca hành chính",
        "shift_instance": "SI-1",
        "planned_start": "2026-09-10 08:00:00",
        "planned_end": "2026-09-10 17:00:00",
        "actual_checkin": "2026-09-10 08:10:00",
        "actual_checkout": "2026-09-10 19:00:00",
        "late_minutes": 10,
        "early_leave_minutes": 0,
        "regular_hours": 8,
        "total_actual_hours": 8.5,
        "raw_overtime_hours": 2.0,
        "approved_overtime_hours": 0,
        "payable_day": 1.0,
        "absent": 0,
        "has_leave": 0,
        "need_review": 0,
        "missing_checkin": 0,
        "missing_checkout": 0,
        "vn_auto_checkout": 0,
    }
    row.update(over)
    fr.stores.setdefault(WS_DT, {})[row["name"]] = row
    return row


def _seed_punches(fr):
    fr.stores.setdefault("Employee Checkin", {}).update(
        {
            "EC-IN": {
                "name": "EC-IN",
                "employee": "E1",
                "time": "2026-09-10 08:10:00",
                "log_type": "IN",
                "device_id": "gege_hr-mobile",
                "latitude": None,
                "longitude": None,
            },
            "EC-OUT": {
                "name": "EC-OUT",
                "employee": "E1",
                "time": "2026-09-10 19:00:00",
                "log_type": "OUT",
                "device_id": "gege_hr-mobile",
                "latitude": None,
                "longitude": None,
            },
        }
    )


def _seed_cr(fr, state="Pending Manager", name="CR-1"):
    fr.stores.setdefault(CR_DT, {})[name] = {
        "name": name,
        "employee": "E1",
        "work_date": "2026-09-10",
        "correction_type": "checkin",
        "reason": "sửa giờ vào",
        "workflow_state": state,
        "docstatus": 0,
    }


def _seed_ot(fr, name="OT-1"):
    fr.stores.setdefault(OT_DT, {})[name] = {
        "name": name,
        "employee": "E1",
        "work_date": "2026-09-10",
        "from_datetime": "2026-09-10 17:00:00",
        "to_datetime": "2026-09-10 19:00:00",
        "requested_hours": 2.0,
        "workflow_state": "Pending HR",
        "docstatus": 0,
    }


def _sundays(y, m):
    return sum(1 for d in range(1, calendar.monthrange(y, m)[1] + 1) if date(y, m, d).weekday() == 6)


# --------------------------------------------------------------------------- #
# my_month_meta — TC-B01..B05, B11
# --------------------------------------------------------------------------- #
class TestMyMonthMeta:
    def test_tcb01_locked_period(self, env):
        att, fr = env
        _seed_period(fr, "Locked")
        meta = att.my_month_meta(year=2026, month=9)
        assert meta["locked"] is True
        assert meta["period"]["name"] == "MAP-2609-01"
        assert meta["period"]["status"] == "Locked"

    def test_tcb02_draft_and_generated_periods(self, env):
        att, fr = env
        _seed_period(fr, "Generated")
        assert att.my_month_meta(year=2026, month=9)["locked"] is False
        fr.stores[PERIOD_DT] = {}
        _seed_period(fr, "Draft", name="MAP-2609-02")
        meta = att.my_month_meta(year=2026, month=9)
        assert meta["locked"] is False and meta["period"]["status"] == "Draft"

    def test_tcb03_no_period(self, env):
        att, fr = env
        meta = att.my_month_meta(year=2026, month=9)
        assert meta["period"] is None and meta["locked"] is False

    def test_tcb04_holidays_and_standard_days(self, env, monkeypatch):
        att, fr = env
        calc = importlib.import_module("gege_hr.gege_hr.utils.calc")
        monkeypatch.setattr(
            calc,
            "load_holiday_dates",
            lambda sd, ed, emp: {date(2026, 9, 1), date(2026, 9, 2)},
        )
        meta = att.my_month_meta(year=2026, month=9)
        assert meta["holidays"] == ["2026-09-01", "2026-09-02"]
        days = calendar.monthrange(2026, 9)[1]
        assert meta["standard_days"] == days - _sundays(2026, 9) - 2

    def test_tcb05_ws_summary(self, env):
        att, fr = env
        _seed_ws(fr, name="WS-1")  # present + late 10' + OT raw 2 (approved 0)
        _seed_ws(
            fr,
            name="WS-2",
            work_date="2026-09-11",
            actual_checkin=None,
            actual_checkout=None,
            late_minutes=0,
            payable_day=0,
            absent=0,
            has_leave=1,
            raw_overtime_hours=0,
        )
        _seed_ws(
            fr,
            name="WS-3",
            work_date="2026-09-12",
            payable_day=0.5,
            late_minutes=0,
            raw_overtime_hours=0,
            approved_overtime_hours=1.5,
        )
        # September has 30 days — WS-2 (leave, no punches) + WS-3 (half day).
        fr.stores.setdefault("Attendance", {})["ATT-1"] = {
            "name": "ATT-1",
            "employee": "E1",
            "attendance_date": "2026-09-10",
            "status": "Present",
        }
        meta = att.my_month_meta(year=2026, month=9)
        s = meta["ws_summary"]
        assert s["worked_days"] == 2  # WS-1 + WS-3 have actual punches
        assert s["late_count"] == 1
        assert s["payable_days"] == 1.5
        assert s["leave_days"] == 1
        assert s["absent_count"] == 0
        assert s["overtime_hours"] == 3.5  # WS-1 raw 2.0 (approved 0) + WS-3 approved 1.5
        assert meta["has_attendance"] is True

    def test_tcb11_month_string_parse(self, env):
        att, fr = env
        meta = att.my_month_meta(month="2026-09")
        assert (meta["year"], meta["month"]) == (2026, 9)

    def test_tcb10_idor_other_employee(self, env, monkeypatch):
        att, fr = env
        monkeypatch.setattr(
            att,
            "emp_utils",
            types.SimpleNamespace(
                get_employee_for_user=lambda: "E1",
                emp_name=lambda e: e,
                get_user_roles=lambda: [],
                HR_MANAGER_ROLES={"HR Manager", "System Manager"},
            ),
        )
        with pytest.raises(_Thrown) as ei:
            att.my_month_meta(employee="E9", year=2026, month=9)
        assert ei.value.kind == "Permission"
        with pytest.raises(_Thrown) as ei:
            att.my_day_detail(employee="E9", work_date="2026-09-10")
        assert ei.value.kind == "Permission"


# --------------------------------------------------------------------------- #
# my_day_detail — TC-B06..B09, B12
# --------------------------------------------------------------------------- #
class TestMyDayDetail:
    def test_tcb06_full_sections(self, env):
        att, fr = env
        _seed_ws(fr)
        _seed_punches(fr)
        _seed_cr(fr)
        _seed_ot(fr)
        d = att.my_day_detail(work_date="2026-09-10")
        assert d["work_date"] == "2026-09-10"
        assert d["work_session"]["name"] == "WS-1"
        assert d["work_session"]["status"] == "Present"
        assert d["work_session"]["shift_instance"] == "SI-1"
        assert [p["name"] for p in d["punches"]] == ["EC-IN", "EC-OUT"]  # asc
        assert d["punches"][0]["log_type"] == "IN"
        assert d["corrections"][0]["workflow_state"] == "Pending Manager"
        assert d["overtime_requests"][0]["name"] == "OT-1"
        assert d["attendance"] is None

    def test_tcb07_empty_day(self, env):
        att, fr = env
        d = att.my_day_detail(work_date="2026-09-20")
        assert d["work_session"] is None
        assert d["punches"] == [] and d["corrections"] == []
        assert d["overtime_requests"] == [] and d["attendance"] is None
        assert d["locked"] is False

    def test_tcb08_attendance_prefers_submitted(self, env):
        att, fr = env
        fr.stores.setdefault("Attendance", {}).update(
            {
                "ATT-D": {
                    "name": "ATT-D",
                    "employee": "E1",
                    "attendance_date": "2026-09-10",
                    "status": "Present",
                    "docstatus": 0,
                },
                "ATT-S": {
                    "name": "ATT-S",
                    "employee": "E1",
                    "attendance_date": "2026-09-10",
                    "status": "Present",
                    "docstatus": 1,
                    "in_time": "2026-09-10 08:00:00",
                    "out_time": "2026-09-10 17:00:00",
                    "late_entry": 1,
                    "early_exit": 0,
                    "working_hours": 8.0,
                },
            }
        )
        d = att.my_day_detail(work_date="2026-09-10")
        assert d["attendance"]["name"] == "ATT-S"
        assert d["attendance"]["docstatus"] == 1 and d["attendance"]["late_entry"] == 1

    def test_tcb09_overnight_punch_framing(self, env):
        """Portal-wall framing (same as _checkins_for): a 00:10 punch on d+1
        belongs to d+1's payload, not d's — mirrors how the admin drawer and
        the engine's portal-day window frame days (documented deviation)."""
        att, fr = env
        fr.stores.setdefault("Employee Checkin", {})["EC-N"] = {
            "name": "EC-N",
            "employee": "E1",
            "time": "2026-09-11 00:10:00",
            "log_type": "OUT",
        }
        d1 = att.my_day_detail(work_date="2026-09-10")
        assert all(p["name"] != "EC-N" for p in d1["punches"])
        d2 = att.my_day_detail(work_date="2026-09-11")
        assert [p["name"] for p in d2["punches"]] == ["EC-N"]

    def test_tcb09b_punch_isolation(self, env):
        att, fr = env
        fr.stores.setdefault("Employee Checkin", {})["EC-X"] = {
            "name": "EC-X",
            "employee": "E2",
            "time": "2026-09-10 09:00:00",
            "log_type": "IN",
        }
        d = att.my_day_detail(work_date="2026-09-10")
        assert all(p["name"] != "EC-X" for p in d["punches"])

    def test_tcb12_invalid_date(self, env):
        att, fr = env
        with pytest.raises(_Thrown) as ei:
            att.my_day_detail(work_date="rác")
        assert ei.value.kind == "Validation"
        with pytest.raises(_Thrown):
            att.my_day_detail(work_date=None)
