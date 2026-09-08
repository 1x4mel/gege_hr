"""Desk-free attendance admin operations (plans/plan-deskfree-attendance-admin.md).

Gives the SPA ``/hr/admin/attendance`` screen the Frappe-Desk write path for
attendance so an HR admin never has to open the Desk:

* ``get_work_session_detail`` — E1: full session + raw punches + corrections
* ``list_checkins``            — E2: raw ``Employee Checkin`` directory
* ``create_checkin``           — E3: manual punch on behalf of an employee
* ``update_checkin``           — E4: fix a wrong punch time / type
* ``delete_checkin``           — E5: remove a stray duplicate punch
* ``generate_attendance``      — E6: delegates to ``attendance_sync.backfill_attendance``
* ``mark_attendance_bulk``     — E7: Employee Attendance Tool parity
* ``approve_session_overtime``— E8: raw OT → approved OT on one session

Conventions (plan §3 invariants):
  * every endpoint is ``frappe.only_for``-gated (E8 stricter — HR Manager/System);
  * every write refuses dates inside a *Locked* ``VN Monthly Attendance Period``;
  * every write requires a ``reason`` (≥ 3 chars) and writes a ``VN Audit Event``;
  * Work-Session recomputation always rides the existing engine hooks
    (``on_employee_checkin_create`` / ``calc.persist_work_session``) — this module
    never hand-edits Work Session metrics.

Bench-free testing note: the module imports ONLY ``frappe`` at top level and
resolves every gege_hr dependency lazily inside tiny indirection helpers
(``_audit``, ``_recalc_for_checkin``, ``_recalc_work_sessions``,
``_attendance_backfill``) so the stub-frappe test harness
(``tests/test_attendance_admin_ops.py``) can monkeypatch them without a bench.
"""

from __future__ import annotations

import datetime as _dt

import frappe

WORK_SESSION_DOCTYPE = "VN Attendance Work Session"
CHECKIN_DOCTYPE = "Employee Checkin"
ATTENDANCE_DOCTYPE = "Attendance"
CORRECTION_DOCTYPE = "VN Attendance Correction Request"
EXCEPTION_DOCTYPE = "VN Attendance Exception"

HR_ROLES = ["HR Manager", "HR User", "System Manager"]
OT_APPROVER_ROLES = ["HR Manager", "System Manager"]

_VALID_LOG_TYPES = ("IN", "OUT")
_VALID_ATTENDANCE_STATUSES = ("Present", "Absent", "Half Day", "Work From Home")

# Full Work-Session projection for the admin detail drawer (E1).
_WORK_SESSION_DETAIL_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "company",
    "work_date",
    "shift_type",
    "shift_instance",
    "calculation_status",
    "planned_start",
    "planned_end",
    "actual_checkin",
    "actual_checkout",
    "actual_within_shift_hours",
    "regular_hours",
    "regular_night_hours",
    "total_actual_hours",
    "late_minutes",
    "early_leave_minutes",
    "raw_overtime_hours",
    "overtime_night_hours",
    "approved_overtime_hours",
    "payable_day",
    "absent",
    "has_leave",
    "leave_application",
    "need_review",
    "missing_checkin",
    "missing_checkout",
    "vn_auto_checkout",
    "attendance",
]

_CHECKIN_FIELDS = [
    "name",
    "employee",
    "time",
    "log_type",
    "device_id",
    "latitude",
    "longitude",
]


# --------------------------------------------------------------------------- #
# Small helpers (stdlib-only so the bench-free test harness stays light)
# --------------------------------------------------------------------------- #
def _require_hr() -> None:
    frappe.only_for(HR_ROLES)


def _require_ot_approver() -> None:
    frappe.only_for(OT_APPROVER_ROLES)


def _is_lm_of(employee: str) -> bool:
    """True khi ``employee.reports_to`` là Employee của caller (lazy, stub-safe).

    plan-team-attendance-desk-free §4 WP4 — Line-Manager scope probe for the
    team-attendance drawer ops. Resolves the employee util lazily so the
    bench-free harness stays light; any resolution failure → False (deny).
    """
    try:
        from gege_hr.gege_hr.utils import employee as emp_utils

        me = emp_utils.get_employee_for_user()
    except Exception:
        return False
    if not me:
        return False
    try:
        reports_to = frappe.db.get_value("Employee", employee, "reports_to")
    except Exception:
        return False
    return bool(reports_to) and reports_to == me


def _require_hr_or_lm_of(employee: str) -> None:
    """Permission gate for the /hr/team/attendance drawer punch ops (WP4).

    HR Manager / HR User / System Manager may act on anyone; a ``Line Manager``
    only on employees whose ``reports_to`` is them — semantics copied from
    ``admin._require_attendance_editor_for`` (no cross-team escalation).
    """
    roles = set(frappe.get_roles())
    if roles & set(HR_ROLES):
        return
    if roles & {"Line Manager"} and _is_lm_of(employee or ""):
        return
    frappe.throw(
        frappe._("Bạn chỉ được thao tác chấm công của nhân viên trong team của mình."),
        frappe.PermissionError,
    )


def _is_locked(work_date: str | None) -> bool:
    """True when a VN Monthly Attendance Period covering ``work_date`` is Locked.

    Mirrors ``attendance_sync.is_work_date_locked`` (kept local so the ops
    module has no gege_hr import at module scope); never raises.
    """
    if not work_date:
        return False
    try:
        return bool(
            frappe.db.get_all(
                "VN Monthly Attendance Period",
                filters=[
                    ["from_date", "<=", work_date],
                    ["to_date", ">=", work_date],
                    ["status", "=", "Locked"],
                ],
                limit_page_length=1,
            )
        )
    except Exception:
        return False


def _guard_writable_date(work_date: str | None, label: str = "ngày") -> None:
    if _is_locked(work_date):
        frappe.throw(
            frappe._("{0} ({1}) thuộc kỳ công đã khoá — không thể thao tác. Hãy mở khoá kỳ trước.").format(
                label, work_date or ""
            ),
            frappe.ValidationError,
        )


def _require_reason(reason: str | None) -> str:
    r = (reason or "").strip()
    if len(r) < 3:
        frappe.throw(
            frappe._("Lý do thao tác là bắt buộc (tối thiểu 3 ký tự) để lưu vết kiểm toán."),
            frappe.ValidationError,
        )
    return r


def _parse_dt(value) -> _dt.datetime | None:
    """Parse a portal-local datetime string → naive datetime (admin.py parity)."""
    if not value:
        return None
    v = str(value).strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return _dt.datetime.strptime(v, fmt)
        except ValueError:
            continue
    try:
        return _dt.datetime.fromisoformat(v)
    except ValueError:
        return None


def _parse_date(value) -> _dt.date | None:
    if not value:
        return None
    v = str(value).strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return _dt.datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def _norm_log_type(value: str | None) -> str:
    lt = (value or "").strip().upper()
    if lt in ("CLOCK IN", "CHECK IN"):
        lt = "IN"
    if lt in ("CLOCK OUT", "CHECK OUT"):
        lt = "OUT"
    if lt not in _VALID_LOG_TYPES:
        frappe.throw(frappe._("Loại lượt chấm phải là IN hoặc OUT."), frappe.ValidationError)
    return lt


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v or "").strip()]
    return [p.strip() for p in str(value).split(",") if p.strip()]


# ---- lazy gege_hr indirections (monkeypatch seams for bench-free tests) ---- #
def _audit(
    action: str,
    employee: str | None = None,
    reference_doctype: str | None = None,
    reference_name: str | None = None,
    description: str = "",
) -> None:
    """Best-effort VN Audit Event write — never blocks the business operation."""
    try:
        from gege_hr.gege_hr.api import audit as audit_api

        audit_api.log(
            action,
            company=None,
            employee=employee,
            reference_doctype=reference_doctype,
            reference_name=reference_name,
            description=description,
        )
    except Exception:
        try:
            frappe.log_error(title=f"attendance_admin_ops audit failed: {action}")
        except Exception:
            pass


def _recalc_for_checkin(doc) -> None:
    """Re-run the Work-Session calculation for a check-in doc (update/delete path)."""
    try:
        from gege_hr.gege_hr.api import attendance as att_api

        att_api.on_employee_checkin_create(doc)
    except Exception:
        try:
            frappe.log_error(title="attendance_admin_ops: recalc after checkin write failed")
        except Exception:
            pass


def _recalc_work_sessions(employee: str, work_date: str) -> None:
    """Recompute every Work Session around ``work_date`` for ``employee`` (delete path).

    ``work_date ± 1 day`` so an overnight shift whose punch lived on the edge
    still recalculates. Best-effort: failures are logged, never raised.
    """
    try:
        d = _parse_date(work_date)
        if not d:
            return
        rows = frappe.db.get_all(
            WORK_SESSION_DOCTYPE,
            filters=[
                ["employee", "=", employee],
                ["work_date", "between", [str(d - _dt.timedelta(days=1)), str(d + _dt.timedelta(days=1))]],
                ["docstatus", "!=", 2],
            ],
            fields=["name", "shift_instance"],
        )
        if not rows:
            return
        from gege_hr.gege_hr.utils import calc

        for r in rows:
            if r.get("shift_instance"):
                calc.persist_work_session(r["shift_instance"], calculate_mode="recalculate")
    except Exception:
        try:
            frappe.log_error(title="attendance_admin_ops: recalc after delete failed")
        except Exception:
            pass


def _publish_att_updated(employee, work_date) -> None:
    """Best-effort realtime ping (plan-team-attendance-desk-free WP9): open
    ``/hr/team/attendance`` tabs refetch the affected member row. Never raises;
    also the monkeypatch seam for the TA22 unit tests."""
    try:
        from gege_hr.gege_hr.api import attendance as _att_api

        _pub = getattr(_att_api, "_publish_team_attendance", None)
        if _pub:
            _pub(employee, work_date)
    except Exception:
        pass


def _attendance_backfill(from_date: str, to_date: str, employee: str | None) -> dict:
    """Delegate the WS → Attendance regeneration to ``attendance_sync`` (E6 core)."""
    from gege_hr.gege_hr.api import attendance_sync

    return attendance_sync.backfill_attendance(from_date=from_date, to_date=to_date, employee=employee)


def _commit() -> None:
    try:
        frappe.db.commit()
    except Exception:
        pass


def _safe_detail_fields() -> list:
    """Intersect the detail projection with the DocType's real columns.

    An unmigrated bench (or a field renamed upstream) must never 500 the
    drawer — unknown columns are dropped, mirroring the ``_safe_fields``
    philosophy of ``_ws_search_or_filters`` (DNA §6.6 A).
    """
    try:
        valid = set(frappe.meta.get_table_columns(WORK_SESSION_DOCTYPE) or [])
        if valid:
            return [f for f in _WORK_SESSION_DETAIL_FIELDS if f in valid]
    except Exception:
        pass
    return list(_WORK_SESSION_DETAIL_FIELDS)


# --------------------------------------------------------------------------- #
# E1 — Work Session detail (drawer payload)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def get_work_session_detail(name: str | None = None) -> dict:
    """Full admin detail for one Work Session: metrics + raw punches + corrections
    + exception + core Attendance link + period-lock flag."""
    _require_hr()
    name = (name or "").strip()
    if not name:
        frappe.throw(frappe._("Thiếu mã phiên làm việc."), frappe.MandatoryError)
    ws = frappe.db.get_value(WORK_SESSION_DOCTYPE, name, _safe_detail_fields(), as_dict=True)
    if not ws or not getattr(ws, "name", None):
        frappe.throw(frappe._("Phiên làm việc {0} không tồn tại.").format(name), frappe.DoesNotExistError)

    work_date = str(getattr(ws, "work_date", "") or "")

    # Punch window: planned_start.date() - 1d → planned_end.date() + 1d so an
    # overnight session catches both edges; falls back to the work_date day.
    start_day = end_day = _parse_date(work_date)
    ps, pe = _parse_dt(getattr(ws, "planned_start", None)), _parse_dt(getattr(ws, "planned_end", None))
    if ps and pe:
        start_day, end_day = min(ps.date(), _parse_date(work_date)), max(pe.date(), _parse_date(work_date))
    win_start = _dt.datetime.combine(start_day, _dt.time.min)
    win_end = _dt.datetime.combine(end_day + _dt.timedelta(days=1), _dt.time.min)

    checkins = (
        frappe.db.get_all(
            CHECKIN_DOCTYPE,
            filters=[
                ["employee", "=", ws.employee],
                ["time", ">=", win_start.strftime("%Y-%m-%d %H:%M:%S")],
                ["time", "<", win_end.strftime("%Y-%m-%d %H:%M:%S")],
            ],
            fields=_CHECKIN_FIELDS,
            order_by="time asc",
        )
        or []
    )

    corrections = (
        frappe.db.get_all(
            CORRECTION_DOCTYPE,
            filters={"employee": ws.employee, "work_date": work_date},
            fields=[
                "name",
                "correction_type",
                "reason",
                "requested_checkin_time",
                "requested_checkout_time",
                "workflow_state",
                "docstatus",
            ],
            order_by="creation desc",
        )
        or []
    )

    exception = frappe.db.get_value(
        EXCEPTION_DOCTYPE,
        {"employee": ws.employee, "work_date": work_date},
        ["name", "exception_type", "severity", "status"],
        as_dict=True,
    )
    attendance = frappe.db.get_value(
        ATTENDANCE_DOCTYPE,
        {"employee": ws.employee, "attendance_date": work_date},
        ["name", "status", "docstatus", "in_time", "out_time"],
        as_dict=True,
    )

    return {
        "work_session": ws,
        "checkins": checkins,
        "corrections": corrections,
        "exception": exception,
        "attendance": attendance,
        "locked": _is_locked(work_date),
    }


# --------------------------------------------------------------------------- #
# E2 — raw Employee Checkin directory
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def list_checkins(
    employee: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    log_type: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """Raw ``Employee Checkin`` rows for an employee in a date window.

    HR roles see anyone; a Line Manager only their own reports (WP4).The feed
    powers the /hr/team/attendance day-drawer punch tab.
    """
    employee = (employee or "").strip()
    if not employee:
        frappe.throw(frappe._("Thiếu mã nhân viên."), frappe.MandatoryError)
    _require_hr_or_lm_of(employee)
    end = _parse_date(to_date) or _dt.date.today()
    start = _parse_date(from_date) or (end - _dt.timedelta(days=30))
    if start > end:
        start, end = end, start

    filters: list = [
        ["employee", "=", employee],
        ["time", ">=", _dt.datetime.combine(start, _dt.time.min).strftime("%Y-%m-%d %H:%M:%S")],
        [
            "time",
            "<",
            _dt.datetime.combine(end + _dt.timedelta(days=1), _dt.time.min).strftime("%Y-%m-%d %H:%M:%S"),
        ],
    ]
    lt = (log_type or "").strip().upper()
    if lt:
        filters.append(["log_type", "=", _norm_log_type(lt)])
    try:
        limit = max(1, min(int(limit or 200), 1000))
    except (TypeError, ValueError):
        limit = 200

    return (
        frappe.db.get_all(
            CHECKIN_DOCTYPE,
            filters=filters,
            fields=_CHECKIN_FIELDS,
            order_by="time desc",
            limit_page_length=limit,
        )
        or []
    )


# --------------------------------------------------------------------------- #
# E3 / E4 / E5 — raw punch CRUD (manual HR corrections)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def create_checkin(
    employee: str | None = None,
    time: str | None = None,
    log_type: str | None = None,
    reason: str | None = None,
    device_id: str | None = None,
) -> dict:
    """Create a manual ``Employee Checkin`` on behalf of an employee (E3).

    ``time`` is portal-local ("YYYY-MM-DD HH:mm[:ss]" / ISO) and is stored as
    naive PORTAL WALL — the live DB frame (PHASE-1, same as
    ``admin_custom_checkin``). The after_insert hook recalculates the Work
    Session automatically. HR roles may act on anyone; a Line Manager only on
    their own reports (plan WP4).
    """
    employee = (employee or "").strip()
    if not employee or not frappe.db.exists("Employee", employee):
        frappe.throw(frappe._("Nhân viên không tồn tại."), frappe.DoesNotExistError)
    _require_hr_or_lm_of(employee)
    lt = _norm_log_type(log_type)
    reason = _require_reason(reason)
    dt = _parse_dt(time)
    if dt is None:
        frappe.throw(frappe._("Thời gian không hợp lệ (dùng YYYY-MM-DD HH:mm)."), frappe.ValidationError)

    work_date = dt.date().isoformat()
    _guard_writable_date(work_date, "Ngày chấm")

    doc = frappe.get_doc(
        {
            "doctype": CHECKIN_DOCTYPE,
            "employee": employee,
            "log_type": lt,
            "time": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "device_id": device_id or "gege_hr-admin",
        }
    )
    doc.insert(ignore_permissions=True)
    # after_insert hook → recalc; the explicit call covers stubs/benches where
    # hooks are disabled.
    _recalc_for_checkin(doc)
    _audit(
        "Admin Manual Checkin",
        employee=employee,
        reference_doctype=CHECKIN_DOCTYPE,
        reference_name=getattr(doc, "name", None),
        description=f"HR tạo lượt chấm {lt} {dt.strftime('%Y-%m-%d %H:%M:%S')} — lý do: {reason}",
    )
    _commit()
    _publish_att_updated(employee, work_date)
    return {
        "ok": True,
        "name": getattr(doc, "name", None),
        "log_type": lt,
        "time": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "message": frappe._("Đã tạo lượt chấm {0}.").format(getattr(doc, "name", "")),
    }


@frappe.whitelist()
def update_checkin(
    name: str | None = None,
    time: str | None = None,
    log_type: str | None = None,
    reason: str | None = None,
) -> dict:
    """Fix a wrong punch's time / log_type in place (E4). Recalcs the session.

    The update path fires no ``after_insert`` hook, so the Work Session is
    recomputed explicitly (same pattern as ``admin._upsert_employee_checkin``).
    HR roles may act on anyone; a Line Manager only on their own reports (WP4).
    """
    name = (name or "").strip()
    if not name:
        frappe.throw(frappe._("Thiếu mã lượt chấm."), frappe.MandatoryError)
    reason = _require_reason(reason)
    existing = frappe.db.get_value(CHECKIN_DOCTYPE, name, ["employee", "time"], as_dict=True)
    if not existing or not getattr(existing, "employee", None):
        frappe.throw(frappe._("Lượt chấm {0} không tồn tại.").format(name), frappe.DoesNotExistError)
    _require_hr_or_lm_of(getattr(existing, "employee", None))

    old_dt = _parse_dt(getattr(existing, "time", None))
    new_dt = _parse_dt(time) if time else None
    new_lt = _norm_log_type(log_type) if log_type else None
    if new_dt is None and new_lt is None:
        frappe.throw(frappe._("Cần cung cấp thời gian hoặc loại lượt chấm mới."), frappe.ValidationError)

    # Lock guard on BOTH the old and the new day (a move across days must not
    # escape a locked period on either side).
    if old_dt:
        _guard_writable_date(old_dt.date().isoformat(), "Ngày cũ")
    if new_dt:
        _guard_writable_date(new_dt.date().isoformat(), "Ngày mới")

    values: dict = {}
    if new_dt:
        values["time"] = new_dt.strftime("%Y-%m-%d %H:%M:%S")
    if new_lt:
        values["log_type"] = new_lt
    frappe.db.set_value(CHECKIN_DOCTYPE, name, values, update_modified=True)

    doc = frappe.get_doc(CHECKIN_DOCTYPE, name)
    _recalc_for_checkin(doc)
    _audit(
        "Admin Edit Checkin",
        employee=getattr(existing, "employee", None),
        reference_doctype=CHECKIN_DOCTYPE,
        reference_name=name,
        description=(
            f"HR sửa lượt chấm {name}: "
            f"{(old_dt.strftime('%Y-%m-%d %H:%M:%S') if old_dt else '?')}"
            f"{'/' + new_dt.strftime('%Y-%m-%d %H:%M:%S') if new_dt else ''} — lý do: {reason}"
        ),
    )
    _commit()
    _publish_att_updated(
        getattr(existing, "employee", None),
        ((new_dt or old_dt).date().isoformat() if (new_dt or old_dt) else None),
    )
    return {"ok": True, "name": name, "message": frappe._("Đã cập nhật lượt chấm {0}.").format(name)}


@frappe.whitelist()
def delete_checkin(name: str | None = None, reason: str | None = None) -> dict:
    """Delete a stray/duplicate punch (E5). Recalcs the surrounding sessions."""
    name = (name or "").strip()
    if not name:
        frappe.throw(frappe._("Thiếu mã lượt chấm."), frappe.MandatoryError)
    reason = _require_reason(reason)
    existing = frappe.db.get_value(CHECKIN_DOCTYPE, name, ["employee", "time"], as_dict=True)
    if not existing or not getattr(existing, "employee", None):
        frappe.throw(frappe._("Lượt chấm {0} không tồn tại.").format(name), frappe.DoesNotExistError)
    _require_hr_or_lm_of(getattr(existing, "employee", None))

    old_dt = _parse_dt(getattr(existing, "time", None))
    work_date = old_dt.date().isoformat() if old_dt else None
    _guard_writable_date(work_date, "Ngày chấm")

    frappe.delete_doc(CHECKIN_DOCTYPE, name)
    if work_date:
        _recalc_work_sessions(getattr(existing, "employee", None), work_date)
    _audit(
        "Admin Delete Checkin",
        employee=getattr(existing, "employee", None),
        reference_doctype=CHECKIN_DOCTYPE,
        reference_name=name,
        description=(f"HR xoá lượt chấm {name} ({work_date or '?'}) — lý do: {reason}"),
    )
    _commit()
    _publish_att_updated(getattr(existing, "employee", None), work_date)
    return {"ok": True, "name": name, "message": frappe._("Đã xoá lượt chấm {0}.").format(name)}


# --------------------------------------------------------------------------- #
# E6 — generate core Attendance from Work Sessions
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def generate_attendance(
    from_date: str | None = None,
    to_date: str | None = None,
    employee: str | None = None,
) -> dict:
    """Regenerate the core ``Attendance`` rows for a date window (E6).

    Thin HR-gated wrapper over ``attendance_sync.backfill_attendance`` (the
    FIX-1 engine): idempotent per (employee, attendance_date); rows submit only
    inside a Locked monthly period. Audited here because the sync module is
    also invoked doc-event-side where the actor is a background job.
    """
    _require_hr()
    start = _parse_date(from_date) or (_dt.date.today() - _dt.timedelta(days=7))
    end = _parse_date(to_date) or _dt.date.today()
    if start > end:
        start, end = end, start
    employee = (employee or "").strip() or None

    result = _attendance_backfill(start.isoformat(), end.isoformat(), employee) or {}
    _audit(
        "Admin Generate Attendance",
        employee=employee,
        reference_doctype=WORK_SESSION_DOCTYPE,
        reference_name=None,
        description=(
            f"HR sinh Attendance {start.isoformat()} → {end.isoformat()}"
            f" (employee={employee or 'all'}): synced={result.get('synced', 0)}, "
            f"skipped={result.get('skipped', 0)}"
        ),
    )
    _commit()
    return {
        "ok": True,
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "employee": employee,
        **result,
    }


# --------------------------------------------------------------------------- #
# E7 — bulk mark attendance (Employee Attendance Tool parity)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def mark_attendance_bulk(
    employees=None,
    attendance_date: str | None = None,
    status: str | None = None,
    overwrite: int = 0,
) -> dict:
    """Mark one attendance status for many employees on one date (E7).

    Desk's *Employee Attendance Tool* parity: ``employees`` is a list (or a
    comma-separated string); ``status`` ∈ Present / Absent / Half Day / Work
    From Home. Existing non-cancelled rows are skipped unless ``overwrite``;
    rows submit only when the date sits in a Locked monthly period (same
    decision as ``attendance_sync``).
    """
    # WP4: Line Manager may bulk-mark their OWN reports only (per-row scope
    # check below); HR roles keep company-wide reach.
    caller_roles = set(frappe.get_roles())
    if not (caller_roles & (set(HR_ROLES) | {"Line Manager"})):
        frappe.throw(frappe._("Bạn không có quyền thực hiện thao tác này."), frappe.PermissionError)
    is_hr_caller = bool(caller_roles & set(HR_ROLES))
    day = _parse_date(attendance_date)
    if not day:
        frappe.throw(frappe._("Thiếu ngày chấm công."), frappe.MandatoryError)
    st = (status or "").strip()
    if st not in _VALID_ATTENDANCE_STATUSES:
        frappe.throw(
            frappe._("Trạng thái phải là một trong: {0}.").format(", ".join(_VALID_ATTENDANCE_STATUSES)),
            frappe.ValidationError,
        )
    emps = _as_list(employees)
    if not emps:
        frappe.throw(frappe._("Chọn ít nhất một nhân viên."), frappe.MandatoryError)

    work_date = day.isoformat()
    _guard_writable_date(work_date, "Ngày chấm công")
    locked_day = _is_locked(work_date)

    created, skipped, submitted, results = 0, 0, 0, []
    for emp in emps:
        if not frappe.db.exists("Employee", emp):
            results.append({"employee": emp, "ok": False, "error": "Nhân viên không tồn tại"})
            continue
        if not is_hr_caller and not _is_lm_of(emp):
            results.append({"employee": emp, "ok": False, "error": "Ngoài phạm vi team của bạn"})
            continue
        existing = frappe.db.get_value(
            ATTENDANCE_DOCTYPE,
            {"employee": emp, "attendance_date": work_date, "docstatus": ["<", 2]},
            ["name", "docstatus"],
            as_dict=True,
        )
        if existing and not int(overwrite or 0):
            skipped += 1
            results.append({"employee": emp, "ok": True, "skipped": existing.name})
            continue
        try:
            if existing:
                frappe.db.set_value(ATTENDANCE_DOCTYPE, existing.name, {"status": st}, update_modified=True)
                att_name = existing.name
            else:
                doc = frappe.get_doc(
                    {
                        "doctype": ATTENDANCE_DOCTYPE,
                        "employee": emp,
                        "attendance_date": work_date,
                        "status": st,
                    }
                )
                doc.insert(ignore_permissions=True)
                att_name = getattr(doc, "name", None)
                if locked_day and not int(getattr(doc, "docstatus", 0) or 0):
                    try:
                        frappe.get_doc(ATTENDANCE_DOCTYPE, att_name).submit()
                        submitted += 1
                    except Exception:
                        frappe.log_error(title="mark_attendance_bulk submit failed")
            created += 1
            results.append({"employee": emp, "ok": True, "attendance": att_name})
            _audit(
                "Admin Mark Attendance",
                employee=emp,
                reference_doctype=ATTENDANCE_DOCTYPE,
                reference_name=att_name,
                description=f"HR chấm thủ công {st} ngày {work_date} (overwrite={int(overwrite or 0)})",
            )
        except Exception:
            frappe.log_error(title=f"mark_attendance_bulk failed for {emp}")
            results.append({"employee": emp, "ok": False, "error": "Lỗi khi tạo Attendance"})

    _commit()
    _publish_att_updated(None, work_date)
    return {
        "ok": True,
        "attendance_date": work_date,
        "status": st,
        "created": created,
        "skipped": skipped,
        "submitted": submitted,
        "results": results,
    }


# --------------------------------------------------------------------------- #
# E8 — approve session overtime
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def approve_session_overtime(
    work_session: str | None = None,
    hours: float | None = None,
    note: str | None = None,
) -> dict:
    """Approve OT on one Work Session (E8). HR Manager / System Manager only.

    ``approved_overtime_hours = min(hours ?? raw_overtime_hours, raw_overtime_hours)``
    — an approval can never exceed the engine-computed raw OT. Refuses when the
    session has no raw OT or its date is inside a Locked period.
    """
    _require_ot_approver()
    ws_name = (work_session or "").strip()
    if not ws_name:
        frappe.throw(frappe._("Thiếu mã phiên làm việc."), frappe.MandatoryError)
    ws = frappe.db.get_value(
        WORK_SESSION_DOCTYPE,
        ws_name,
        ["employee", "work_date", "raw_overtime_hours", "approved_overtime_hours", "calculation_status"],
        as_dict=True,
    )
    if not ws or not getattr(ws, "employee", None):
        frappe.throw(frappe._("Phiên làm việc {0} không tồn tại.").format(ws_name), frappe.DoesNotExistError)

    _guard_writable_date(str(getattr(ws, "work_date", "") or ""), "Ngày công")

    try:
        raw = float(getattr(ws, "raw_overtime_hours", 0) or 0)
    except (TypeError, ValueError):
        raw = 0.0
    if raw <= 0:
        frappe.throw(frappe._("Phiên này không có giờ tăng ca thô để duyệt."), frappe.ValidationError)
    try:
        requested = float(hours) if hours is not None else raw
    except (TypeError, ValueError):
        requested = raw
    approved = round(min(requested, raw), 4)

    frappe.db.set_value(
        WORK_SESSION_DOCTYPE,
        ws_name,
        {"approved_overtime_hours": approved},
        update_modified=True,
    )
    _audit(
        "Admin Approve Overtime",
        employee=getattr(ws, "employee", None),
        reference_doctype=WORK_SESSION_DOCTYPE,
        reference_name=ws_name,
        description=(
            f"HR duyệt OT phiên {ws_name}: raw={raw} → approved={approved}"
            f"{(' — ' + (note or '').strip()) if (note or '').strip() else ''}"
        ),
    )
    _commit()
    _publish_att_updated(getattr(ws, "employee", None), str(getattr(ws, "work_date", "") or ""))
    return {
        "ok": True,
        "work_session": ws_name,
        "raw_overtime_hours": raw,
        "approved_overtime_hours": approved,
        "message": frappe._("Đã duyệt {0}h tăng ca.").format(approved),
    }
