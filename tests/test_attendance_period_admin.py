"""Bench-free unit tests for the desk-free closing admin endpoints
(``api/attendance_period.py`` — plans/plan-lock-desk-free.md §4.1).

Covers TC-BE-01…TC-BE-12:

  * ``lock_logs``       — history read gate + pushed-down filters
  * ``delete_period``   — Draft-only trash + audit + child lines
  * ``adjust_line``     — whitelist, reason gate, rollup refresh, audit
  * ``periods(search)`` — server-side broad LIKE (HR-BL-10)
  * lock/unlock         — VN Notification pushes (best-effort) + realtime

Stub-frappe harness pattern of ``test_blackout_api.py`` /
``test_attendance_admin_ops.py`` (``monkeypatch.setitem(sys.modules, "frappe",
stub)`` — auto-restored, never leaks to the sibling bench-free tests).
"""

from __future__ import annotations

import importlib
import json
import sys
import types

import pytest

PERIOD_DT = "VN Monthly Attendance Period"
LINE_DT = "VN Monthly Attendance Line"
LOG_DT = "VN Attendance Lock Log"
REVIEW_DT = "VN Payroll Review Period"


# --------------------------------------------------------------------------- #
# frappe stub
# --------------------------------------------------------------------------- #
def _build_stub_frappe():
    mod = types.ModuleType("frappe")
    mod._ = lambda s: s
    mod.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)

    class _PermissionError(Exception):
        pass

    class _ValidationError(Exception):
        pass

    class _DoesNotExistError(Exception):
        pass

    mod.PermissionError = _PermissionError
    mod.ValidationError = _ValidationError
    mod.DoesNotExistError = _DoesNotExistError

    def _throw(msg, exc=None):
        raise (exc or _ValidationError)(str(msg))

    mod.throw = _throw
    mod.session = types.SimpleNamespace(user="hr@test.local")
    mod.get_roles = lambda user=None: ("HR Manager",)
    mod.log_error = lambda *a, **k: None
    mod.get_traceback = lambda: "tb"
    mod.publish_realtime = lambda *a, **k: None
    mod.get_doc = lambda *a, **k: None
    mod.delete_doc = lambda *a, **k: None
    mod.db = None

    # frappe.utils — module-level __getattr__ so ANY ``from frappe.utils
    # import X`` executed by the gege_hr modules under test resolves.
    utils = types.ModuleType("frappe.utils")
    utils.now_datetime = lambda: "2026-08-29 10:00:00"
    utils.today = lambda: "2026-08-29"

    def _utils_getattr(name):
        def _generic(*_a, **_k):
            return None

        return _generic

    utils.__getattr__ = _utils_getattr
    mod.utils = utils
    return mod, utils


class _FakeDoc(dict):
    """Doc-like stub: attribute access writes DICT keys (so save()/as_dict()
    see mutations) and save() syncs the backing fixture row for re-reads."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)

    def __setattr__(self, key, value):
        self[key] = value

    def set(self, key, value):
        self[key] = value

    def as_dict(self):
        return {k: v for k, v in self.items() if not k.startswith("_")}

    def insert(self, ignore_permissions=False):
        if not self.get("name"):
            self["name"] = f"NEW-{len(self['_store']['inserted']) + 1}"
        self["_store"]["inserted"].append(self.as_dict())
        return self

    def save(self):
        self["_store"]["saved"].append(self.as_dict())
        row = self.get("_row")
        if row is not None:
            row.update(self.as_dict())
        return self


class _Row(dict):
    """frappe._dict-like row: attribute access on top of dict storage."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)


class _FakeDB:
    """Routes per-doctype fixtures; records every get_all/get_value call so
    tests can assert the filters pushed down to the real DB."""

    def __init__(self):
        self.rows = {PERIOD_DT: [], LINE_DT: [], LOG_DT: []}
        self.has_role_rows = []  # {"parent": user}
        self.employee_rows = []  # {"name": emp, "user_id": user, "status": ...}
        self.payroll_review_exists = "PRP-1"  # truthy → lock skips review create
        self.payroll_review_dep = None  # unlock guard fixture (None = free)
        self.get_value_calls = []
        self.get_all_calls = []

    def table_exists(self, doctype):
        return True

    def get_value(self, doctype, key, fields=None, as_dict=False):
        self.get_value_calls.append((doctype, key, fields))
        if doctype == REVIEW_DT:
            return self.payroll_review_dep
        if doctype == PERIOD_DT and isinstance(key, str):
            for row in self.rows[PERIOD_DT]:
                if row.get("name") == key:
                    return _Row(row)
        return None

    def exists(self, doctype, filters=None):
        if doctype == REVIEW_DT:
            return self.payroll_review_exists
        if doctype == PERIOD_DT and isinstance(filters, dict):
            for row in self.rows[PERIOD_DT]:
                plain = {
                    k: v
                    for k, v in filters.items()
                    if not (isinstance(v, list) and v and v[0] in ("<", "<=", ">", ">=", "like"))
                }
                if all(row.get(k) == v for k, v in plain.items()):
                    return row.get("name")
        return None

    def set_value(self, doctype, name, field, value, update_modified=None):
        for row in self.rows.get(doctype, []):
            if row.get("name") == name:
                row[field] = value

    def get_all(
        self,
        doctype,
        filters=None,
        fields=None,
        or_filters=None,
        order_by=None,
        pluck=None,
        **_kw,
    ):
        self.get_all_calls.append(
            {
                "doctype": doctype,
                "filters": filters,
                "fields": fields,
                "or_filters": or_filters,
                "order_by": order_by,
            }
        )
        if doctype == "Has Role":
            rows = list(self.has_role_rows)
        elif doctype == "Employee":
            rows = list(self.employee_rows)
        else:
            rows = list(self.rows.get(doctype, []))
        if pluck:
            return [r.get(pluck) for r in rows]
        return [_Row(r) for r in rows]


def make_fake(monkeypatch, roles=("HR Manager",)):
    """Register the stub frappe, import ``api/attendance_period`` and wire the
    fake db / doc factory / audit / notify / realtime recorders."""
    stub, utils = _build_stub_frappe()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    stub.get_roles = lambda user=None: tuple(roles)

    api = importlib.import_module("gege_hr.gege_hr.api.attendance_period")
    monkeypatch.setattr(api, "frappe", stub)

    # Re-pin sibling modules' frappe references — the gege_hr modules are
    # cached across tests (sys.modules), so without this the FIRST stub's
    # roles leak into later tests (get_user_roles kept "HR Manager").
    emp_utils_mod = importlib.import_module("gege_hr.gege_hr.utils.employee")
    monkeypatch.setattr(emp_utils_mod, "frappe", stub)
    notify_mod = importlib.import_module("gege_hr.gege_hr.utils.notify")
    monkeypatch.setattr(notify_mod, "frappe", stub)
    monkeypatch.setattr(api.audit_api, "frappe", stub)

    db = _FakeDB()
    monkeypatch.setattr(stub, "db", db)

    store = {"inserted": [], "saved": [], "deleted": []}

    def _get_doc(arg, name=None, **_kw):
        if isinstance(arg, dict):
            doc = _FakeDoc(dict(arg))
            doc["_store"] = store
            return doc
        target = name if name is not None else arg
        for row in db.rows.get(arg, []):
            if row.get("name") == target:
                doc = _FakeDoc({k: v for k, v in row.items()})
                doc["_store"] = store
                doc["_row"] = row
                doc["doctype"] = arg
                return doc
        raise RuntimeError(f"{target} does not exist")

    monkeypatch.setattr(stub, "get_doc", _get_doc)
    monkeypatch.setattr(
        stub, "delete_doc", lambda dt, nm, **_k: store["deleted"].append((dt, nm))
    )

    audits = []

    def _audit(*a, **k):
        audits.append((a, k))
        return "AUD-1"

    monkeypatch.setattr(api.audit_api, "log", _audit)

    pushes = []

    def _push(**k):
        pushes.append(k)
        return "NT-1"

    monkeypatch.setattr(api.notify, "push_notification", _push)

    published = []

    def _pub(event, message=None, **_k):
        published.append((event, message))

    monkeypatch.setattr(stub, "publish_realtime", _pub)

    return types.SimpleNamespace(
        api=api, db=db, stub=stub, store=store, audits=audits, pushes=pushes,
        published=published,
    )


@pytest.fixture
def fake(monkeypatch):
    return make_fake(monkeypatch)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _period(name="P-1", status="Generated", month="07", year=2026, company="ACME", **kw):
    row = {
        "name": name,
        "period_name": f"Công tháng {month}/{year}",
        "company": company,
        "from_date": f"{year}-{month}-01",
        "to_date": f"{year}-{month}-31",
        "payroll_month": month,
        "payroll_year": year,
        "status": status,
        "total_employees": 0,
        "total_present_days": 0.0,
        "total_absent_days": 0.0,
        "total_overtime_hours": 0.0,
        "total_late_minutes": 0,
        "total_need_review": 0,
    }
    row.update(kw)
    return row


def _line(name="LN-1", period="P-1", employee="E1", status="Confirmed", **kw):
    row = {
        "name": name,
        "attendance_period": period,
        "employee": employee,
        "employee_name": f"NV {employee}",
        "department": "IT",
        "branch": "HQ",
        "company": "ACME",
        "status": status,
        "working_days": 26,
        "present_days": 24.0,
        "absent_days": 0.0,
        "paid_leave_days": 2.0,
        "unpaid_leave_days": 0.0,
        "holiday_days": 0.0,
        "regular_hours": 192.0,
        "regular_night_hours": 0.0,
        "overtime_hours": 10.0,
        "overtime_night_hours": 0.0,
        "overtime_holiday_hours": 0.0,
        "late_count": 0,
        "late_minutes": 0,
        "early_leave_count": 0,
        "early_leave_minutes": 0,
        "payable_hours": 200.0,
        "payable_days": 22.0,
        "need_review_count": 0,
    }
    row.update(kw)
    return row


def _last_period_call(fake):
    calls = [c for c in fake.db.get_all_calls if c["doctype"] == PERIOD_DT]
    assert calls, "expected a PERIOD get_all call"
    return calls[-1]


# --------------------------------------------------------------------------- #
# TC-BE-01 — lock_logs
# --------------------------------------------------------------------------- #
def test_be01_lock_logs_hr_user_reads_rows_and_pushes_filters(fake):
    fake.db.rows[LOG_DT] = [
        {
            "name": "LL-1",
            "attendance_period": "P-1",
            "action": "Lock",
            "reason": "chốt sổ",
            "actor": "hr@test.local",
            "old_status": "Generated",
            "new_status": "Locked",
            "created_at": "2026-08-01 09:00:00",
        },
        {
            "name": "LL-2",
            "attendance_period": "P-1",
            "action": "Unlock",
            "reason": "sai OT",
            "actor": "hr@test.local",
            "old_status": "Locked",
            "new_status": "Unlocked",
            "created_at": "2026-08-02 09:00:00",
        },
    ]
    out = fake.api.lock_logs("P-1")
    assert [r["name"] for r in out] == ["LL-1", "LL-2"]
    assert all(
        f in out[0] for f in ("action", "reason", "actor", "old_status", "new_status", "created_at")
    )
    log_call = [c for c in fake.db.get_all_calls if c["doctype"] == LOG_DT][-1]
    assert log_call["filters"] == {"attendance_period": "P-1"}
    assert log_call["order_by"] == "created_at desc"


def test_be01b_lock_logs_denied_for_plain_employee(monkeypatch):
    fake = make_fake(monkeypatch, roles=("Employee",))
    with pytest.raises(Exception, match="không có quyền"):
        fake.api.lock_logs("P-1")


# --------------------------------------------------------------------------- #
# TC-BE-02/03/04 — delete_period
# --------------------------------------------------------------------------- #
def test_be02_delete_draft_period_removes_lines_and_audits(fake):
    fake.db.rows[PERIOD_DT] = [_period(status="Draft")]
    fake.db.rows[LINE_DT] = [_line("LN-1", status="Draft"), _line("LN-2", employee="E2", status="Draft")]

    out = fake.api.delete_period("P-1")

    assert "Đã xoá" in out["message"]
    assert (LINE_DT, "LN-1") in fake.store["deleted"]
    assert (LINE_DT, "LN-2") in fake.store["deleted"]
    assert (PERIOD_DT, "P-1") in fake.store["deleted"]
    assert fake.audits, "delete must be audited"
    assert fake.audits[0][0][0] == "Manual Override"
    assert ("hr-portal:attendance-periods", {"action": "delete", "name": "P-1"}) in fake.published


@pytest.mark.parametrize("status", ["Generated", "Locked", "Unlocked"])
def test_be03_delete_non_draft_rejected(fake, status):
    fake.db.rows[PERIOD_DT] = [_period(status=status)]
    with pytest.raises(Exception, match="Chỉ kỳ công nháp"):
        fake.api.delete_period("P-1")
    assert fake.store["deleted"] == []


def test_be04_delete_requires_closer_role(monkeypatch):
    fake = make_fake(monkeypatch, roles=("Employee",))
    fake.db.rows[PERIOD_DT] = [_period(status="Draft")]
    with pytest.raises(Exception, match="không có quyền"):
        fake.api.delete_period("P-1")


# --------------------------------------------------------------------------- #
# TC-BE-05/06/07/08 — adjust_line
# --------------------------------------------------------------------------- #
def test_be05_adjust_line_updates_status_audit_and_rollup(fake):
    fake.db.rows[PERIOD_DT] = [_period()]
    fake.db.rows[LINE_DT] = [_line(overtime_hours=10.0)]

    out = fake.api.adjust_line("LN-1", values={"overtime_hours": "12"}, reason="đơn OT giấy")

    assert out["status"] == "Adjusted"
    assert "Đã điều chỉnh" in out["message"]
    # fixture row synced (status + value) so a re-read sees the override
    assert fake.db.rows[LINE_DT][0]["overtime_hours"] == 12.0
    assert fake.db.rows[LINE_DT][0]["status"] == "Adjusted"
    # period rollup refreshed
    saved_period = [d for d in fake.store["saved"] if d.get("name") == "P-1"][-1]
    assert saved_period["total_overtime_hours"] == 12.0
    # audit carries old/new values
    audit_type, kwargs = fake.audits[0]
    assert audit_type[0] == "Manual Override"
    assert json.loads(kwargs["old_value"]) == {"overtime_hours": 10.0}
    assert json.loads(kwargs["new_value"]) == {"overtime_hours": 12.0}
    assert "đơn OT giấy" in kwargs["description"]
    assert ("hr-portal:attendance-periods", {"action": "adjust", "name": "P-1"}) in fake.published


def test_be05b_adjust_line_accepts_json_string_values(fake):
    fake.db.rows[PERIOD_DT] = [_period()]
    fake.db.rows[LINE_DT] = [_line(payable_days=22.0)]

    out = fake.api.adjust_line("LN-1", values='{"payable_days": "20"}', reason="trừ ngày không lương")

    assert out["status"] == "Adjusted"
    assert fake.db.rows[LINE_DT][0]["payable_days"] == 20.0


def test_be06_adjust_line_rejects_non_whitelisted_keys(fake):
    fake.db.rows[PERIOD_DT] = [_period()]
    fake.db.rows[LINE_DT] = [_line()]
    with pytest.raises(Exception, match="Không thể điều chỉnh"):
        fake.api.adjust_line("LN-1", values={"employee": "E9", "status": "Confirmed"}, reason="hack")
    assert fake.db.rows[LINE_DT][0]["employee"] == "E1"


@pytest.mark.parametrize("reason", ["", "  ", "ab"])
def test_be07_adjust_line_requires_reason(fake, reason):
    fake.db.rows[PERIOD_DT] = [_period()]
    fake.db.rows[LINE_DT] = [_line()]
    with pytest.raises(Exception, match="tối thiểu 3 ký tự"):
        fake.api.adjust_line("LN-1", values={"payable_days": 20}, reason=reason)


def test_be08_adjust_line_blocked_when_locked(fake):
    fake.db.rows[PERIOD_DT] = [_period(status="Locked")]
    fake.db.rows[LINE_DT] = [_line(status="Confirmed")]
    with pytest.raises(Exception, match="Kỳ công đã khoá"):
        fake.api.adjust_line("LN-1", values={"payable_days": 20}, reason="điều chỉnh")

    fake.db.rows[PERIOD_DT] = [_period(status="Generated")]
    fake.db.rows[LINE_DT] = [_line(status="Locked")]
    with pytest.raises(Exception, match="Dòng công đã khoá"):
        fake.api.adjust_line("LN-1", values={"payable_days": 20}, reason="điều chỉnh")


# --------------------------------------------------------------------------- #
# TC-BE-09 — periods(search)
# --------------------------------------------------------------------------- #
def test_be09_search_builds_or_filters(fake):
    fake.db.rows[PERIOD_DT] = [_period(), _period(name="P-2", month="08", status="Draft")]
    fake.api.periods(search="07")
    call = _last_period_call(fake)
    assert ["period_name", "like", "%07%"] in call["or_filters"]
    assert ["name", "like", "%07%"] in call["or_filters"]
    assert ["locked_by", "like", "%07%"] in call["or_filters"]
    assert ["status", "like", "%07%"] in call["or_filters"]


def test_be09b_no_search_keeps_legacy_shape(fake):
    fake.api.periods()
    call = _last_period_call(fake)
    assert call["or_filters"] is None
    assert call["filters"] == {}


# --------------------------------------------------------------------------- #
# TC-BE-10 — lock/unlock notifications + realtime (best-effort)
# --------------------------------------------------------------------------- #
def test_be10_lock_survives_notification_failure(fake, monkeypatch):
    fake.db.rows[PERIOD_DT] = [_period()]
    fake.db.rows[LINE_DT] = [
        _line("LN-1", status="Draft"),  # exercises the auto-confirm path
        _line("LN-2", employee="E2", status="Confirmed"),
    ]

    def _boom(**_k):
        raise RuntimeError("notify down")

    monkeypatch.setattr(fake.api.notify, "push_notification", _boom)

    out = fake.api.lock_period("P-1", reason="chốt sổ")

    assert out["status"] == "Locked"
    lock_logs = [d for d in fake.store["inserted"] if d.get("doctype") == LOG_DT]
    assert lock_logs and lock_logs[0]["action"] == "Lock"
    assert lock_logs[0]["reason"] == "chốt sổ"
    assert ("hr-portal:attendance-periods", {"action": "lock", "name": "P-1"}) in fake.published


def test_be10b_lock_notifies_employees_unlock_notifies_managers(fake):
    fake.db.rows[PERIOD_DT] = [_period()]
    fake.db.rows[LINE_DT] = [
        _line("LN-1", status="Confirmed"),
        _line("LN-2", employee="E2", status="Confirmed"),
    ]

    out = fake.api.lock_period("P-1")

    assert out["status"] == "Locked"
    assert {p["employee"] for p in fake.pushes} == {"E1", "E2"}
    assert all(p["notification_type"] == "Payroll" for p in fake.pushes)
    assert all(p["reference_name"] == "P-1" for p in fake.pushes)

    # --- unlock: managers alerted, lines restored, reason logged ----------
    fake.db.rows[PERIOD_DT][0]["status"] = "Locked"
    for row in fake.db.rows[LINE_DT]:
        row["status"] = "Locked"
    fake.db.has_role_rows = [{"parent": "hr2@test.local"}]
    fake.db.employee_rows = [
        {"name": "E9", "user_id": "hr2@test.local", "status": "Active"}
    ]
    fake.pushes.clear()

    out = fake.api.unlock_period("P-1", reason="sai dữ liệu")

    assert out["status"] == "Unlocked"
    assert [p["employee"] for p in fake.pushes] == ["E9"]
    assert fake.pushes[0]["notification_type"] == "Alert"
    unlock_logs = [d for d in fake.store["inserted"] if d.get("doctype") == LOG_DT]
    assert unlock_logs[-1]["action"] == "Unlock"
    assert unlock_logs[-1]["reason"] == "sai dữ liệu"
    # lines flipped back to Confirmed so the period can be re-locked
    assert all(r["status"] == "Confirmed" for r in fake.db.rows[LINE_DT])
    assert ("hr-portal:attendance-periods", {"action": "unlock", "name": "P-1"}) in fake.published


# --------------------------------------------------------------------------- #
# TC-BE-11/12 — FE-contract wrappers in api/attendance.py
# --------------------------------------------------------------------------- #
def _import_attendance(fake, monkeypatch):
    try:
        att = importlib.import_module("gege_hr.gege_hr.api.attendance")
    except Exception as exc:  # pragma: no cover — heavier import surface
        pytest.skip(f"api.attendance import needs more frappe surface: {exc}")
    monkeypatch.setattr(att, "frappe", fake.stub)
    return att


def test_be11_wrapper_forwards_search(fake, monkeypatch):
    att = _import_attendance(fake, monkeypatch)
    att.get_monthly_period_list(company=None, year="2026", status=None, search="07")
    call = _last_period_call(fake)
    assert ["period_name", "like", "%07%"] in call["or_filters"]


def test_be12_wrappers_delegate_to_core(fake, monkeypatch):
    att = _import_attendance(fake, monkeypatch)
    called = {}
    monkeypatch.setattr(fake.api, "confirm_line", lambda n: called.setdefault("confirm_line", n))
    monkeypatch.setattr(
        fake.api, "confirm_all_lines", lambda p: called.setdefault("confirm_all_lines", p)
    )
    monkeypatch.setattr(
        fake.api,
        "adjust_line",
        lambda n, values=None, reason=None: called.setdefault(
            "adjust_line", (n, values, reason)
        ),
    )
    monkeypatch.setattr(fake.api, "delete_period", lambda n: called.setdefault("delete_period", n))
    monkeypatch.setattr(fake.api, "lock_logs", lambda p: called.setdefault("lock_logs", p))

    att.confirm_monthly_line("LN-1")
    att.confirm_all_monthly_lines("P-1")
    att.adjust_monthly_line("LN-1", values={"payable_days": 20}, reason="ok reason")
    att.delete_monthly_period("P-1")
    att.get_lock_logs("P-1")

    assert called == {
        "confirm_line": "LN-1",
        "confirm_all_lines": "P-1",
        "adjust_line": ("LN-1", {"payable_days": 20}, "ok reason"),
        "delete_period": "P-1",
        "lock_logs": "P-1",
    }
