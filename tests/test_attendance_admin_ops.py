"""Bench-free unit tests for ``api/attendance_admin_ops.py``
(plans/plan-deskfree-attendance-admin.md §5.1 — TC-B01…TC-B15).

Uses the same stub-frappe harness as ``test_attendance_sync.py`` /
``test_admin_custom_checkin.py`` (``monkeypatch.setitem(sys.modules, "frappe",
stub)``). The ops module imports only ``frappe`` at top level, and every gege_hr
dependency is an indirection helper (``_audit``, ``_recalc_for_checkin``,
``_recalc_work_sessions``, ``_attendance_backfill``) monkeypatched to a spy —
so no bench is needed.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest


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


class _DoesNotExistErr(Exception):
    pass


class _MandatoryErr(Exception):
    pass


class _Thrown(Exception):
    """Captures a frappe.throw call (original exception type is lost on purpose)."""

    def __init__(self, message, kind):
        super().__init__(message)
        self.message = message
        self.kind = kind


_EXC_KIND = {
    _PermErr: "Permission",
    _ValidationErr: "Validation",
    _DoesNotExistErr: "DoesNotExist",
    _MandatoryErr: "Mandatory",
}


class _DB:
    def __init__(self, fr):
        self.fr = fr

    def _store(self, doctype):
        return self.fr.stores.setdefault(doctype, {})

    @staticmethod
    def _match_dict(row, filters):
        for k, v in filters.items():
            rv = row.get(k)
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                op, val = v[0], v[1]
                if op == "<" and not (rv is not None and rv < val):
                    return False
                if op == "<=" and not (rv is not None and rv <= val):
                    return False
                if op == ">" and not (rv is not None and rv > val):
                    return False
                if op == ">=" and not (rv is not None and rv >= val):
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
            if op == "between":
                lo, hi = value
                if not (rv is not None and lo <= rv <= hi):
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
        if fields is None:
            return _AttrDict(row) if as_dict else dict(row)
        if isinstance(fields, str):
            return row.get(fields)
        d = {f: row.get(f) for f in fields}
        return _AttrDict(d) if as_dict else (d[fields[0]] if len(fields) == 1 else d)

    def get_all(self, doctype, filters=None, fields=None, pluck=None, **kw):
        rows = list(self._store(doctype).values())
        if isinstance(filters, dict):
            rows = [r for r in rows if self._match_dict(r, filters)]
        elif isinstance(filters, list):
            rows = [r for r in rows if self._match_list(r, filters)]
        if pluck:
            return [r.get(pluck) for r in rows]
        if fields:
            return [{f: r.get(f) for f in fields} for r in rows]
        return [dict(r) for r in rows]

    def set_value(self, doctype, name, values, **kw):
        store = self._store(doctype)
        store.setdefault(name, {"name": name}).update(values or {})
        self.fr.set_calls.append((doctype, name, dict(values or {})))

    def exists(self, doctype, name):
        return name if name in self._store(doctype) else None

    def commit(self):
        self.fr.commits += 1


class _Frappe:
    def __init__(self, roles=("HR Manager",)):
        self.stores: dict[str, dict] = {}
        self.set_calls: list = []
        self.commits = 0
        self.deleted: list = []
        self._roles = set(roles)
        self._counter = iter(range(900001, 999999))
        self.db = _DB(self)

    # -- frappe surface ------------------------------------------------------
    def _(self, s, *a, **k):
        try:
            return s.format(*a) if a else s
        except Exception:
            return s

    def whitelist(self, fn=None, **kw):
        return fn if fn is not None else (lambda f: f)

    def log_error(self, *a, **k):
        return None

    ValidationError = _ValidationErr
    PermissionError = _PermErr
    DoesNotExistError = _DoesNotExistErr
    MandatoryError = _MandatoryErr

    def throw(self, message, exc=None):
        raise _Thrown(message, _EXC_KIND.get(exc, "Generic"))

    def only_for(self, roles):
        if not (self._roles & set(roles)):
            raise _PermErr("not allowed")

    def delete_doc(self, doctype, name):
        self.deleted.append((doctype, name))
        self.stores.get(doctype, {}).pop(name, None)

    def get_doc(self, payload_or_name, maybe_name=None):
        if isinstance(payload_or_name, dict):
            payload = dict(payload_or_name)
            store = self.stores.setdefault(payload.get("doctype"), {})

            class _NewDoc(_AttrDict):
                def insert(inner, **kw):
                    inner["name"] = f"{payload.get('doctype')[:12]}-{next(self._counter)}"
                    inner["docstatus"] = 0
                    store[inner["name"]] = dict(inner)
                    return inner

            return _NewDoc(payload)
        store = self.stores.setdefault(payload_or_name, {})
        row = store.setdefault(maybe_name, {"name": maybe_name, "docstatus": 0})
        outer = self

        class _Wrap(_AttrDict):
            def submit(inner):
                store[maybe_name]["docstatus"] = 1
                row["docstatus"] = 1
                outer.submits.append(maybe_name)
                return inner

        return _Wrap(row)

    submits: list = None  # initialised per-instance below


# `submits` list needs per-instance init (class attr default above is only a hint)
def _new_stub(roles=("HR Manager",)):
    f = _Frappe(roles)
    f.submits = []
    return f


# --------------------------------------------------------------------------- #
# fixture
# --------------------------------------------------------------------------- #
@pytest.fixture
def env(monkeypatch):
    stub = _new_stub()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    m = importlib.import_module("gege_hr.gege_hr.api.attendance_admin_ops")
    importlib.reload(m)

    spies = {"audit": [], "recalc": [], "recalc_ws": [], "backfill": []}
    monkeypatch.setattr(m, "_audit", lambda action, **kw: spies["audit"].append((action, kw)))
    monkeypatch.setattr(
        m, "_recalc_for_checkin", lambda doc: spies["recalc"].append(getattr(doc, "name", None))
    )
    monkeypatch.setattr(
        m, "_recalc_work_sessions", lambda emp, wd: spies["recalc_ws"].append((emp, wd))
    )

    def _backfill(from_date, to_date, employee):
        spies["backfill"].append((from_date, to_date, employee))
        return {"synced": 2, "skipped": 1, "total": 3}

    monkeypatch.setattr(m, "_attendance_backfill", _backfill)
    return m, stub, spies


def _seed_base(stub):
    """Employees + one Work Session (E1, 2026-08-08, raw OT 2h) + punches."""
    stub.stores["Employee"] = {"E1": {"name": "E1"}, "E2": {"name": "E2"}}
    stub.stores[m_ws(stub)] = {
        "WS-1": {
            "name": "WS-1",
            "employee": "E1",
            "employee_name": "Nhân viên Một",
            "company": "Gege",
            "work_date": "2026-08-08",
            "shift_type": "Ca hành chính",
            "shift_instance": "SI-1",
            "calculation_status": "Calculated",
            "planned_start": "2026-08-08 08:00:00",
            "planned_end": "2026-08-08 17:00:00",
            "actual_checkin": "2026-08-08 08:10:00",
            "actual_checkout": "2026-08-08 19:00:00",
            "late_minutes": 10,
            "early_leave_minutes": 0,
            "raw_overtime_hours": 2.0,
            "approved_overtime_hours": 0,
            "payable_day": 1.0,
            "absent": 0,
            "has_leave": 0,
            "need_review": 0,
            "missing_checkin": 0,
            "missing_checkout": 0,
            "vn_auto_checkout": 0,
            "attendance": None,
        }
    }
    stub.stores["Employee Checkin"] = {
        "EC-IN": {
            "name": "EC-IN",
            "employee": "E1",
            "time": "2026-08-08 08:10:00",
            "log_type": "IN",
            "device_id": "gege_hr-mobile",
        },
        "EC-OUT": {
            "name": "EC-OUT",
            "employee": "E1",
            "time": "2026-08-08 19:00:00",
            "log_type": "OUT",
            "device_id": "gege_hr-mobile",
        },
    }
    stub.stores["VN Attendance Correction Request"] = {
        "CR-1": {
            "name": "CR-1",
            "employee": "E1",
            "work_date": "2026-08-08",
            "correction_type": "Wrong Time",
            "reason": "sửa giờ vào",
            "workflow_state": "Pending",
            "docstatus": 0,
        }
    }


def m_ws(stub):
    return "VN Attendance Work Session"


def _lock_month(stub, from_d="2026-07-01", to_d="2026-07-31"):
    stub.stores["VN Monthly Attendance Period"] = {
        "P-LOCK": {"name": "P-LOCK", "from_date": from_d, "to_date": to_d, "status": "Locked"}
    }


# --------------------------------------------------------------------------- #
# E1 — get_work_session_detail
# --------------------------------------------------------------------------- #
def test_detail_returns_punches_corrections_and_lock(env):
    m, stub, _ = env
    _seed_base(stub)
    res = m.get_work_session_detail("WS-1")
    assert res["work_session"]["employee"] == "E1"
    assert [c["name"] for c in res["checkins"]] == ["EC-IN", "EC-OUT"]
    assert res["corrections"][0]["name"] == "CR-1"
    assert res["locked"] is False


def test_detail_unknown_name_raises(env):
    m, stub, _ = env
    _seed_base(stub)
    with pytest.raises(_Thrown) as e:
        m.get_work_session_detail("WS-404")
    assert e.value.kind == "DoesNotExist"


def test_detail_fields_intersect_meta_columns(env, monkeypatch):
    """Regression (live-bench 500): a projected field that is not a real column
    must be dropped via the meta intersection instead of raising
    UnknownColumnError."""
    m, stub, _ = env
    _seed_base(stub)

    class _Meta:
        @staticmethod
        def get_table_columns(doctype):
            # pretend the live schema lacks most fields
            return ["name", "employee", "employee_name", "work_date", "shift_type"]

    monkeypatch.setattr(stub, "meta", _Meta, raising=False)
    res = m.get_work_session_detail("WS-1")
    assert res["work_session"]["employee"] == "E1"
    # projection respected the meta columns (only real ones requested)
    assert set(res["work_session"].keys()) >= {"name", "employee"}
    # and without meta the fallback returns the full list without crashing
    monkeypatch.delattr(stub, "meta", raising=False)
    res2 = m.get_work_session_detail("WS-1")
    assert res2["work_session"]["employee_name"] == "Nhân viên Một"


# --------------------------------------------------------------------------- #
# E2 — list_checkins
# --------------------------------------------------------------------------- #
def test_list_checkins_filters_range_and_log_type(env):
    m, stub, _ = env
    _seed_base(stub)
    rows = m.list_checkins(employee="E1", from_date="2026-08-08", to_date="2026-08-08")
    assert {r["name"] for r in rows} == {"EC-IN", "EC-OUT"}
    rows = m.list_checkins(employee="E1", from_date="2026-08-08", to_date="2026-08-08", log_type="IN")
    assert [r["name"] for r in rows] == ["EC-IN"]
    # outside window → nothing
    rows = m.list_checkins(employee="E1", from_date="2026-08-01", to_date="2026-08-05")
    assert rows == []


def test_list_checkins_requires_employee(env):
    m, _, _ = env
    with pytest.raises(_Thrown) as e:
        m.list_checkins()
    assert e.value.kind == "Mandatory"


# --------------------------------------------------------------------------- #
# E3 — create_checkin
# --------------------------------------------------------------------------- #
def test_create_checkin_inserts_recalcs_and_audits(env):
    m, stub, spies = env
    _seed_base(stub)
    res = m.create_checkin(
        employee="E2", time="2026-08-08 08:30", log_type="IN", reason="quên chấm máy"
    )
    assert res["ok"] is True
    created = [r for r in stub.stores["Employee Checkin"].values() if r["employee"] == "E2"]
    assert len(created) == 1
    assert created[0]["time"] == "2026-08-08 08:30:00"
    assert created[0]["log_type"] == "IN"
    # recalc + audit fired
    assert spies["recalc"], "recalc hook must run after manual create"
    assert spies["audit"] and spies["audit"][0][0] == "Admin Manual Checkin"
    assert stub.commits >= 1


def test_create_checkin_requires_reason(env):
    m, stub, _ = env
    _seed_base(stub)
    with pytest.raises(_Thrown) as e:
        m.create_checkin(employee="E2", time="2026-08-08 08:30", log_type="IN", reason="x")
    assert e.value.kind == "Validation"


def test_create_checkin_invalid_log_type(env):
    m, stub, _ = env
    _seed_base(stub)
    with pytest.raises(_Thrown) as e:
        m.create_checkin(employee="E2", time="2026-08-08 08:30", log_type="BREAK", reason="abc")
    assert e.value.kind == "Validation"


def test_create_checkin_unknown_employee(env):
    m, stub, _ = env
    _seed_base(stub)
    with pytest.raises(_Thrown) as e:
        m.create_checkin(employee="E9", time="2026-08-08 08:30", log_type="IN", reason="abc")
    assert e.value.kind == "DoesNotExist"


# --------------------------------------------------------------------------- #
# E4 — update_checkin
# --------------------------------------------------------------------------- #
def test_update_checkin_sets_time_and_recalcs(env):
    m, stub, spies = env
    _seed_base(stub)
    res = m.update_checkin(name="EC-IN", time="2026-08-08 08:00", reason="sai giờ do máy")
    assert res["ok"] is True
    assert stub.stores["Employee Checkin"]["EC-IN"]["time"] == "2026-08-08 08:00:00"
    assert spies["recalc"], "update path must trigger explicit recalc"
    assert spies["audit"] and spies["audit"][0][0] == "Admin Edit Checkin"


def test_update_checkin_requires_change(env):
    m, stub, _ = env
    _seed_base(stub)
    with pytest.raises(_Thrown) as e:
        m.update_checkin(name="EC-IN", reason="không đổi gì")
    assert e.value.kind == "Validation"


# --------------------------------------------------------------------------- #
# E5 — delete_checkin
# --------------------------------------------------------------------------- #
def test_delete_checkin_removes_and_recalcs_sessions(env):
    m, stub, spies = env
    _seed_base(stub)
    res = m.delete_checkin(name="EC-OUT", reason="lượt ra trùng")
    assert res["ok"] is True
    assert "EC-OUT" not in stub.stores["Employee Checkin"]
    assert stub.deleted == [("Employee Checkin", "EC-OUT")]
    assert spies["recalc_ws"] == [("E1", "2026-08-08")]
    assert spies["audit"] and spies["audit"][0][0] == "Admin Delete Checkin"


# --------------------------------------------------------------------------- #
# Locked period guard (TC-B05)
# --------------------------------------------------------------------------- #
def test_locked_period_blocks_all_writes(env):
    m, stub, _ = env
    _seed_base(stub)
    _lock_month(stub)
    with pytest.raises(_Thrown) as e:
        m.create_checkin(employee="E1", time="2026-07-15 08:30", log_type="IN", reason="abc")
    assert e.value.kind == "Validation"

    stub.stores["Employee Checkin"]["EC-JUL"] = {
        "name": "EC-JUL",
        "employee": "E1",
        "time": "2026-07-15 08:30:00",
        "log_type": "IN",
    }
    with pytest.raises(_Thrown) as e:
        m.update_checkin(name="EC-JUL", time="2026-07-15 09:00", reason="abc")
    assert e.value.kind == "Validation"
    with pytest.raises(_Thrown) as e:
        m.delete_checkin(name="EC-JUL", reason="abc")
    assert e.value.kind == "Validation"
    with pytest.raises(_Thrown) as e:
        m.mark_attendance_bulk(employees=["E1"], attendance_date="2026-07-15", status="Present")
    assert e.value.kind == "Validation"


# --------------------------------------------------------------------------- #
# E6 — generate_attendance (delegation)
# --------------------------------------------------------------------------- #
def test_generate_attendance_delegates_with_dates_and_employee(env):
    m, stub, spies = env
    _seed_base(stub)
    res = m.generate_attendance(from_date="2026-08-01", to_date="2026-08-31", employee="E1")
    assert res["ok"] is True
    assert res["synced"] == 2 and res["skipped"] == 1
    assert spies["backfill"] == [("2026-08-01", "2026-08-31", "E1")]
    assert spies["audit"] and spies["audit"][0][0] == "Admin Generate Attendance"


def test_generate_attendance_swaps_reversed_range(env):
    m, stub, spies = env
    _seed_base(stub)
    m.generate_attendance(from_date="2026-08-31", to_date="2026-08-01")
    assert spies["backfill"][0][0] == "2026-08-01"
    assert spies["backfill"][0][1] == "2026-08-31"


# --------------------------------------------------------------------------- #
# E7 — mark_attendance_bulk
# --------------------------------------------------------------------------- #
def test_mark_bulk_creates_and_skips_existing(env):
    m, stub, spies = env
    _seed_base(stub)
    stub.stores["Attendance"] = {
        "ATT-OLD": {
            "name": "ATT-OLD",
            "employee": "E2",
            "attendance_date": "2026-08-08",
            "status": "Absent",
            "docstatus": 0,
        }
    }
    res = m.mark_attendance_bulk(
        employees=["E1", "E2"], attendance_date="2026-08-08", status="Present"
    )
    assert res["created"] == 1 and res["skipped"] == 1
    new_rows = [
        r
        for r in stub.stores["Attendance"].values()
        if r["employee"] == "E1" and r["attendance_date"] == "2026-08-08"
    ]
    assert len(new_rows) == 1 and new_rows[0]["status"] == "Present"
    assert len([a for a in spies["audit"] if a[0] == "Admin Mark Attendance"]) == 1


def test_mark_bulk_overwrite_updates_existing(env):
    m, stub, _ = env
    _seed_base(stub)
    stub.stores["Attendance"] = {
        "ATT-OLD": {
            "name": "ATT-OLD",
            "employee": "E2",
            "attendance_date": "2026-08-08",
            "status": "Absent",
            "docstatus": 0,
        }
    }
    res = m.mark_attendance_bulk(
        employees=["E2"], attendance_date="2026-08-08", status="Half Day", overwrite=1
    )
    assert res["created"] == 1 and res["skipped"] == 0
    assert stub.stores["Attendance"]["ATT-OLD"]["status"] == "Half Day"


def test_mark_bulk_invalid_status(env):
    m, stub, _ = env
    _seed_base(stub)
    with pytest.raises(_Thrown) as e:
        m.mark_attendance_bulk(employees=["E1"], attendance_date="2026-08-08", status="Foo")
    assert e.value.kind == "Validation"


def test_mark_bulk_accepts_comma_separated_employees(env):
    m, stub, _ = env
    _seed_base(stub)
    res = m.mark_attendance_bulk(employees="E1, E2", attendance_date="2026-08-08", status="Present")
    assert res["created"] == 2


# --------------------------------------------------------------------------- #
# E8 — approve_session_overtime
# --------------------------------------------------------------------------- #
def test_approve_ot_defaults_to_raw(env):
    m, stub, _ = env
    _seed_base(stub)
    res = m.approve_session_overtime(work_session="WS-1")
    assert res["approved_overtime_hours"] == 2.0


def test_approve_ot_caps_at_raw(env):
    m, stub, _ = env
    _seed_base(stub)
    res = m.approve_session_overtime(work_session="WS-1", hours=5, note="duyệt hết")
    assert res["approved_overtime_hours"] == 2.0
    assert ("VN Attendance Work Session", "WS-1", {"approved_overtime_hours": 2.0}) in stub.set_calls


def test_approve_ot_partial_hours(env):
    m, stub, _ = env
    _seed_base(stub)
    res = m.approve_session_overtime(work_session="WS-1", hours=1.5)
    assert res["approved_overtime_hours"] == 1.5


def test_approve_ot_refuses_when_no_raw(env):
    m, stub, _ = env
    _seed_base(stub)
    stub.stores["VN Attendance Work Session"]["WS-1"]["raw_overtime_hours"] = 0
    with pytest.raises(_Thrown) as e:
        m.approve_session_overtime(work_session="WS-1")
    assert e.value.kind == "Validation"


def test_approve_ot_blocked_on_locked_period(env):
    m, stub, _ = env
    _seed_base(stub)
    stub.stores["VN Attendance Work Session"]["WS-1"]["work_date"] = "2026-07-15"
    _lock_month(stub)
    with pytest.raises(_Thrown) as e:
        m.approve_session_overtime(work_session="WS-1")
    assert e.value.kind == "Validation"


# --------------------------------------------------------------------------- #
# Permission gates (TC-B14 / TC-B15)
# --------------------------------------------------------------------------- #
def test_all_endpoints_deny_plain_employee(env, monkeypatch):
    m, stub, _ = env
    _seed_base(stub)
    stub._roles = {"Employee"}
    calls = [
        lambda: m.get_work_session_detail("WS-1"),
        lambda: m.list_checkins(employee="E1"),
        lambda: m.create_checkin(employee="E1", time="2026-08-08 08:30", log_type="IN", reason="abc"),
        lambda: m.update_checkin(name="EC-IN", time="2026-08-08 08:00", reason="abc"),
        lambda: m.delete_checkin(name="EC-IN", reason="abc"),
        lambda: m.generate_attendance(from_date="2026-08-01", to_date="2026-08-31"),
        lambda: m.mark_attendance_bulk(employees=["E1"], attendance_date="2026-08-08", status="Present"),
        lambda: m.approve_session_overtime(work_session="WS-1"),
    ]
    for call in calls:
        with pytest.raises(_PermErr):
            call()


def test_approve_ot_requires_hr_manager_or_system(env):
    m, stub, _ = env
    _seed_base(stub)
    stub._roles = {"HR User"}  # HR User may read/write punches but NOT approve OT
    with pytest.raises(_PermErr):
        m.approve_session_overtime(work_session="WS-1")
    # …while the punch write path stays allowed for HR User
    res = m.create_checkin(employee="E2", time="2026-08-08 08:30", log_type="IN", reason="abc")
    assert res["ok"] is True
