"""
Attendance API — plan v5 §10.2 + §8.6 (12-state button).

Endpoints implemented in this Milestone-1 foundation:

* ``today_status``        → button_state + shift + times + geo/lock context
* ``mobile_checkin``      → GPS-aware check-in/out with idempotency + rate-limit
* ``my_logs``             → employee raw check-in/out log (Employee Checkin)
* ``my_monthly_summary``  → monthly worked/payable/late/absent/leave/OT totals
* ``team_daily_status``   → manager team snapshot (M2; minimal impl here)
* ``on_employee_checkin_create`` / ``auto_mark_absent_job`` — hook stubs

The full Work-Session calculation engine (segments, night split, OT) ships in
Milestone 2; until then these endpoints operate on Frappe HR's ``Employee
Checkin`` + ``Shift Assignment`` so the frontend has real, correct data for the
check-in button and the employee log.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import frappe
from frappe import _
from frappe.utils import add_days, flt, get_datetime, getdate

from gege_hr.gege_hr.api import audit as audit_api
from gege_hr.gege_hr.utils import employee as emp_utils
from gege_hr.gege_hr.utils import tz as tz_utils
from gege_hr.gege_hr.utils.ratelimit import rate_limit
from gege_hr.gege_hr.utils.request_workflow import send_for_approval

# ---------------------------------------------------------------------------
# Button states — mirror hr-ui CHECKIN_STATES exactly (plan §8.6).
# ---------------------------------------------------------------------------
STATE = {
    "NO_SHIFT": "NO_SHIFT_TODAY",
    "BEFORE_WINDOW": "BEFORE_CHECKIN_WINDOW",
    "CAN_CHECK_IN": "CAN_CHECK_IN",
    "CHECKED_IN": "CHECKED_IN",
    "CAN_CHECK_OUT": "CAN_CHECK_OUT",
    "CHECKED_OUT": "CHECKED_OUT",
    "MISSING_CHECKOUT": "MISSING_CHECKOUT",
    "LOCKED": "LOCKED_PERIOD",
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _resolve_employee(employee: str | None) -> str:
    """Resolve the employee for the caller (param > session user) → its *name*.

    ``employee`` may arrive as a full object from the SPA; coerce to the name
    string so it is safe to use as a ``filters`` value.
    """
    if employee:
        return emp_utils.emp_name(employee)
    emp = emp_utils.get_employee_for_user()
    if not emp:
        frappe.throw(_("Tài khoản này chưa được liên kết với nhân viên."), frappe.PermissionError)
    return emp


def _checkins_for(employee: str, day: date) -> list[dict]:
    """All Employee Checkin rows for ``employee`` on a portal-date ``day``.

    Frappe stores datetimes as UTC; convert the portal-day bounds to UTC first
    and query with a list-of-conditions filter (the field appears twice).
    """
    employee = emp_utils.emp_name(employee)
    tz = tz_utils.get_tzinfo()
    utc = tz_utils.ZoneInfo("UTC")
    start_utc = datetime.combine(day, datetime.min.time(), tzinfo=tz).astimezone(utc)
    end_utc = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=tz).astimezone(utc)
    return (
        frappe.db.get_all(
            "Employee Checkin",
            filters=[
                ["employee", "=", employee],
                ["time", ">=", start_utc.strftime("%Y-%m-%d %H:%M:%S")],
                ["time", "<", end_utc.strftime("%Y-%m-%d %H:%M:%S")],
            ],
            fields=["name", "employee", "time", "log_type", "device_id", "latitude", "longitude"],
            order_by="time asc",
        )
        or []
    )


def _today_shift(employee: str, day: date) -> dict | None:
    """Best-effort shift context for ``day`` from Frappe HR Shift Assignment."""
    employee = emp_utils.emp_name(employee)
    shift_name = frappe.db.get_value(
        "Shift Assignment",
        {"employee": employee, "status": "Active", "start_date": ["<=", day], "docstatus": 1},
        "shift_type",
    )
    if not shift_name:
        return None
    st = frappe.get_cached_doc("Shift Type", shift_name)
    start_time = getattr(st, "start_time", None)
    end_time = getattr(st, "end_time", None)
    if not start_time or not end_time:
        return None
    planned_start, planned_end = tz_utils.planned_window(day, start_time, end_time)
    return {
        "shift_type": shift_name,
        "start_time": str(start_time),
        "end_time": str(end_time),
        "is_overnight": tz_utils.is_overnight(start_time, end_time),
        "planned_start": tz_utils.utc_iso(planned_start),
        "planned_end": tz_utils.utc_iso(planned_end),
        "work_date": day.isoformat(),
    }


def _derive_button_state(shift: dict | None, checkins: list[dict], now_local: datetime) -> str:
    """Compute the authoritative button_state (12-state machine, server side)."""
    if shift is None:
        return STATE["NO_SHIFT"]

    # Lock check (any closed monthly period covering this date).
    if _is_date_locked(shift["work_date"]):
        return STATE["LOCKED"]

    planned_start = tz_utils.to_portal(datetime.fromisoformat(shift["planned_start"].replace("Z", "+00:00")))
    earliest_in = planned_start - timedelta(minutes=_shift_minutes("vn_earliest_checkin_minutes", 60))

    has_in = any((c.log_type or "").upper() in ("IN", "CLOCK IN") for c in checkins)
    has_out = any((c.log_type or "").upper() in ("OUT", "CLOCK OUT") for c in checkins)

    if has_in and has_out:
        return STATE["CHECKED_OUT"]
    if has_in:
        # Shift already started (past planned_start) and still no OUT → can check out.
        if now_local >= planned_start:
            return STATE["CAN_CHECK_OUT"]
        return STATE["CHECKED_IN"]
    if now_local < earliest_in:
        return STATE["BEFORE_WINDOW"]
    return STATE["CAN_CHECK_IN"]


def _shift_minutes(field: str, default: int) -> int:
    """Read a Shift Type VN custom-field minute setting with a sane default."""
    try:
        return int(frappe.db.get_value("Shift Type", {"name": ["like", "%"]}, field) or default)
    except Exception:
        return default


def _is_date_locked(work_date: str) -> bool:
    """True when a VN Monthly Attendance Period covering ``work_date`` is locked."""
    try:
        return bool(
            frappe.db.exists(
                "VN Monthly Attendance Period",
                {"start_date": ["<=", work_date], "end_date": [">=", work_date], "is_locked": 1},
            )
        )
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def today_status(employee: str | None = None) -> dict:
    """Plan §10.2 — button_state + shift + times + geo/lock context."""
    emp = _resolve_employee(employee)
    day = tz_utils.now_in_portal().date()
    shift = _today_shift(emp, day)
    checkins = _checkins_for(emp, day) if shift else []
    now_local = tz_utils.now_in_portal()
    button_state = _derive_button_state(shift, checkins, now_local)

    setting = _portal_setting()
    work_location = _work_location_for(emp)

    return {
        "employee": emp,
        "work_date": day.isoformat(),
        "button_state": button_state,
        "shift": shift,
        "times": {
            "last_checkin": checkins[-1]["time"] if checkins else None,
            "checkin_count": len(checkins),
        },
        "geo": {
            "require_geolocation": bool(setting.get("require_geolocation")),
            "work_location": work_location,
        },
        "locked": _is_date_locked(day.isoformat()),
    }


@frappe.whitelist()
def mobile_checkin(
    employee: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    client_request_id: str | None = None,
    client_timestamp: str | None = None,
    device_id: str | None = None,
    **kwargs,
) -> dict:
    """Plan §10.2 — GPS-aware check-in/out with idempotency + 1/3s rate limit.

    Determines IN vs OUT by parity of the day's existing logs, creates a
    ``VN Mobile Checkin Attempt`` audit record + an ``Employee Checkin``, then
    returns the refreshed ``button_state``.
    """
    emp = _resolve_employee(employee)
    if not client_request_id:
        client_request_id = f"srv-{datetime.utcnow().timestamp():.0f}"

    # Idempotency: a second tap with the same client_request_id is a no-op.
    existing = frappe.db.get_value("VN Mobile Checkin Attempt", {"client_request_id": client_request_id})
    if existing:
        return _checkin_result(emp, message="Yêu cầu đã được xử lý trước đó.")

    # Rate limit: max 1 request / 3s per employee (plan §Security).
    rate_limit(f"checkin:{emp}", max_requests=1, window_seconds=3)

    day = tz_utils.now_in_portal().date()
    shift = _today_shift(emp, day)
    checkins = _checkins_for(emp, day)
    log_type = "OUT" if _has_in_only(checkins) else "IN"

    # Geofence server-side pre-check (best-effort; client already guards).
    _enforce_geofence(emp, latitude, longitude)

    server_now = tz_utils.now_in_portal()

    # Audit record (VN Mobile Checkin Attempt) — created before the checkin so a
    # failure leaves an audit trail.
    attempt = frappe.get_doc(
        {
            "doctype": "VN Mobile Checkin Attempt",
            "employee": emp,
            "client_request_id": client_request_id,
            "device_id": device_id,
            "client_timestamp": client_timestamp,
            "server_timestamp": server_now.isoformat(),
            "latitude": flt(latitude) if latitude is not None else None,
            "longitude": flt(longitude) if longitude is not None else None,
            "intended_log_type": log_type,
            "status": "Success",
        }
    )
    attempt.insert()

    # The actual Frappe HR check-in record (drives downstream recalculation).
    frappe.get_doc(
        {
            "doctype": "Employee Checkin",
            "employee": emp,
            "log_type": log_type,
            "time": server_now.astimezone(tz_utils.ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S"),
            "device_id": device_id or "gege_hr-mobile",
            "latitude": flt(latitude) if latitude is not None else None,
            "longitude": flt(longitude) if longitude is not None else None,
        }
    ).insert()

    frappe.db.commit()
    refreshed = _checkin_result(emp, shift=shift)
    refreshed["log_type"] = log_type
    return refreshed


def _has_in_only(checkins: list[dict]) -> bool:
    has_in = any((c.log_type or "").upper() in ("IN", "CLOCK IN") for c in checkins)
    has_out = any((c.log_type or "").upper() in ("OUT", "CLOCK OUT") for c in checkins)
    return has_in and not has_out


def _enforce_geofence(employee: str, latitude, longitude) -> None:
    """Block check-in when GPS is required and the employee is outside the geofence."""
    setting = _portal_setting()
    if not setting.get("require_geolocation"):
        return
    if latitude is None or longitude is None:
        frappe.throw(_("Vui lòng bật GPS để chấm công."), frappe.ValidationError)
    loc = _work_location_for(employee)
    if not loc or loc.get("latitude") is None:
        return  # no geofence configured → allow
    allowed_m = loc.get("allowed_radius_meters", 50)
    from math import asin, cos, radians, sin, sqrt

    def _haversine(la1, lo1, la2, lo2):
        R = 6371000.0
        dla = radians(la2 - la1)
        dlo = radians(lo2 - lo1)
        a = sin(dla / 2) ** 2 + cos(radians(la1)) * cos(radians(la2)) * sin(dlo / 2) ** 2
        return 2 * R * asin(sqrt(a))

    dist = _haversine(float(latitude), float(longitude), loc["latitude"], loc["longitude"])
    if dist > allowed_m:
        frappe.throw(_("Bạn đang ngoài phạm vi chấm công (%.0fm).") % dist, frappe.ValidationError)


def _checkin_result(employee: str, shift: dict | None = None, message: str | None = None) -> dict:
    """Recompute button_state + return the standard checkin result payload."""
    day = tz_utils.now_in_portal().date()
    if shift is None:
        shift = _today_shift(employee, day)
    checkins = _checkins_for(employee, day) if shift else []
    state = _derive_button_state(shift, checkins, tz_utils.now_in_portal())
    msg = message or _state_message(state)
    return {
        "ok": True,
        "button_state": state,
        "message": msg,
        "time": tz_utils.utc_iso(tz_utils.now_in_portal()),
    }


def _state_message(state: str) -> str:
    return {
        STATE["CHECKED_IN"]: "Đã chấm công vào",
        STATE["CAN_CHECK_OUT"]: "Đã chấm công vào",
        STATE["CHECKED_OUT"]: "Đã chấm công ra",
        STATE["MISSING_CHECKOUT"]: "Thiếu chấm công ra",
        STATE["LOCKED"]: "Kỳ công đã khóa",
        STATE["BEFORE_WINDOW"]: "Chưa đến giờ chấm công",
        STATE["CAN_CHECK_IN"]: "Đã chấm công vào",
        STATE["NO_SHIFT"]: "Không có ca hôm nay",
    }.get(state, "Đã chấm công")


def _portal_setting() -> dict:
    try:
        s = frappe.get_cached_doc("VN HR Portal Setting", "VN HR Portal Setting")
        return {
            "enable_mobile_checkin": bool(s.enable_mobile_checkin),
            "require_geolocation": bool(s.require_geolocation),
            "require_selfie": bool(s.require_selfie),
            "enable_device_sync": bool(s.enable_device_sync),
        }
    except Exception:
        return {
            "enable_mobile_checkin": True,
            "require_geolocation": True,
            "require_selfie": False,
            "enable_device_sync": False,
        }


def _work_location_for(employee: str) -> dict | None:
    # The Employee's work location is the gege_hr custom field
    # ``default_work_location`` (it may not be installed on a site that hasn't
    # run migrate yet) — guard so today_status never 500s.
    try:
        loc_name = (
            frappe.db.get_value("Employee", employee, "default_work_location")
            if frappe.get_meta("Employee").has_field("default_work_location")
            else None
        )
    except Exception:
        loc_name = None
    loc_name = loc_name or frappe.db.get_value(
        "VN HR Portal Setting", "VN HR Portal Setting", "default_work_location"
    )
    if not loc_name:
        return None
    loc = frappe.db.get_value(
        "VN Work Location",
        loc_name,
        ["name", "location_name", "latitude", "longitude", "allowed_radius_meters"],
        as_dict=True,
    )
    if not loc:
        return None
    return {
        "name": loc.name,
        "label": loc.location_name,
        "latitude": loc.latitude,
        "longitude": loc.longitude,
        "allowed_radius_meters": loc.allowed_radius_meters or 50,
    }


@frappe.whitelist()
def my_logs(
    employee: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    """Plan §10.2 — employee raw check-in/out log."""
    emp = _resolve_employee(employee)
    today = tz_utils.now_in_portal().date()
    start = getdate(from_date) if from_date else add_days(today, -30)
    end = getdate(to_date) if to_date else today
    rows = frappe.db.get_all(
        "Employee Checkin",
        filters={"employee": emp, "time": ["between", [start, add_days(end, 1)]]},
        fields=["name", "time", "log_type", "device_id", "latitude", "longitude"],
        order_by="time desc",
        limit_page_length=500,
    )
    for r in rows:
        r["log_type"] = (r.log_type or "").upper()
    return rows


def _parse_year_month(year, month, now):
    """Parse ``year``/``month`` args into (year, month) ints.

    Accepts ``month`` as an int (1-12) or a ``'YYYY-MM'`` / ``'YYYY/MM'`` string
    (the SPA's ``monthKey``). When a combined string is supplied it also drives
    the year. Falls back to the portal "now" for anything missing/invalid.
    """
    y = now.year
    m = now.month
    if year not in (None, "", 0):
        try:
            y = int(year)
        except (TypeError, ValueError):
            pass
    if month not in (None, "", 0):
        ms = str(month).strip()
        for sep in ("-", "/"):
            if sep in ms:
                ys, _, ms = ms.partition(sep)
                try:
                    y, m = int(ys), int(ms)
                except ValueError:
                    pass
                return y, m
        try:
            m = int(ms)
        except ValueError:
            pass
    return y, m


@frappe.whitelist()
def my_monthly_summary(
    employee: str | None = None, year: int | None = None, month: int | None = None
) -> dict:
    """Plan §10.2 — monthly worked/payable/late/absent/leave/OT totals.

    Milestone-1 uses Attendance (Frappe HR) counts; the Work-Session engine
    (M2) will replace these with precise payable/OT figures.
    """
    emp = _resolve_employee(employee)
    now = tz_utils.now_in_portal()
    y, m = _parse_year_month(year, month, now)
    start = date(y, m, 1)
    end = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)

    def _count(status):
        return frappe.db.count(
            "Attendance", {"employee": emp, "status": status, "attendance_date": ["between", [start, end]]}
        )

    rows = frappe.db.get_all(
        "Attendance",
        filters={"employee": emp, "attendance_date": ["between", [start, end]]},
        fields=[
            "attendance_date",
            "status",
            "shift",
            "in_time",
            "out_time",
            "late_entry",
            "early_exit",
            "working_hours",
        ],
        order_by="attendance_date desc",
    )
    return {
        "employee": emp,
        "year": y,
        "month": m,
        "summary": {
            "present": _count("Present"),
            "absent": _count("Absent"),
            "leave": _count("On Leave"),
            "half_day": _count("Half Day"),
            "worked_days": _count("Present") + 0.5 * _count("Half Day"),
            "payable_days": _count("Present") + 0.5 * _count("Half Day"),
            "late_days": frappe.db.count(
                "Attendance",
                {"employee": emp, "late_entry": 1, "attendance_date": ["between", [start, end]]},
            ),
            "overtime_hours": 0.0,  # populated by M2 Work-Session engine
        },
        "rows": rows,
    }


@frappe.whitelist()
def team_daily_status(date_str: str | None = None) -> list[dict]:
    """Plan §10.2 — manager snapshot: one row per team member for a day."""
    frappe.only_for(["HR Manager", "HR User", "System Manager"])
    day = getdate(date_str) if date_str else tz_utils.now_in_portal().date()
    manager_emp = emp_utils.get_employee_for_user()
    members = frappe.db.get_all(
        "Employee",
        filters={"status": "Active", "reports_to": manager_emp},
        fields=["name", "employee_name", "designation"],
    )
    out = []
    for m in members:
        att = frappe.db.get_value(
            "Attendance",
            {"employee": m.name, "attendance_date": day},
            ["status", "in_time", "out_time", "late_entry", "early_exit"],
            as_dict=True,
        )
        out.append(
            {
                **m,
                "status": (att.status if att else "Not marked"),
                "in_time": att.in_time if att else None,
                "out_time": att.out_time if att else None,
                "late_entry": bool(att and att.late_entry),
                "early_exit": bool(att and att.early_exit),
            }
        )
    return out


@frappe.whitelist()
def team_attendance(
    manager: str = "",
    from_date: str = "",
    to_date: str = "",
) -> dict:
    """Plan §10.2 / §13 — manager's team attendance across a date range.

    Returns one member row carrying per-day presence plus a period summary.
    Honours Frappe role permissions (HR / Line Manager); reads ``Employee`` and
    ``Attendance`` only. Defaults to the current month when no range is given.
    """
    frappe.only_for(["HR Manager", "HR User", "System Manager", "Line Manager"])
    manager_emp = emp_utils.emp_name(manager) if manager else emp_utils.get_employee_for_user()
    if from_date and to_date:
        start = getdate(from_date)
        end = getdate(to_date)
    else:
        today = tz_utils.now_in_portal().date()
        start = today.replace(day=1)
        end = today

    members = frappe.db.get_all(
        "Employee",
        filters={"status": "Active", "reports_to": manager_emp},
        fields=["name", "employee_name", "designation"],
    )

    summary = {"present": 0, "late": 0, "absent": 0, "on_leave": 0}
    out_members = []
    for m in members:
        rows = frappe.db.get_all(
            "Attendance",
            filters={
                "employee": m.name,
                "attendance_date": ["between", [start, end]],
                "docstatus": 1,
            },
            fields=["attendance_date", "status", "in_time", "out_time", "late_entry", "early_exit"],
            order_by="attendance_date asc",
        )
        by_date = {str(r.attendance_date): r for r in rows}
        days = []
        cur = start
        while cur <= end:
            att = by_date.get(str(cur))
            days.append(
                {
                    "work_date": str(cur),
                    "status": (att.status if att else "Not marked"),
                    "checkin_time": (att.in_time if att else None),
                    "checkout_time": (att.out_time if att else None),
                    "late_minutes": 0,  # not modelled on core Attendance
                    "early_leave_minutes": 0,
                }
            )
            if att:
                if att.status == "Present":
                    summary["present"] += 1
                    if att.late_entry:
                        summary["late"] += 1
                elif att.status == "Absent":
                    summary["absent"] += 1
                elif att.status == "On Leave":
                    summary["on_leave"] += 1
            cur += timedelta(days=1)
        out_members.append({**m, "days": days})

    return {
        "from_date": str(start),
        "to_date": str(end),
        "members": out_members,
        "summary": summary,
    }


@frappe.whitelist()
def get_exceptions(
    status: str = "",
    employee: str = "",
    work_date: str = "",
    from_date: str = "",
    to_date: str = "",
    limit: int = 200,
) -> list[dict]:
    """Plan §2.6.7 — list ``VN Attendance Exception`` rows for the HR screen.

    Honours Frappe role permissions (HR Manager / HR User have read via the
    doctype JSON). Gated by ``frappe.only_for`` (no permission bypass).
    """
    frappe.only_for(["HR Manager", "HR User", "System Manager"])
    filters = []
    if status:
        filters.append(["status", "=", status])
    if employee:
        filters.append(["employee", "=", emp_utils.emp_name(employee)])
    if work_date:
        filters.append(["work_date", "=", getdate(work_date)])
    if from_date:
        filters.append(["work_date", ">=", getdate(from_date)])
    if to_date:
        filters.append(["work_date", "<=", getdate(to_date)])
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 200
    return frappe.get_all(
        "VN Attendance Exception",
        fields=[
            "name",
            "employee",
            "employee_name",
            "work_date",
            "shift_instance",
            "work_session",
            "attendance",
            "exception_type",
            "severity",
            "description",
            "status",
            "assigned_to",
            "resolution_type",
            "resolution_note",
            "resolved_by",
            "resolved_at",
        ],
        filters=filters or None,
        order_by="work_date desc",
        limit_page_length=limit,
    )


@frappe.whitelist()
def resolve_exception(
    name: str,
    status: str = "Resolved",
    resolution_type: str = "",
    resolution_note: str = "",
) -> dict:
    """Plan §2.6.7 — resolve / ignore a ``VN Attendance Exception`` (HR only).

    Honours Frappe role permissions (HR Manager has write on the doctype).
    """
    frappe.only_for(["HR Manager", "System Manager"])
    name = (name or "").strip()
    if not name:
        frappe.throw(_("name là bắt buộc."))
    doc = frappe.get_doc("VN Attendance Exception", name)
    if status:
        doc.status = status
    if resolution_type:
        doc.resolution_type = resolution_type
    if resolution_note:
        doc.resolution_note = resolution_note
    if status in ("Resolved", "Ignored"):
        doc.resolved_by = frappe.session.user
        doc.resolved_at = get_datetime()
    doc.save()
    return {"name": doc.name, "status": doc.status}


# --------------------------------------------------------------------------- #
# Doc-event / scheduler hooks — wired in M2 to the calculation engine
# --------------------------------------------------------------------------- #
def on_employee_checkin_create(doc, method: str | None = None) -> None:
    """``Employee Checkin`` after_insert → enqueue Work-Session recalculation.

    Resolves the VN Employee Shift Instance covering this check-in (by employee
    + planned window) and triggers ``utils.calc.persist_work_session`` on the
    short queue. Falls back to inline computation when enqueue is unavailable.
    """
    if not getattr(doc, "employee", None):
        return
    shift_instance = _resolve_shift_instance_for_checkin(doc)
    if not shift_instance:
        return
    try:
        frappe.enqueue(
            "gege_hr.gege_hr.utils.calc.persist_work_session",
            queue="short",
            timeout=60,
            shift_instance_name=shift_instance,
            calculate_mode="realtime",
        )
    except Exception:
        from gege_hr.gege_hr.utils import calc

        calc.persist_work_session(shift_instance, calculate_mode="realtime")


def _resolve_shift_instance_for_checkin(checkin_doc) -> str | None:
    """Find the VN Employee Shift Instance whose planned window covers the check-in."""
    check_dt = getattr(checkin_doc, "time", None)
    if check_dt is None:
        return None
    if hasattr(check_dt, "astimezone"):
        check_portal = tz_utils.to_portal(check_dt)
    else:
        check_portal = tz_utils.to_portal(get_datetime(check_dt))

    # Prefer a Shift Instance linked to this shift_type on the work_date.
    candidates = frappe.db.get_all(
        "VN Employee Shift Instance",
        filters={
            "employee": checkin_doc.employee,
            "docstatus": 1,
            "work_date": check_portal.date(),
        },
        fields=["name", "planned_start", "planned_end"],
    )
    # Overnight shifts: the check-in may land on work_date+1, so widen search.
    if not candidates:
        prev = check_portal.date() - timedelta(days=1)
        candidates = frappe.db.get_all(
            "VN Employee Shift Instance",
            filters={
                "employee": checkin_doc.employee,
                "docstatus": 1,
                "work_date": ["in", [check_portal.date(), prev]],
            },
            fields=["name", "planned_start", "planned_end"],
        )

    for c in candidates:
        ps = tz_utils.to_portal(get_datetime(c.planned_start))
        pe = tz_utils.to_portal(get_datetime(c.planned_end))
        if ps - timedelta(hours=24) <= check_portal <= pe + timedelta(hours=24):
            return c.name
    return candidates[0].name if candidates else None


@frappe.whitelist()
def recalculate_work_session(work_session: str | None = None, shift_instance: str | None = None) -> dict:
    """Plan §10.2 — recompute a Work Session on demand (HR/manager action).

    Pass either ``work_session`` or ``shift_instance``. Returns the recomputed
    Work Session name + key totals.
    """
    frappe.only_for(["HR Manager", "HR User", "System Manager"])
    if not shift_instance and work_session:
        shift_instance = frappe.db.get_value("VN Attendance Work Session", work_session, "shift_instance")
    if not shift_instance:
        frappe.throw(_("Cần cung cấp work_session hoặc shift_instance."), frappe.MandatoryError)

    from gege_hr.gege_hr.utils import calc

    # CAS guard (plan §19.3): refuse concurrent recalculation.
    ws_name = frappe.db.get_value("VN Attendance Work Session", {"shift_instance": shift_instance})
    if ws_name:
        status = frappe.db.get_value("VN Attendance Work Session", ws_name, "calculation_status")
        if status == "Recalculating":
            frappe.throw(_("Work Session đang được tính lại, vui lòng đợi."), frappe.ValidationError)
        frappe.db.set_value("VN Attendance Work Session", ws_name, "calculation_status", "Recalculating")

    name = calc.persist_work_session(shift_instance, calculate_mode="recalculate")
    if name:
        ws_doc = frappe.db.get_value(
            "VN Attendance Work Session",
            name,
            ["company", "employee", "shift_instance"],
            as_dict=True,
        )
        audit_api.log(
            "Work Session Recalculate",
            doc=ws_doc,
            company=ws_doc.company if ws_doc else None,
            employee=ws_doc.employee if ws_doc else None,
            reference_doctype="VN Attendance Work Session",
            reference_name=name,
            description=f"Tính lại Work Session {name}",
        )
    return {
        "ok": True,
        "work_session": name,
        "totals": _work_session_totals(name) if name else None,
    }


def _work_session_totals(ws_name: str) -> dict:
    ws = frappe.db.get_value(
        "VN Attendance Work Session",
        ws_name,
        [
            "regular_hours",
            "regular_night_hours",
            "raw_overtime_hours",
            "overtime_night_hours",
            "late_minutes",
            "early_leave_minutes",
            "payable_day",
            "need_review",
            "missing_checkin",
            "missing_checkout",
        ],
        as_dict=True,
    )
    return ws or {}


def auto_mark_absent_job() -> None:
    """Daily 02:00 portal-time cron → mark no-shows as Absent.

    For every submitted Shift Instance of yesterday (portal date) with no IN
    check-in, set its Work Session ``absent = 1`` and ``payable_day = 0``.
    Honours the policy's ``auto_mark_absent`` flag when set.
    """
    yesterday = tz_utils.now_in_portal().date() - timedelta(days=1)
    instances = frappe.db.get_all(
        "VN Employee Shift Instance",
        filters={"work_date": yesterday, "docstatus": 1, "status": ["!=", "Cancelled"]},
        fields=["name"],
    )
    for si in instances:
        ws_name = frappe.db.get_value("VN Attendance Work Session", {"shift_instance": si.name})
        if not ws_name:
            continue
        missing = frappe.db.get_value("VN Attendance Work Session", ws_name, "missing_checkin")
        if not missing:
            continue
        try:
            frappe.db.set_value("VN Attendance Work Session", ws_name, "absent", 1)
            frappe.db.set_value("VN Attendance Work Session", ws_name, "payable_day", 0)
        except Exception:
            continue


# --------------------------------------------------------------------------- #
# Correction Request (plan v5 §10.2 / doctype-design §12)
# --------------------------------------------------------------------------- #
CR_DOCTYPE = "VN Attendance Correction Request"

# Flat row shape returned to the SPA — mirrors the OT request contract so the
# list renders without a second lookup (matches hr-ui/src/api/index.js).
_CR_LIST_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "work_date",
    "shift_instance",
    "work_session",
    "company",
    "correction_type",
    "current_checkin_time",
    "current_checkout_time",
    "requested_checkin_time",
    "requested_checkout_time",
    "reason",
    "attachment",
    "workflow_state",
    "approver",
    "approved_at",
    "generated_checkin",
    "generated_attendance",
    "docstatus",
]


def _is_hr_manager() -> bool:
    roles = set(emp_utils.get_user_roles() or [])
    return bool(roles & emp_utils.HR_MANAGER_ROLES)


def _assert_own_correction(employee: str) -> None:
    """HR/Manager may read anyone; a plain Employee only their own row."""
    if _is_hr_manager():
        return
    own = emp_utils.get_employee_for_user()
    if own != emp_utils.emp_name(employee):
        frappe.throw(
            _("Bạn không có quyền truy cập dữ liệu của nhân viên khác."),
            frappe.PermissionError,
        )


@frappe.whitelist()
def my_correction_requests(
    employee: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    """Plan §10.2 — the caller's correction requests, narrowed by work_date.

    Managers (HR Manager/System Manager) may pass any ``employee``; a plain
    Employee is scoped to their own record.
    """
    emp = _resolve_employee(employee)
    _assert_own_correction(emp)

    filters = {"employee": emp}
    if from_date or to_date:
        filters["work_date"] = ["between", [from_date or to_date, to_date or from_date]]

    return frappe.db.get_all(
        CR_DOCTYPE,
        filters=filters,
        fields=_CR_LIST_FIELDS,
        order_by="work_date desc, creation desc",
    )


@frappe.whitelist()
def submit_correction_request(**kwargs) -> dict:
    """Plan §10.2 — create a Draft correction request.

    Accepts the flat payload the SPA sends: ``employee``, ``work_date``,
    ``correction_type``, ``reason`` (optional ``shift_instance``/
    ``work_session``/``current_*``/``requested_*``/``attachment``). Validation
    (lock period, requested change present, reason length, no duplicate) runs in
    ``VNAttendanceCorrectionRequest.validate``; naming in ``before_insert``
    (``CR-YYMMDD-XXXXXX``).

    Returns ``{ name, status, message }``.
    """
    if not kwargs:
        frappe.throw(_("Thiếu dữ liệu yêu cầu điều chỉnh."))

    employee = (kwargs.get("employee") or "").strip()
    if employee:
        _assert_own_correction(employee)
    emp = _resolve_employee(employee)

    doc = frappe.new_doc(CR_DOCTYPE)
    doc.update(
        {
            "employee": emp,
            "work_date": getdate(kwargs.get("work_date")) if kwargs.get("work_date") else None,
            "correction_type": kwargs.get("correction_type"),
            "reason": kwargs.get("reason") or "",
            "workflow_state": "Draft",
            "docstatus": 0,
        }
    )
    # Optional context / detail fields.
    for opt in (
        "shift_instance",
        "work_session",
        "current_checkin_time",
        "current_checkout_time",
        "requested_checkin_time",
        "requested_checkout_time",
        "attachment",
    ):
        if kwargs.get(opt):
            doc.set(opt, kwargs.get(opt))

    doc.insert()
    # Move into the approval pipeline (Draft → Pending Manager). Best-effort:
    # stays Draft if the workflow isn't seeded yet.
    send_for_approval(doc)
    return {
        "name": doc.name,
        "status": doc.workflow_state,
        "message": _("Đã tạo yêu cầu điều chỉnh {0}.").format(doc.name),
    }


@frappe.whitelist()
def cancel_correction_request(name: str | None = None) -> dict:
    """Cancel a Draft/Pending correction request (set Rejected, keep the audit
    trail). A dedicated endpoint keeps the transition consistent and
    permission-checked server-side (the SPA previously used a ``setValue``
    fallback)."""
    if not name:
        frappe.throw(_("Thiếu mã yêu cầu điều chỉnh."))
    doc = frappe.get_doc(CR_DOCTYPE, name)
    _assert_own_correction(doc.employee)
    if doc.docstatus >= 2:
        frappe.throw(_("Yêu cầu này đã bị hủy."))
    if doc.workflow_state == "Approved":
        frappe.throw(
            _("Yêu cầu đã duyệt không thể hủy từ phía nhân viên."),
            frappe.PermissionError,
        )
    doc.workflow_state = "Rejected"
    if doc.docstatus == 1:
        doc.cancel()
    else:
        doc.save()
    return {
        "name": name,
        "status": doc.workflow_state,
        "message": _("Đã hủy yêu cầu điều chỉnh {0}.").format(name),
    }


# --------------------------------------------------------------------------- #
# Monthly Attendance Period — thin delegating wrappers (FE contract:
# hr-ui/src/api/index.js calls these as `attendance.<fn>`). The real logic
# lives in gege_hr.gege_hr.api.attendance_period.
# --------------------------------------------------------------------------- #
from gege_hr.gege_hr.api import attendance_period as _ap  # noqa: E402


@frappe.whitelist()
def get_monthly_period_list(company=None, year=None, status=None):
    """FE contract → :func:`attendance_period.periods`."""
    return _ap.periods(company=company or None, status=status or None, year=year or None)


@frappe.whitelist()
def get_monthly_period_detail(name):
    """FE contract → :func:`attendance_period.period_detail`."""
    return _ap.period_detail(name)


@frappe.whitelist()
def generate_monthly_period(**kwargs):
    """FE contract → :func:`attendance_period.generate_monthly_period`."""
    return _ap.generate_monthly_period(**kwargs)


@frappe.whitelist()
def lock_monthly_period(name, reason=None):
    """FE contract → :func:`attendance_period.lock_period`."""
    return _ap.lock_period(name, reason=reason)


@frappe.whitelist()
def unlock_monthly_period(name, reason=None):
    """FE contract → :func:`attendance_period.unlock_period`."""
    return _ap.unlock_period(name, reason=reason)
