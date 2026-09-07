"""Bench-free unit tests for the Team Today desk-free endpoints
``api/attendance.py::team_daily_status`` (v2) + ``team_member_day_detail`` +
``nudge_team_member`` + ``export_team_day_csv``
(plans/team-today-desk-free.md §3.1 — TT-01…TT-14 + IDOR-TT1/2).

Same stub-frappe harness as ``test_monthly_self.py``: the stub is installed
into ``sys.modules`` BEFORE importing/reloading ``gege_hr.gege_hr.api.attendance``
so the module's top-level ``import frappe`` resolves against the fake.
``emp_utils`` / ``tz_utils`` / ``notify_util`` / ``audit_api`` / ``rate_limit``
indirections are monkeypatched on the module object (auto-restored). The DB
double here additionally supports the ``["in", values]`` filter operator and
date-normalised list comparisons the team batch loaders use.
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
MISS_DT = "VN Checkout Miss"

DAY = "2026-09-15"


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
    """Minimal frappe.db double with dict + list filters (in/between/!=/ranges)."""

    def __init__(self, fr):
        self.fr = fr

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
    return mod, utils


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
    monkeypatch.setattr(
        att, "tz_utils", types.SimpleNamespace(now_in_portal=lambda: datetime(2026, 9, 15, 10, 0, 0))
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

    rl = {"n": 0, "keys": []}

    def _rate_limit(key, max_requests=1, window_seconds=3):
        rl["n"] += 1
        rl["keys"].append(key)
        if rl["n"] > max_requests:
            raise _Thrown("Quá nhiều yêu cầu.", "Validation")

    monkeypatch.setattr(att, "rate_limit", _rate_limit)

    return types.SimpleNamespace(
        att=att, fr=mod, state=state, pushed=pushed, audits=audits, rl=rl, published=mod.published
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
        "E4": {
            "name": "E4",
            "employee_name": "Dũng Phạm",
            "status": "Active",
            "reports_to": "M1",
            "designation": "Thực tập",
        },
    }
    fr.stores.setdefault("Employee", {}).update(emps)


def _seed_attendance(fr):
    fr.stores.setdefault("Attendance", {})["ATT-E1"] = {
        "name": "ATT-E1",
        "employee": "E1",
        "attendance_date": DAY,
        "status": "Present",
        "in_time": f"{DAY} 08:10:00",
        "out_time": f"{DAY} 17:05:00",
        "late_entry": 1,
        "early_exit": 0,
        "docstatus": 1,
        "creation": f"{DAY} 18:00:00",
    }


def _seed_ws(fr):
    base = {
        "shift_type": "Ca hành chính",
        "shift_instance": "SI-1",
        "planned_start": f"{DAY} 08:00:00",
        "planned_end": f"{DAY} 17:00:00",
        "regular_hours": 8,
        "payable_day": 1.0,
        "raw_overtime_hours": 0,
        "approved_overtime_hours": 0,
        "need_review": 0,
        "vn_auto_checkout": 0,
        "creation": f"{DAY} 12:00:00",
        "docstatus": 1,
    }
    fr.stores.setdefault(WS_DT, {})["WS-E1"] = {
        **base,
        "name": "WS-E1",
        "employee": "E1",
        "work_date": DAY,
        "actual_checkin": f"{DAY} 08:10:00",
        "actual_checkout": f"{DAY} 17:05:00",
        "late_minutes": 10,
        "early_leave_minutes": 0,
        "total_actual_hours": 8.0,
        "absent": 0,
        "has_leave": 0,
        "missing_checkin": 0,
        "missing_checkout": 0,
    }
    fr.stores[WS_DT]["WS-E2"] = {
        **base,
        "name": "WS-E2",
        "employee": "E2",
        "work_date": DAY,
        "actual_checkin": None,
        "actual_checkout": None,
        "late_minutes": 0,
        "early_leave_minutes": 0,
        "total_actual_hours": 0,
        "absent": 0,
        "has_leave": 0,
        "missing_checkin": 1,
        "missing_checkout": 1,
    }


def _seed_requests(fr):
    fr.stores.setdefault(CR_DT, {})["CR-E1"] = {
        "name": "CR-E1",
        "employee": "E1",
        "work_date": DAY,
        "workflow_state": "Pending HR",
        "docstatus": 0,
    }
    fr.stores[CR_DT]["CR-E1-DONE"] = {
        "name": "CR-E1-DONE",
        "employee": "E1",
        "work_date": DAY,
        "workflow_state": "Approved",
        "docstatus": 1,
    }
    fr.stores.setdefault(OT_DT, {})["OT-E1"] = {
        "name": "OT-E1",
        "employee": "E1",
        "work_date": DAY,
        "workflow_state": "Pending HR",
        "docstatus": 0,
        "from_datetime": f"{DAY} 18:00:00",
    }
    fr.stores.setdefault("Leave Application", {})["LA-E2"] = {
        "name": "LA-E2",
        "employee": "E2",
        "from_date": DAY,
        "to_date": DAY,
        "status": "Open",
        "docstatus": 0,
        "leave_type": "Nghỉ phép",
    }
    fr.stores["Leave Application"]["LA-E1-APPROVED"] = {
        "name": "LA-E1-APPROVED",
        "employee": "E1",
        "from_date": DAY,
        "to_date": DAY,
        "status": "Approved",
        "docstatus": 1,
        "leave_type": "Nghỉ phép",
    }


def _seed_shift_and_miss(fr):
    fr.stores.setdefault("Shift Assignment", {}).update(
        {
            "SA-E1": {
                "name": "SA-E1",
                "employee": "E1",
                "status": "Active",
                "docstatus": 1,
                "shift_type": "Ca hành chính",
                "start_date": "2026-09-01",
                "end_date": "2026-12-31",
            },
            "SA-E1-OLD": {
                "name": "SA-E1-OLD",
                "employee": "E1",
                "status": "Active",
                "docstatus": 1,
                "shift_type": "Ca cũ",
                "start_date": "2026-01-01",
                "end_date": "2026-08-31",
            },
        }
    )
    fr.stores.setdefault(MISS_DT, {})["CM-E2"] = {
        "name": "CM-E2",
        "employee": "E2",
        "work_date": DAY,
        "status": "Pending",
        "docstatus": 0,
    }


def _seed_locked(fr):
    fr.stores.setdefault(PERIOD_DT, {})["MAP-2609"] = {
        "name": "MAP-2609",
        "from_date": "2026-09-01",
        "to_date": "2026-09-30",
        "status": "Locked",
        "docstatus": 1,
    }


def _seed_all(env):
    _seed_team(env.fr)
    _seed_attendance(env.fr)
    _seed_ws(env.fr)
    _seed_requests(env.fr)
    _seed_shift_and_miss(env.fr)
    return env


# --------------------------------------------------------------------------- #
# TT-01…TT-14 + IDOR
# --------------------------------------------------------------------------- #
def test_tt01_team_daily_status_shape(_env_for_roster):
    env = _env_for_roster
    res = env.att.team_daily_status(date_str=DAY)
    assert isinstance(res, dict)
    assert res["work_date"] == DAY
    assert res["locked"] is False
    assert set(res) == {"work_date", "locked", "summary", "members"}
    assert isinstance(res["members"], list)
    assert {m["name"] for m in res["members"]} == {"E1", "E2", "E4"}


def test_tt02_summarize_team_pure(env):
    rows = [
        {"status": "Present"},
        {"status": "Late"},
        {"status": "Late"},
        {"status": "Absent", "late_minutes": 30},  # absent không đếm late
        {"status": "On Leave"},
        {"status": "Half Day"},
        {"status": "Not Checked In"},
        {"status": None},
    ]
    s = env.att.summarize_team(rows)
    assert s == {
        "total": 8,
        "present": 1,
        "late": 2,
        "absent": 1,
        "on_leave": 2,
        "not_checked_in": 2,
    }


def test_tt03_line_manager_scope(_env_for_roster):
    env = _env_for_roster
    env.state["roles"] = ["Line Manager"]
    env.state["emp"] = "M1"
    res = env.att.team_daily_status(date_str=DAY)
    assert {m["name"] for m in res["members"]} == {"E1", "E2", "E4"}  # E3 (team M2) + M1 bị loại


def test_tt04_line_manager_allowed(_env_for_roster):
    env = _env_for_roster
    env.state["roles"] = ["Line Manager"]
    env.state["emp"] = "M1"
    res = env.att.team_daily_status(date_str=DAY)  # fix D2 — không còn 403
    assert res["summary"]["total"] == 3


def test_tt05_plain_employee_denied(env):
    env.state["roles"] = ["Employee"]
    with pytest.raises(_Thrown) as ei:
        env.att.team_daily_status(date_str=DAY)
    assert ei.value.kind == "Permission"


def test_tt06_search_status_server_side(_env_for_roster):
    env = _env_for_roster
    res = env.att.team_daily_status(date_str=DAY, search="bình")
    assert {m["name"] for m in res["members"]} == {"E2"}
    res = env.att.team_daily_status(date_str=DAY, status="Not Checked In")
    assert {m["name"] for m in res["members"]} == {"E2", "E4"}  # E2 thiếu punch, E4 không dữ liệu
    assert res["summary"]["not_checked_in"] == 2
    with pytest.raises(_Thrown) as ei:
        env.att.team_daily_status(date_str=DAY, status="Bogus")
    assert ei.value.kind == "Validation"


def test_tt07_pending_counts(_env_for_roster):
    env = _env_for_roster
    res = env.att.team_daily_status(date_str=DAY)
    by = {m["name"]: m for m in res["members"]}
    assert by["E1"]["pending_counts"] == {"corrections": 1, "overtime": 1, "leaves": 0}
    assert by["E2"]["pending_counts"] == {"corrections": 0, "overtime": 0, "leaves": 1}
    assert by["E1"]["has_checkout_miss"] is False
    assert by["E2"]["has_checkout_miss"] is True
    assert by["E1"]["leave_application"]["name"] == "LA-E1-APPROVED"
    assert by["E1"]["shift_type_name"] == "Ca hành chính"  # SA hết hạn bị bỏ qua


def test_tt08_team_member_day_detail_full_shape(_env_for_roster):
    env = _env_for_roster
    env.state["roles"] = ["Line Manager"]
    env.state["emp"] = "M1"
    res = env.att.team_member_day_detail(employee="E1", date_str=DAY)
    for key in (
        "work_date",
        "locked",
        "work_session",
        "punches",
        "corrections",
        "overtime_requests",
        "attendance",
        "employee",
        "status",
        "shift",
        "leave_application",
        "checkout_miss",
        "pending_approvals",
        "can",
    ):
        assert key in res, key
    assert res["work_session"]["name"] == "WS-E1"
    assert res["status"] == "Late"  # Attendance Present + late_entry → Late
    assert res["shift"]["shift_type"] == "Ca hành chính"
    assert [c["name"] for c in res["pending_approvals"]["corrections"]] == ["CR-E1"]
    can = res["can"]
    assert can["view_detail"] is True
    assert can["fix_punch"] is True
    # mark_attendance là HR-only (mark_attendance_bulk gate) — LM thấy False
    assert can["mark_attendance"] is False
    assert can["request_correction"] is True
    assert can["override_shift"] is True  # DAY == today của stub
    assert can["nudge"] is False  # E1 đã checkout
    assert can["open_360"] is False  # Line Manager không phải HR
    assert can["export"] is True


def test_tt09_locked_period_disables_writes(_env_for_roster):
    env = _env_for_roster
    _seed_locked(env.fr)
    res = env.att.team_member_day_detail(employee="E1", date_str=DAY)
    assert res["locked"] is True
    for key in ("fix_punch", "mark_attendance", "request_correction", "override_shift"):
        assert res["can"][key] is False, key
    roster = env.att.team_daily_status(date_str=DAY)
    assert roster["locked"] is True


def test_tt10_team_day_can_pure(env):
    f = env.att.team_day_can
    # Locked chặn mọi write nhưng KHÔNG chặn nudge (thông báo, không phải ghi công)
    can = f(status="Not Checked In", locked=True, is_hr=False, is_lm_of=True, ws={}, is_today=True)
    assert can["fix_punch"] is False and can["nudge"] is True
    # HR mở 360; LM thì không
    assert f(status="Present", locked=False, is_hr=True, is_lm_of=False)["open_360"] is True
    assert f(status="Present", locked=False, is_hr=False, is_lm_of=True)["open_360"] is False
    # override_shift chỉ hôm nay
    assert (
        f(status="Present", locked=False, is_hr=True, is_lm_of=False, is_today=True)["override_shift"] is True
    )
    assert (
        f(status="Present", locked=False, is_hr=True, is_lm_of=False, is_today=False)["override_shift"]
        is False
    )
    # nudge checkout-miss
    assert (
        f(status="Present", locked=False, is_hr=False, is_lm_of=True, ws={"missing_checkout": True})["nudge"]
        is True
    )
    assert (
        f(status="Present", locked=False, is_hr=False, is_lm_of=True, ws={"missing_checkout": False})["nudge"]
        is False
    )
    assert f(status="Absent", locked=False, is_hr=False, is_lm_of=True)["nudge"] is False
    # plain employee (không HR, không LM) — không có quyền gì trừ view
    can = f(status="Present", locked=False, is_hr=False, is_lm_of=False)
    assert can["view_detail"] is True
    assert not any(
        can[k]
        for k in (
            "fix_punch",
            "mark_attendance",
            "request_correction",
            "override_shift",
            "open_360",
            "export",
        )
    )
    # export cho cả HR và LM
    assert f(status="Present", locked=False, is_hr=False, is_lm_of=True)["export"] is True


def test_tt11_nudge_creates_notification_and_rate_limits(_env_for_roster):
    env = _env_for_roster
    env.state["roles"] = ["Line Manager"]
    env.state["emp"] = "M1"
    res = env.att.nudge_team_member(employee="E2", kind="missing_checkin", date_str=DAY)
    assert res["employee"] == "E2" and res["kind"] == "missing_checkin" and res["work_date"] == DAY
    assert len(env.pushed) == 1 and env.pushed[0]["employee"] == "E2"
    assert env.pushed[0]["title"] == "Nhắc chấm công vào"
    assert any(a == "Manual Override" for a, _ in env.audits)
    assert any(e == "team_today_changed" for e, _ in env.published)
    # Lần 2 trong cửa sổ 10 phút → rate limit chặn
    with pytest.raises(_Thrown) as ei:
        env.att.nudge_team_member(employee="E2", kind="missing_checkin", date_str=DAY)
    assert ei.value.kind == "Validation"
    assert len(env.pushed) == 1
    # Member đã đủ check-in/out → không cần nhắc
    with pytest.raises(_Thrown, match="không cần nhắc"):
        env.att.nudge_team_member(employee="E1", kind="missing_checkout", date_str=DAY)
    assert len(env.pushed) == 1
    # kind lạ
    with pytest.raises(_Thrown) as ei:
        env.att.nudge_team_member(employee="E2", kind="bogus", date_str=DAY)
    assert ei.value.kind == "Validation"


def test_tt12_export_csv(_env_for_roster):
    env = _env_for_roster
    res = env.att.export_team_day_csv(date_str=DAY)
    assert res["filename"] == f"team-{DAY}.csv"
    assert res["csv"].startswith("\ufeff")
    lines = res["csv"].lstrip("\ufeff").splitlines()
    assert "Mã NV" in lines[0] and "Trạng thái" in lines[0]
    assert len(lines) == res["rows"] + 1
    assert res["rows"] == 3  # E1 + E2 + E4


def test_tt13_status_vocabulary(_env_for_roster):
    env = _env_for_roster
    res = env.att.team_daily_status(date_str=DAY)
    statuses = {m["status"] for m in res["members"]}
    assert "Not marked" not in statuses
    assert "Not Checked In" in statuses  # E4 không dữ liệu


def test_tt14_my_day_detail_no_regression(_env_for_roster):
    env = _env_for_roster
    res = env.att.my_day_detail(employee="E1", work_date=DAY)
    assert set(res) == {
        "work_date",
        "locked",
        "work_session",
        "punches",
        "corrections",
        "overtime_requests",
        "attendance",
    }
    assert res["work_session"]["name"] == "WS-E1"
    assert res["attendance"]["name"] == "ATT-E1"


def test_idor_tt1_line_manager_other_team_denied(_env_for_roster):
    env = _env_for_roster
    env.state["roles"] = ["Line Manager"]
    env.state["emp"] = "M1"
    with pytest.raises(_Thrown) as ei:
        env.att.team_member_day_detail(employee="E3", date_str=DAY)
    assert ei.value.kind == "Permission"
    with pytest.raises(_Thrown) as ei:
        env.att.nudge_team_member(employee="E3", kind="missing_checkin", date_str=DAY)
    assert ei.value.kind == "Permission"
    assert env.pushed == []  # zero side effects


def test_idor_tt2_plain_employee_denied_everywhere(_env_for_roster):
    env = _env_for_roster
    env.state["roles"] = ["Employee"]
    env.state["emp"] = "E1"
    for call in (
        lambda: env.att.team_member_day_detail(employee="E2", date_str=DAY),
        lambda: env.att.nudge_team_member(employee="E2", kind="missing_checkin", date_str=DAY),
        lambda: env.att.export_team_day_csv(date_str=DAY),
    ):
        with pytest.raises(_Thrown) as ei:
            call()
        assert ei.value.kind == "Permission"
    assert env.pushed == []


# --------------------------------------------------------------------------- #
# roster-seeded env (module-scoped via fixture factory to keep seeds isolated)
# --------------------------------------------------------------------------- #
@pytest.fixture
def _env_for_roster(env):
    return _seed_all(env)
