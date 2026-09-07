"""Bench-free unit tests for the desk-free /hr/team/attendance grid
``api/attendance.py::_team_att_cell_can`` + ``team_attendance`` enhancements +
``team_attendance_context`` (plans/plan-team-attendance-desk-free.md §5.1 —
cases TA1…TA28).

Same stub-frappe harness as ``test_team_today.py``: the stub is installed into
``sys.modules`` BEFORE importing/reloading ``gege_hr.gege_hr.api.attendance``.
The DB double here additionally supports the ``["between", [a, b]]`` filter
operator the range grid queries use, plus per-doctype call counting (TA6
batching assertions).
"""

from __future__ import annotations

import importlib
import sys
import types
from datetime import date, datetime

import pytest

PERIOD_DT = "VN Monthly Attendance Period"
WS_DT = "VN Attendance Work Session"
CR_DT = "VN Attendance Correction Request"
OT_DT = "VN Overtime Request"
LA_DT = "Leave Application"
MISS_DT = "VN Checkout Miss"

DAY = "2026-09-15"
MONTH_FIRST = "2026-09-01"


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


def _cmp_scalar(rv, val, op):
    """Compare in str space when either side is a date (ISO ordering)."""
    if isinstance(rv, (date, datetime)) or isinstance(val, (date, datetime)):
        rv, val = str(rv), str(val)
    if op == "=":
        return rv == val
    if op == "!=":
        return rv != val
    if op == "<":
        return rv is not None and rv < val
    if op == "<=":
        return rv is not None and rv <= val
    if op == ">":
        return rv is not None and rv > val
    if op == ">=":
        return rv is not None and rv >= val
    raise ValueError(op)


class _DB:
    """Minimal frappe.db double: dict + list filters (in/between/!=/ranges)
    plus per-doctype get_all counters for the TA6 batching assertions."""

    def __init__(self, fr):
        self.fr = fr
        self.get_all_calls: dict[str, int] = {}

    def _store(self, doctype):
        return self.fr.stores.setdefault(doctype, {})

    def _match_dict(self, row, filters):
        for k, v in filters.items():
            rv = row.get(k)
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                op, val = v[0], v[1]
                if op == "in":
                    if rv not in val:
                        return False
                elif op == "between":
                    lo, hi = val[0], val[1]
                    if rv is None or not (str(lo) <= str(rv) <= str(hi)):
                        return False
                elif not _cmp_scalar(rv, val, op):
                    return False
            elif isinstance(v, (date, datetime)) or isinstance(rv, (date, datetime)):
                if str(rv) != str(v):
                    return False
            elif rv != v:
                return False
        return True

    @staticmethod
    def _match_list(row, filters):
        for cond in filters or []:
            field, op, value = cond[0], cond[1], cond[2]
            if not _cmp_scalar(row.get(field), value, op):
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
        self.get_all_calls[doctype] = self.get_all_calls.get(doctype, 0) + 1
        rows = list(self._store(doctype).values())
        if isinstance(filters, dict):
            rows = [r for r in rows if self._match_dict(r, filters)]
        elif isinstance(filters, list):
            rows = [r for r in rows if self._match_list(r, filters)]
        order = kw.get("order_by")
        if order:
            parts = [p.strip() for p in order.split(",")]
            for p in reversed(parts):
                if " " in p:
                    fld, direction = p.rsplit(" ", 1)
                    rows = sorted(
                        rows,
                        key=lambda r: (r.get(fld) is None, r.get(fld)),
                        reverse=direction == "desc",
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

    def set_value(self, doctype, name, values, **kw):
        store = self._store(doctype)
        store.setdefault(name, {"name": name}).update(values or {})


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
    mod.get_roles = lambda user=None: ("HR Manager",)
    mod.session = types.SimpleNamespace(user="mgr@test.local")
    mod.published = []
    mod.publish_realtime = lambda event, payload=None, **kw: mod.published.append((event, payload))

    utils = types.ModuleType("frappe.utils")
    utils.flt = _flt
    utils.getdate = _getdate
    utils.now_datetime = lambda: datetime(2026, 9, 15, 10, 0, 0)
    utils.today = lambda: DAY
    utils.cint = lambda v: int(v or 0)
    utils.get_datetime = lambda v: v
    utils.add_days = lambda v, n: v

    def _utils_getattr(name):
        def _generic(*_a, **_k):
            return None

        return _generic

    utils.__getattr__ = _utils_getattr
    mod.utils = utils

    mod.stores = {}
    mod.db = _DB(mod)

    def _get_doc(payload=None, *args, **kw):
        if isinstance(payload, str) and args:
            row = mod.stores.get(payload, {}).get(args[0]) or {"name": args[0]}
            return _FakeDoc({**row, "doctype": payload}, mod)
        return _FakeDoc(payload or {}, mod)

    mod.get_doc = _get_doc
    mod.delete_doc = lambda doctype, name: mod.stores.get(doctype, {}).pop(name, None)
    mod.new_doc = lambda doctype: _FakeDoc({"doctype": doctype}, mod)
    return mod, utils


class _FakeDoc:
    """Minimal document double whose ``insert()``/``submit()`` write back into
    the stub stores (mark_attendance_bulk's insert path re-reads them)."""

    def __init__(self, payload, fr):
        object.__setattr__(self, "_fr", fr)
        self.__dict__.update(payload)
        self.name = payload.get("name")
        self.docstatus = payload.get("docstatus", 0) or 0

    def _store(self):
        return self._fr.stores.setdefault(self.__dict__.get("doctype", "Doc"), {})

    def insert(self, **kw):
        if not self.name:
            self.name = f"{self.__dict__.get('doctype', 'DOC')}-{len(self._store()) + 1:04d}"
        self.__dict__["name"] = self.name
        self._store()[self.name] = dict(self.__dict__)
        return self

    def submit(self, **kw):
        self.docstatus = 1
        self._store()[self.name] = dict(self.__dict__)
        return self

    def save(self, **kw):
        if self.name:
            self._store()[self.name] = dict(self.__dict__)
        return self

    def update(self, d):
        self.__dict__.update(d or {})
        return self

    def set(self, key, value):
        self.__dict__[key] = value
        return self


# --------------------------------------------------------------------------- #
# fixture
# --------------------------------------------------------------------------- #
@pytest.fixture
def env(monkeypatch):
    mod, utils = _build_frappe()
    monkeypatch.setitem(sys.modules, "frappe", mod)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)

    att = importlib.import_module("gege_hr.gege_hr.api.attendance")
    importlib.reload(att)

    state = {"roles": ["HR Manager"], "emp": "M1"}

    monkeypatch.setattr(
        att,
        "emp_utils",
        types.SimpleNamespace(
            get_employee_for_user=lambda: state["emp"],
            emp_name=lambda e: (
                e if isinstance(e, str) else ((e or {}).get("name") if isinstance(e, dict) else e)
            ),
            get_user_roles=lambda: list(state["roles"]),
            HR_MANAGER_ROLES={"HR Manager", "System Manager"},
        ),
    )
    _portal_now = datetime(2026, 9, 15, 10, 0, 0)

    def _planned_window(day, s, e):
        s_t = datetime.strptime(str(s)[:5], "%H:%M").time()
        e_t = datetime.strptime(str(e)[:5], "%H:%M").time()
        return datetime.combine(day, s_t), datetime.combine(day, e_t)

    monkeypatch.setattr(
        att,
        "tz_utils",
        types.SimpleNamespace(
            now_in_portal=lambda: _portal_now,
            planned_window=_planned_window,
            wall=lambda v: v,
        ),
    )

    pushed = []

    def _push(**kw):
        pushed.append(kw)
        return f"NOTIF-{len(pushed):03d}"

    monkeypatch.setattr(att, "notify_util", types.SimpleNamespace(push_notification=_push))

    audits = []
    monkeypatch.setattr(
        att,
        "audit_api",
        types.SimpleNamespace(log=lambda action, **kw: audits.append((action, kw)) or "AUD-1"),
    )

    return types.SimpleNamespace(
        att=att, fr=mod, state=state, pushed=pushed, audits=audits, published=mod.published
    )


# --------------------------------------------------------------------------- #
# seeds
# --------------------------------------------------------------------------- #
def _seed_team(fr):
    emps = {
        "M1": {
            "name": "M1",
            "employee_name": "Trưởng Phương",
            "status": "Active",
            "reports_to": None,
            "designation": "Trưởng nhóm",
        },
        "E1": {
            "name": "E1",
            "employee_name": "An Nguyễn",
            "status": "Active",
            "reports_to": "M1",
            "designation": "Sale",
        },
        "E2": {
            "name": "E2",
            "employee_name": "Bình Trần",
            "status": "Active",
            "reports_to": "M1",
            "designation": "Kỹ thuật",
        },
        "E3": {
            "name": "E3",
            "employee_name": "Cường Lê",
            "status": "Active",
            "reports_to": "M2",
            "designation": "Kế toán",
        },
    }
    fr.stores.setdefault("Employee", {}).update(emps)


def _seed_month(fr):
    """September-2026 grid context: shift + assignments + Attendance/WS rows
    for E1 (late + OT day 09-14, clean day 09-15), an On-Leave day for E2
    (approved Leave Application 09-14), pending correction/OT for E1, an open
    Leave Application for E2, an open Checkout-Miss ticket on E1 09-14, and a
    LOCKED period covering 09-01…09-10."""
    fr.stores.setdefault("Shift Type", {})["Ca Sáng"] = {
        "name": "Ca Sáng",
        "start_time": "08:00:00",
        "end_time": "17:00:00",
    }
    sa = fr.stores.setdefault("Shift Assignment", {})
    for emp in ("E1", "E2"):
        sa[f"SA-{emp}"] = {
            "name": f"SA-{emp}",
            "employee": emp,
            "shift_type": "Ca Sáng",
            "status": "Active",
            "start_date": "2026-09-01",
            "end_date": None,
            "docstatus": 1,
        }

    att = fr.stores.setdefault("Attendance", {})
    ws = fr.stores.setdefault(WS_DT, {})
    # E1 — 09-14: 10-min late + 1.0 raw OT (0.5 approved); 09-15: clean.
    att["ATT-E1-14"] = {
        "name": "ATT-E1-14",
        "employee": "E1",
        "attendance_date": "2026-09-14",
        "status": "Present",
        "shift": "Ca Sáng",
        "in_time": "2026-09-14 08:10:00",
        "out_time": "2026-09-14 18:00:00",
        "late_entry": 1,
        "early_exit": 0,
        "docstatus": 1,
    }
    ws["WS-E1-14"] = {
        "name": "WS-E1-14",
        "employee": "E1",
        "work_date": "2026-09-14",
        "actual_checkin": "2026-09-14 08:10:00",
        "actual_checkout": "2026-09-14 18:00:00",
        "late_minutes": 10,
        "early_leave_minutes": 0,
        "raw_overtime_hours": 1.0,
        "approved_overtime_hours": 0.5,
        "missing_checkout": 0,
        "vn_auto_checkout": 0,
        "docstatus": 1,
    }
    att["ATT-E1-15"] = {
        "name": "ATT-E1-15",
        "employee": "E1",
        "attendance_date": "2026-09-15",
        "status": "Present",
        "shift": "Ca Sáng",
        "in_time": "2026-09-15 08:00:00",
        "out_time": "2026-09-15 17:00:00",
        "late_entry": 0,
        "early_exit": 0,
        "docstatus": 1,
    }
    ws["WS-E1-15"] = {
        "name": "WS-E1-15",
        "employee": "E1",
        "work_date": "2026-09-15",
        "actual_checkin": "2026-09-15 08:00:00",
        "actual_checkout": "2026-09-15 17:00:00",
        "late_minutes": 0,
        "early_leave_minutes": 0,
        "raw_overtime_hours": 0,
        "approved_overtime_hours": 0,
        "missing_checkout": 0,
        "vn_auto_checkout": 0,
        "docstatus": 1,
    }
    # E2 — on leave 09-14 (approved LA + an Attendance "On Leave" marker).
    att["ATT-E2-14"] = {
        "name": "ATT-E2-14",
        "employee": "E2",
        "attendance_date": "2026-09-14",
        "status": "On Leave",
        "shift": "Ca Sáng",
        "in_time": None,
        "out_time": None,
        "late_entry": 0,
        "early_exit": 0,
        "docstatus": 1,
    }
    fr.stores.setdefault(LA_DT, {})["LA-E2"] = {
        "name": "LA-E2",
        "employee": "E2",
        "from_date": "2026-09-14",
        "to_date": "2026-09-14",
        "leave_type": "Annual Leave",
        "status": "Approved",
        "docstatus": 1,
    }
    # Pending badges: one correction + one OT for E1; one OPEN leave for E2.
    fr.stores.setdefault(CR_DT, {})["CR-E1"] = {
        "name": "CR-E1",
        "employee": "E1",
        "work_date": "2026-09-14",
        "workflow_state": "Pending HR",
        "docstatus": 0,
    }
    fr.stores.setdefault(OT_DT, {})["OT-E1"] = {
        "name": "OT-E1",
        "employee": "E1",
        "workflow_state": "Pending",
        "docstatus": 0,
    }
    fr.stores.setdefault(LA_DT, {})["LA-E2-2"] = {
        "name": "LA-E2-2",
        "employee": "E2",
        "from_date": "2026-09-20",
        "to_date": "2026-09-20",
        "leave_type": "Annual Leave",
        "status": "Open",
        "docstatus": 0,
    }
    # Open checkout-miss ticket for E1 on 09-14.
    fr.stores.setdefault(MISS_DT, {})["CM-E1"] = {
        "name": "CM-E1",
        "employee": "E1",
        "work_date": "2026-09-14",
        "status": "Pending",
        "docstatus": 0,
    }
    # Locked period 09-01…09-10 → those grid days are read-only.
    fr.stores.setdefault(PERIOD_DT, {})["MAP-2026-09-LOCK"] = {
        "name": "MAP-2026-09-LOCK",
        "from_date": "2026-09-01",
        "to_date": "2026-09-10",
        "status": "Locked",
    }


def _member(res, name):
    return next((m for m in res.get("members", []) if m.get("name") == name), None)


def _day(member, iso):
    return next((d for d in (member or {}).get("days", []) if d.get("work_date") == iso), None)


# --------------------------------------------------------------------------- #
# §5.1 — pure per-day-cell can matrix (plan WP2)
# --------------------------------------------------------------------------- #
def test_ta8_locked_day_all_mutations_false(env):
    """TA8: a Locked date kills every mutation flag for EVERY role (even HR
    Manager + OT approver + open ticket + punches present)."""
    can = env.att._team_att_cell_can(
        is_hr=True,
        is_lm_of=False,
        locked=True,
        is_future=False,
        has_punch=True,
        raw_ot=2.0,
        open_cm=True,
        is_ot_approver=True,
    )
    for key in (
        "fix_punch",
        "delete_punch",
        "mark_attendance",
        "create_request",
        "approve_ot",
        "resolve_checkout_miss",
        "recalc",
    ):
        assert can[key] is False, key
    assert can["view_detail"] is True


def test_ta9_future_day_fix_and_request_false_nudge_true(env):
    """TA9: a future day blocks fix/mark/create but nudge stays available."""
    can = env.att._team_att_cell_can(is_hr=False, is_lm_of=True, is_future=True)
    assert can["fix_punch"] is False
    assert can["delete_punch"] is False
    assert can["mark_attendance"] is False
    assert can["create_request"] is False
    assert can["nudge"] is True


def test_ta10_lm_foreign_member_no_manage(env):
    """TA10: a Line Manager on a NON-report member gets no manage flags."""
    can = env.att._team_att_cell_can(is_hr=False, is_lm_of=False, has_punch=True, raw_ot=1.5, open_cm=True)
    assert can["fix_punch"] is False
    assert can["delete_punch"] is False
    assert can["mark_attendance"] is False
    assert can["create_request"] is False
    assert can["approve_ot"] is False
    assert can["resolve_checkout_miss"] is False
    assert can["recalc"] is False
    assert can["nudge"] is False


def test_ta18_approve_ot_requires_approver_and_raw_ot(env):
    """TA18: approve_ot = OT-approver role AND raw OT > 0 (and not locked)."""
    ok = env.att._team_att_cell_can(is_hr=True, is_lm_of=False, raw_ot=1.5, is_ot_approver=True)
    assert ok["approve_ot"] is True
    no_raw = env.att._team_att_cell_can(is_hr=True, is_lm_of=False, raw_ot=0, is_ot_approver=True)
    assert no_raw["approve_ot"] is False
    no_role = env.att._team_att_cell_can(is_hr=True, is_lm_of=False, raw_ot=1.5, is_ot_approver=False)
    assert no_role["approve_ot"] is False
    locked = env.att._team_att_cell_can(
        is_hr=True, is_lm_of=False, raw_ot=1.5, is_ot_approver=True, locked=True
    )
    assert locked["approve_ot"] is False


def test_ta18b_happy_paths_for_hr_and_lm(env):
    """Companion: HR on a past unlocked day with a punch → everything on;
    LM on own member → manage flags on but mark/recalc/CM/OT off."""
    hr = env.att._team_att_cell_can(
        is_hr=True, is_lm_of=False, has_punch=True, raw_ot=1.0, open_cm=True, is_ot_approver=True
    )
    assert hr["fix_punch"] is True
    assert hr["delete_punch"] is True
    assert hr["mark_attendance"] is True
    assert hr["create_request"] is True
    assert hr["resolve_checkout_miss"] is True
    assert hr["recalc"] is True
    assert hr["nudge"] is True

    lm = env.att._team_att_cell_can(is_hr=False, is_lm_of=True, has_punch=True)
    assert lm["fix_punch"] is True
    assert lm["create_request"] is True
    assert lm["nudge"] is True
    assert lm["mark_attendance"] is False  # HR-only (Employee Attendance Tool parity)
    assert lm["recalc"] is False
    assert lm["approve_ot"] is False


# --------------------------------------------------------------------------- #
# §5.1 — team_attendance grid enhancements (plan WP2)
# --------------------------------------------------------------------------- #
def test_ta6_grid_batched_no_n_plus_one(env):
    """TA6: with a 2-member roster the grid issues exactly 1 membership query +
    1 windowed Attendance query + 1 windowed Work-Session query — never
    per-member loops."""
    _seed_team(env.fr)
    _seed_month(env.fr)
    env.fr.db.get_all_calls.clear()
    res = env.att.team_attendance(manager="M1", from_date="2026-09-01", to_date="2026-09-30")
    assert len(res["members"]) == 2
    calls = env.fr.db.get_all_calls
    assert calls.get("Attendance", 0) == 2  # membership + window (NOT 1 + members)
    assert calls.get(WS_DT, 0) == 1


def test_ta7_grid_server_side_search(env):
    """TA7: ``search`` narrows the roster server-side (name match only)."""
    _seed_team(env.fr)
    _seed_month(env.fr)
    res = env.att.team_attendance(
        manager="M1", from_date="2026-09-01", to_date="2026-09-30", search="an nguyễn"
    )
    assert [m["name"] for m in res["members"]] == ["E1"]


def test_ta11_member_pending_badges(env):
    """TA11: per-member pending badges from the batched request sources."""
    _seed_team(env.fr)
    _seed_month(env.fr)
    res = env.att.team_attendance(manager="M1", from_date="2026-09-01", to_date="2026-09-30")
    assert _member(res, "E1")["pending"] == {
        "corrections": 1,
        "overtime": 1,
        "leaves": 0,
        "checkout_misses": 1,
    }
    assert _member(res, "E2")["pending"]["leaves"] == 1


def test_ta12_legacy_response_shape_preserved(env):
    """TA12: regression — legacy keys + day fields unchanged (enhancements are
    strictly additive)."""
    _seed_team(env.fr)
    _seed_month(env.fr)
    res = env.att.team_attendance(manager="M1", from_date="2026-09-01", to_date="2026-09-30")
    for key in ("from_date", "to_date", "groups", "members", "summary"):
        assert key in res
    for key in ("present", "late", "early", "overtime", "absent", "on_leave"):
        assert key in res["summary"]
    e1 = _member(res, "E1")
    assert len(e1["days"]) == 30
    late_day = _day(e1, "2026-09-14")
    assert late_day["late_minutes"] == 10
    assert late_day["status"] == "Late"
    assert late_day["approved_overtime_hours"] == 0.5
    assert str(late_day["checkin_time"]).startswith("2026-09-14 08:10")
    on_leave = _day(_member(res, "E2"), "2026-09-14")
    assert on_leave["status"] == "On Leave"
    assert res["summary"]["on_leave"] == 1


def test_ta12b_cell_matrix_locks_and_badges_wired(env):
    """Companion: cell extras wired into every branch — locked day read-only
    even for HR; checkout-miss + OT chips on the right cell; future day blocks
    fix but not nudge."""
    _seed_team(env.fr)
    _seed_month(env.fr)
    res = env.att.team_attendance(manager="M1", from_date="2026-09-01", to_date="2026-09-30")
    e1 = _member(res, "E1")
    assert res["period"] == {"name": "MAP-2026-09-LOCK", "status": "Locked"}
    assert res["locked_dates"][0] == "2026-09-01" and res["locked_dates"][-1] == "2026-09-10"
    locked_cell = _day(e1, "2026-09-03")
    assert locked_cell["locked"] is True
    assert locked_cell["can"]["fix_punch"] is False
    assert locked_cell["can"]["recalc"] is False
    late_cell = _day(e1, "2026-09-14")
    assert late_cell["locked"] is False
    assert late_cell["work_session"] == "WS-E1-14"
    assert late_cell["open_checkout_miss"] == "CM-E1"
    assert late_cell["can"]["fix_punch"] is True
    assert late_cell["can"]["approve_ot"] is True  # HR Manager + raw_ot 1.0
    assert late_cell["can"]["resolve_checkout_miss"] is True
    future_cell = _day(e1, "2026-09-16")
    assert future_cell["can"]["fix_punch"] is False
    assert future_cell["can"]["nudge"] is True
    today_cell = _day(e1, "2026-09-15")
    assert today_cell["can"]["fix_punch"] is True


def test_ta25_window_clamp(env):
    """TA25: a window wider than TEAM_ATTENDANCE_MAX_DAYS → ValidationError."""
    _seed_team(env.fr)
    with pytest.raises(_Thrown) as ei:
        env.att.team_attendance(manager="M1", from_date="2026-08-01", to_date="2026-10-31")
    assert ei.value.kind == "Validation"


def test_ta26_member_paging(env):
    """TA26: ``page``/``page_size`` slice the member rows; ``total_members``
    carries the un-paged count."""
    _seed_team(env.fr)
    _seed_month(env.fr)
    res = env.att.team_attendance(
        manager="M1", from_date="2026-09-01", to_date="2026-09-30", page=1, page_size=1
    )
    assert len(res["members"]) == 1
    assert res["total_members"] == 2
    assert res["page"] == 1 and res["page_size"] == 1
    page2 = env.att.team_attendance(
        manager="M1", from_date="2026-09-01", to_date="2026-09-30", page=2, page_size=1
    )
    assert {res["members"][0]["name"], page2["members"][0]["name"]} == {"E1", "E2"}


def test_ta28_cross_month_week_window(env):
    """TA28: a week window rolling into the next month returns every day with
    data (day set = requested window, no month assumptions)."""
    _seed_team(env.fr)
    _seed_month(env.fr)
    res = env.att.team_attendance(manager="M1", from_date="2026-09-28", to_date="2026-10-04")
    days = _member(res, "E1")["days"]
    assert [d["work_date"] for d in days] == [
        "2026-09-28",
        "2026-09-29",
        "2026-09-30",
        "2026-10-01",
        "2026-10-02",
        "2026-10-03",
        "2026-10-04",
    ]
    assert all("can" in d for d in days)


# --------------------------------------------------------------------------- #
# §5.1 — team_attendance_context (plan WP1)
# --------------------------------------------------------------------------- #
def _set_roles(env, roles):
    env.state["roles"] = list(roles)
    env.fr.get_roles = lambda user=None: tuple(env.state["roles"])

    def _only_for(required):
        if not set(required) & set(env.state["roles"]):
            raise _PermErr("not allowed")

    env.fr.only_for = _only_for


def test_ta1_context_line_manager_scope(env):
    """TA1: LM context — team scope, manage flags on, OT approval off."""
    _seed_team(env.fr)
    _seed_month(env.fr)
    _set_roles(env, ["Line Manager", "Employee"])
    res = env.att.team_attendance_context(from_date="2026-09-01", to_date="2026-09-30")
    assert res["scope"] == {"mode": "team", "member_count": 2}
    assert res["viewer_employee"] == "M1"
    assert res["can"]["fix_punch"] is True
    assert res["can"]["approve_ot"] is False
    assert res["can"]["manage_period"] is False


def test_ta2_context_plain_employee_denied(env):
    """TA2: a plain Employee has no team grid → PermissionError."""
    _seed_team(env.fr)
    _set_roles(env, ["Employee"])
    with pytest.raises(_PermErr):
        env.att.team_attendance_context()


def test_ta3_context_hr_manager_company_scope(env):
    """TA3: HR Manager — company scope, mark + manage_period on."""
    _seed_team(env.fr)
    _seed_month(env.fr)
    _set_roles(env, ["HR Manager"])
    res = env.att.team_attendance_context(from_date="2026-09-01", to_date="2026-09-30")
    assert res["scope"]["mode"] == "company"
    assert res["scope"]["member_count"] == 3  # E1 + E2 + E3 (M1 excluded)
    assert res["can"]["mark_attendance"] is True
    assert res["can"]["approve_ot"] is True
    assert res["can"]["manage_period"] is True
    assert "Ca Sáng" in res["filters"]["shift_types"]


def test_ta4_context_locked_period(env):
    """TA4: a Locked period overlapping the window surfaces in the context."""
    _seed_team(env.fr)
    _seed_month(env.fr)
    _set_roles(env, ["HR Manager"])
    res = env.att.team_attendance_context(from_date="2026-09-01", to_date="2026-09-30")
    assert res["period"]["status"] == "Locked"
    assert res["locked_dates"][0] == "2026-09-01"
    assert res["locked_dates"][-1] == "2026-09-10"
    assert res["window"]["max_days"] == 62


def test_ta5_context_pending_approvals_scoped(env):
    """TA5: pending counts honour the viewer scope — an LM never sees a foreign
    member's pending requests."""
    _seed_team(env.fr)
    _seed_month(env.fr)
    # A foreign pending correction (E3 reports to M2, NOT to the LM M1).
    env.fr.stores.setdefault(CR_DT, {})["CR-E3"] = {
        "name": "CR-E3",
        "employee": "E3",
        "work_date": "2026-09-14",
        "workflow_state": "Pending HR",
        "docstatus": 0,
    }
    _set_roles(env, ["Line Manager", "Employee"])
    res = env.att.team_attendance_context(from_date="2026-09-01", to_date="2026-09-30")
    assert res["pending_approvals"] == {
        "corrections": 1,  # CR-E1 only — CR-E3 is out of scope
        "overtime": 1,
        "leaves": 1,
        "checkout_misses": 1,
    }


# --------------------------------------------------------------------------- #
# §5.1 — WP4 Line-Manager gates on attendance_admin_ops (TA13…TA16)
# --------------------------------------------------------------------------- #
@pytest.fixture
def ops_env(monkeypatch):
    mod, utils = _build_frappe()
    monkeypatch.setitem(sys.modules, "frappe", mod)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)

    ops = importlib.import_module("gege_hr.gege_hr.api.attendance_admin_ops")
    importlib.reload(ops)

    state = {"roles": ["HR Manager"], "emp": "M1"}
    mod.get_roles = lambda user=None: tuple(state["roles"])

    # The ops module resolves emp_utils lazily; patch the REAL util module so
    # ``_is_lm_of`` sees the fixture employee regardless of import caching.
    emp_utils_mod = importlib.import_module("gege_hr.gege_hr.utils.employee")
    monkeypatch.setattr(emp_utils_mod, "get_employee_for_user", lambda user=None: state["emp"])

    audits: list = []
    recalcs: list = []
    rec_ws: list = []
    monkeypatch.setattr(ops, "_audit", lambda action, **kw: audits.append((action, kw)))
    monkeypatch.setattr(ops, "_recalc_for_checkin", lambda doc: recalcs.append(getattr(doc, "name", None)))
    monkeypatch.setattr(ops, "_recalc_work_sessions", lambda emp, d: rec_ws.append((emp, d)))
    monkeypatch.setattr(ops, "_commit", lambda: None)

    return types.SimpleNamespace(ops=ops, fr=mod, state=state, audits=audits, recalcs=recalcs, rec_ws=rec_ws)


def test_ta13_mark_bulk_lm_own_team(ops_env):
    """TA13: a Line Manager may bulk-mark their own reports."""
    _seed_team(ops_env.fr)
    ops_env.state["roles"] = ["Line Manager", "Employee"]
    res = ops_env.ops.mark_attendance_bulk(
        employees=["E1", "E2"], attendance_date="2026-09-20", status="Present"
    )
    assert res["created"] == 2
    assert res["skipped"] == 0
    assert all(r["ok"] for r in res["results"])
    marked = [
        r
        for r in ops_env.fr.stores.get("Attendance", {}).values()
        if r.get("attendance_date") == "2026-09-20"
    ]
    assert len(marked) == 2


def test_ta14_mark_bulk_lm_mixed_partial_safe(ops_env):
    """TA14: an LM mixing own + foreign members → foreign rows refused, the
    batch itself is NOT aborted (partial-safe)."""
    _seed_team(ops_env.fr)
    ops_env.state["roles"] = ["Line Manager"]
    res = ops_env.ops.mark_attendance_bulk(
        employees=["E1", "E3"], attendance_date="2026-09-20", status="Absent"
    )
    by_emp = {r["employee"]: r for r in res["results"]}
    assert by_emp["E1"]["ok"] is True
    assert by_emp["E3"]["ok"] is False
    assert res["created"] == 1


def test_ta15_delete_checkin_lm_own_team(ops_env):
    """TA15: an LM deletes a stray punch of their OWN member → ok + audit +
    Work-Session recalc fired."""
    _seed_team(ops_env.fr)
    ops_env.fr.stores.setdefault("Employee Checkin", {})["CH-1"] = {
        "name": "CH-1",
        "employee": "E1",
        "time": "2026-09-14 18:30:00",
        "log_type": "OUT",
        "device_id": "gege_hr-admin",
    }
    ops_env.state["roles"] = ["Line Manager"]
    res = ops_env.ops.delete_checkin(name="CH-1", reason="punch trùng")
    assert res["ok"] is True
    assert "CH-1" not in ops_env.fr.stores["Employee Checkin"]
    assert ops_env.rec_ws and ops_env.rec_ws[0][0] == "E1"
    assert ops_env.audits  # VN Audit Event written


def test_ta15b_delete_checkin_lm_foreign_denied(ops_env):
    """Companion: an LM CANNOT delete a foreign team member's punch."""
    _seed_team(ops_env.fr)
    ops_env.fr.stores.setdefault("Employee Checkin", {})["CH-F"] = {
        "name": "CH-F",
        "employee": "E3",
        "time": "2026-09-14 09:00:00",
        "log_type": "IN",
        "device_id": "x",
    }
    ops_env.state["roles"] = ["Line Manager"]
    with pytest.raises(_Thrown) as ei:
        ops_env.ops.delete_checkin(name="CH-F", reason="thử quyền")
    assert ei.value.kind == "Permission"


def test_ta16_delete_checkin_locked_day_refused(ops_env):
    """TA16: the period lock guard still refuses writes on Locked days (even
    for HR) — the LM gate must not weaken it."""
    _seed_team(ops_env.fr)
    _seed_month(ops_env.fr)  # Locked period 09-01…09-10
    ops_env.fr.stores.setdefault("Employee Checkin", {})["CH-2"] = {
        "name": "CH-2",
        "employee": "E1",
        "time": "2026-09-05 09:00:00",
        "log_type": "IN",
        "device_id": "x",
    }
    with pytest.raises(_Thrown) as ei:
        ops_env.ops.delete_checkin(name="CH-2", reason="xoá punch kỳ khoá")
    assert ei.value.kind == "Validation"
    assert "CH-2" in ops_env.fr.stores["Employee Checkin"]  # untouched


# --------------------------------------------------------------------------- #
# §5.1 — WP9 export + realtime (TA20…TA22) · WP5 on-behalf (TA27) ·
# WP3 day-detail reuse (TA24) · recalc gate (TA23)
# --------------------------------------------------------------------------- #
def test_ta20_export_csv_employee_denied(env):
    """TA20: a plain Employee cannot export the team grid."""
    _seed_team(env.fr)
    _seed_month(env.fr)
    _set_roles(env, ["Employee"])
    with pytest.raises(_PermErr):
        env.att.team_attendance_export_csv(from_date="2026-09-01", to_date="2026-09-30")


def test_ta21_export_csv_content(env):
    """TA21: CSV carries the BOM, the header and one row per member-day with
    the grid's own numbers (export == grid)."""
    _seed_team(env.fr)
    _seed_month(env.fr)
    res = env.att.team_attendance_export_csv(from_date="2026-09-01", to_date="2026-09-30")
    assert res["filename"] == "team-attendance-2026-09.csv"
    assert res["csv"].startswith("\ufeff")
    lines = res["csv"].lstrip("\ufeff").splitlines()
    assert lines[0].startswith("Mã NV")
    assert res["rows"] == 2 * 30  # 2 roster members × 30 days
    late_row = next(l for l in lines if l.startswith("E1,") and ",2026-09-14," in l)
    assert ",Late," in late_row
    assert "0.5" in late_row  # approved OT hours carried through


def test_ta22_publish_fires_after_mutations(ops_env, monkeypatch):
    """TA22: punch delete + bulk mark publish the team-attendance realtime
    event context (employee, work_date) via the WP9 indirection."""
    _seed_team(ops_env.fr)
    published: list = []
    monkeypatch.setattr(ops_env.ops, "_publish_att_updated", lambda emp, d: published.append((emp, d)))
    ops_env.fr.stores.setdefault("Employee Checkin", {})["CH-1"] = {
        "name": "CH-1",
        "employee": "E1",
        "time": "2026-09-14 18:30:00",
        "log_type": "OUT",
        "device_id": "gege_hr-admin",
    }
    ops_env.ops.delete_checkin(name="CH-1", reason="punch trùng")
    assert published == [("E1", "2026-09-14")]
    ops_env.ops.mark_attendance_bulk(employees=["E1"], attendance_date="2026-09-20", status="Present")
    assert published[-1] == (None, "2026-09-20")


def test_ta23_recalculate_work_session_lm_denied(env):
    """TA23: Line Manager cannot trigger the recalc engine (HR-only parity)."""
    _seed_team(env.fr)
    _set_roles(env, ["Line Manager"])
    with pytest.raises(_PermErr):
        env.att.recalculate_work_session(work_session="WS-E1-14")


def test_ta24_member_day_detail_any_past_date(env):
    """TA24: the Team-Today drawer endpoint works for ANY date (the range grid
    reuses it) — lock-aware + not-today aware."""
    _seed_team(env.fr)
    _seed_month(env.fr)
    checkins = env.fr.stores.setdefault("Employee Checkin", {})
    checkins["CH-14-IN"] = {
        "name": "CH-14-IN",
        "employee": "E1",
        "time": "2026-09-14 08:10:00",
        "log_type": "IN",
        "device_id": "gege_hr-admin",
    }
    checkins["CH-14-OUT"] = {
        "name": "CH-14-OUT",
        "employee": "E1",
        "time": "2026-09-14 18:00:00",
        "log_type": "OUT",
        "device_id": "gege_hr-admin",
    }
    res = env.att.team_member_day_detail(employee="E1", date_str="2026-09-14")
    assert res["employee"] == "E1"
    assert res.get("punches") is not None and len(res["punches"]) == 2
    assert res["can"]["view_detail"] is True
    assert res["can"]["fix_punch"] is True  # unlocked past day, HR viewer
    assert res["can"]["override_shift"] is False  # past day ≠ today
    locked = env.att.team_member_day_detail(employee="E1", date_str="2026-09-03")
    assert locked["can"]["fix_punch"] is False  # Locked period day


def test_ta27_correction_on_behalf_gates(env, monkeypatch):
    """TA27: submit_correction_request on-behalf — HR ok, LM-of-own ok (WP5),
    Employee-for-another → PermissionError."""
    _seed_team(env.fr)
    _seed_month(env.fr)
    monkeypatch.setattr(env.att, "send_for_approval", lambda doc: None)

    # HR Manager on behalf of E1
    res = env.att.submit_correction_request(
        employee="E1", work_date="2026-09-14", correction_type="missing_checkout", reason="quên chấm ra"
    )
    assert res["status"] == "Draft"

    # Line Manager (M1) on behalf of their own report E1
    env.state["roles"] = ["Line Manager", "Employee"]
    res2 = env.att.submit_correction_request(
        employee="E1", work_date="2026-09-14", correction_type="missing_checkout", reason="hộ nhân viên"
    )
    assert res2["status"] == "Draft"

    # Plain Employee (E1) filing for someone else (E2) → denied
    env.state["roles"] = ["Employee"]
    env.state["emp"] = "E1"
    with pytest.raises(_Thrown) as ei:
        env.att.submit_correction_request(
            employee="E2", work_date="2026-09-14", correction_type="missing_checkout", reason="thử quyền"
        )
    assert ei.value.kind == "Permission"
