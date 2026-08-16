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
from gege_hr.gege_hr.utils import _db as _db_mod
from gege_hr.gege_hr.utils import employee as emp_utils
from gege_hr.gege_hr.utils import gamification as game
from gege_hr.gege_hr.utils import pagination
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
    """Resolve the employee for the caller → its *name*.

    Ownership-safe (IDOR fix): a plain Employee is ALWAYS pinned to the
    session user's own employee — a client-supplied ``employee`` param is only
    honoured for HR Manager / System Manager callers (who legitimately act on
    behalf of others, e.g. admin custom check-ins). ``employee`` may arrive as
    a full object from the SPA; it is coerced to the name string so it is safe
    to use as a ``filters`` value.
    """
    own = emp_utils.get_employee_for_user()
    if not own:
        frappe.throw(_("Tài khoản này chưa được liên kết với nhân viên."), frappe.PermissionError)
    if employee:
        requested = emp_utils.emp_name(employee)
        if requested == own or _is_hr_manager():
            return requested
        frappe.throw(
            _("Bạn không có quyền truy cập dữ liệu của nhân viên khác."),
            frappe.PermissionError,
        )
    return own


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


def _first_in_last_out(checkins: list[dict]) -> tuple[object, object]:
    """Return (first IN, last OUT) raw ``time`` values from checkin rows.

    Frappe ``Employee Checkin.log_type`` uses "IN"/"OUT" (or the localized
    "Clock In"/"Clock Out"). The raw value is kept (datetime or string) so the
    caller can normalise it to the portal timezone.
    """
    in_times, out_times = [], []
    for c in checkins:
        lt = (c.log_type or "").upper()
        if lt in ("IN", "CLOCK IN"):
            in_times.append(c.time)
        elif lt in ("OUT", "CLOCK OUT"):
            out_times.append(c.time)
    first_in = sorted(in_times)[0] if in_times else None
    last_out = sorted(out_times)[-1] if out_times else None
    return first_in, last_out


def _to_portal_dt(value):
    """Normalise a raw checkin ``time`` (datetime/ISO/SQL str) → portal-local dt.

    Frappe stores datetimes as UTC (naive); the helper treats them as UTC then
    converts to the portal timezone (plan v5 §2.7). Returns ``None`` for falsy.
    """
    if not value:
        return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if "T" in raw:
            iso = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
            try:
                value = datetime.fromisoformat(iso)
            except ValueError:
                return None
        else:
            try:
                value = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=tz_utils.ZoneInfo("UTC"))
    return tz_utils.to_portal(value)


def _status_key(
    actual_in,
    actual_out,
    deviation,
    now_local: datetime | None = None,
    planned_end: datetime | None = None,
) -> str:
    """Map (check-in done?, check-out done?, deviation) to an emotion status key.

    Keys mirror the frontend ``StatusEmotion`` table:
    ``early | on_time | late | very_late | not_started | completed |
    missing_checkout``.
    The check-in quality (early/on_time/late) is shown while the employee is
    still working; ``completed`` once they have checked out. ``missing_checkout``
    fires when the employee checked in but the shift end has passed with no OUT.
    """
    if actual_in and actual_out:
        return "completed"
    if actual_in is None:
        return "not_started"
    # Checked in but never checked out, and the planned shift end is in the past
    # → flag a forgotten checkout so the UI can prompt a correction request.
    if actual_out is None and now_local and planned_end and now_local > planned_end:
        return "missing_checkout"
    if deviation is None:
        return "not_started"
    if deviation <= -5:
        return "early"
    if deviation <= 5:
        return "on_time"
    if deviation <= 30:
        return "late"
    return "very_late"


def _session_context(shift: dict | None, checkins: list[dict], now_local: datetime) -> dict | None:
    """Build the rich ``session`` block (plan v5 §10.2 + gamification design).

    Computes actual check-in/out, early/late deviation (portal-local minutes),
    elapsed/remaining session minutes and the emotion ``status_key``. Returns
    ``None`` when there is no shift today.

    The block also exposes derived check-out semantics the UI uses for the
    "về sớm / đúng giờ / tăng ca" pill: ``checkout_status``, ``overtime_minutes``
    and ``early_exit_minutes``.
    """
    if not shift:
        return None

    first_in_raw, last_out_raw = _first_in_last_out(checkins)
    planned_start = tz_utils.to_portal(
        datetime.fromisoformat(shift["planned_start"].replace("Z", "+00:00"))
    )
    planned_end = tz_utils.to_portal(
        datetime.fromisoformat(shift["planned_end"].replace("Z", "+00:00"))
    )
    actual_in = _to_portal_dt(first_in_raw)
    actual_out = _to_portal_dt(last_out_raw)

    deviation = (
        int(round((actual_in - planned_start).total_seconds() / 60.0)) if actual_in else None
    )
    checkout_deviation = (
        int(round((actual_out - planned_end).total_seconds() / 60.0)) if actual_out else None
    )

    if actual_in and not actual_out:
        elapsed = int(max(0, (now_local - actual_in).total_seconds() / 60.0))
        remaining = int(max(0, (planned_end - now_local).total_seconds() / 60.0))
    elif actual_in and actual_out:
        elapsed = int(max(0, (actual_out - actual_in).total_seconds() / 60.0))
        remaining = 0
    else:
        elapsed = 0
        remaining = int(max(0, (planned_end - planned_start).total_seconds() / 60.0))

    status_key = _status_key(actual_in, actual_out, deviation, now_local, planned_end)

    # Derive checkout semantics (only meaningful once the employee checked out).
    overtime_minutes = 0
    early_exit_minutes = 0
    checkout_status = None
    if actual_out and checkout_deviation is not None:
        if checkout_deviation >= 5:
            checkout_status = "overtime"
            overtime_minutes = checkout_deviation
        elif checkout_deviation <= -5:
            checkout_status = "early"
            early_exit_minutes = -checkout_deviation
        else:
            checkout_status = "on_time"

    return {
        "status_key": status_key,
        "deviation_minutes": deviation,
        "checkout_deviation_minutes": checkout_deviation,
        "checkout_status": checkout_status,
        "overtime_minutes": overtime_minutes,
        "early_exit_minutes": early_exit_minutes,
        "actual_checkin": tz_utils.utc_iso(actual_in) if actual_in else None,
        "actual_checkout": tz_utils.utc_iso(actual_out) if actual_out else None,
        "elapsed_minutes": elapsed,
        "remaining_minutes": remaining,
        "planned_duration_minutes": int(max(0, (planned_end - planned_start).total_seconds() / 60.0)),
    }


def _is_date_locked(work_date: str) -> bool:
    """True when a VN Monthly Attendance Period covering ``work_date`` is Locked.

    Schema note: the period stores its window in ``from_date``/``to_date`` and
    its lock state in ``status == "Locked"`` — the historic filter on
    ``start_date``/``end_date``/``is_locked`` matched no column, so the query
    always failed (and the ``except`` silently returned False), leaving every
    lock guard in the portal permissive.
    """
    try:
        return bool(
            frappe.db.exists(
                "VN Monthly Attendance Period",
                {
                    "from_date": ["<=", work_date],
                    "to_date": [">=", work_date],
                    "status": "Locked",
                },
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
    work_location = _work_location_for(emp, day)
    session = _session_context(shift, checkins, now_local)

    return {
        "employee": emp,
        "work_date": day.isoformat(),
        "button_state": button_state,
        "shift": shift,
        "times": {
            "last_checkin": checkins[-1]["time"] if checkins else None,
            "checkin_count": len(checkins),
            # Expose the first-IN / last-OUT (UTC ISO) so the UI can show actual
            # times + worked duration. These match what TodayStatusCard expects.
            "actual_checkin": session["actual_checkin"] if session else None,
            "actual_checkout": session["actual_checkout"] if session else None,
        },
        # Rich session block (plan v5 §10.2 + attendance-gamification-design).
        "session": session,
        # Gamification snapshot (XP / streak / badges) — null if disabled.
        "gamification": game.get_snapshot(emp),
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
        client_request_id = f"srv-{tz_utils.utc_now().timestamp():.0f}"

    # Normalise the client timestamp BEFORE touching the DB. Browsers send
    # ``new Date().toISOString()`` ("2026-06-24T06:30:35.734Z") which MySQL
    # rejects with OperationalError(1292). Convert to an aware datetime that
    # Frappe serialises safely; a non-parseable value is logged + nulled out
    # rather than failing the whole check-in.
    client_ts = tz_utils.parse_client_timestamp(client_timestamp)
    if client_timestamp and client_ts is None:
        frappe.log_error(
            title="VN Mobile Checkin: unparseable client_timestamp",
            message=f"raw={client_timestamp!r} employee={emp}",
        )

    # Idempotency: a second tap with the same client_request_id is a no-op.
    existing = frappe.db.get_value("VN Mobile Checkin Attempt", {"client_request_id": client_request_id})
    if existing:
        return _checkin_result(emp, message="Yêu cầu đã được xử lý trước đó.")

    # Rate limit: max 1 request / 3s per employee (plan §Security).
    rate_limit(f"checkin:{emp}", max_requests=1, window_seconds=3)

    # Close any prior session the employee forgot to check out of (overnight or
    # day) BEFORE deciding this check-in's parity — so a forgotten checkout is
    # auto-closed at its planned_end instead of dropping the whole shift's hours
    # (Chính sách A). Safe + idempotent; no-op when disabled.
    try:
        from gege_hr.gege_hr.utils import checkout_miss

        checkout_miss.auto_close_missed_checkouts(emp)
    except Exception:
        frappe.log_error(title="checkout_miss.auto_close on checkin failed")

    day = tz_utils.now_in_portal().date()

    # Lock guard (defense-in-depth): a date inside a Locked monthly period is
    # closed for new punches — the FE already renders the LOCKED button state
    # via ``today_status``; enforce it server-side too.
    if _is_date_locked(day.isoformat()):
        frappe.throw(
            _("Hôm nay ({0}) thuộc kỳ công đã khoá — không thể chấm công. Liên hệ HR để mở khóa kỳ.")
            .format(day.isoformat())
        )

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
            "client_timestamp": client_ts,
            # Stamp the audit ``server_timestamp`` with TRUE UTC, not
            # ``frappe.utils.now()``. When ``System Settings.time_zone`` is
            # unset, Frappe falls back to Asia/Kolkata (+5:30) and every stamp
            # would land 5.5h in the future (the attendance "19:37" bug).
            "server_timestamp": tz_utils.utc_now_str(),
            "latitude": flt(latitude) if latitude is not None else None,
            "longitude": flt(longitude) if longitude is not None else None,
            "intended_log_type": log_type,
            "status": "Success",
        }
    )
    attempt.insert()

    # The actual Frappe HR check-in record (drives downstream recalculation).
    #
    # ``Employee Checkin.time`` MUST be true UTC (the storage convention
    # ``gege_hr`` relies on). We stamp it from ``tz_utils.utc_now_str()``
    # rather than ``server_now.astimezone(UTC)`` so the value is anchored to
    # the OS clock and is immune to a misconfigured Frappe system timezone.
    frappe.get_doc(
        {
            "doctype": "Employee Checkin",
            "employee": emp,
            "log_type": log_type,
            "time": tz_utils.utc_now_str(),
            "device_id": device_id or "gege_hr-mobile",
            "latitude": flt(latitude) if latitude is not None else None,
            "longitude": flt(longitude) if longitude is not None else None,
        }
    ).insert()

    frappe.db.commit()

    # Gamification: finalise XP/streak/badges once the session closes (check-out).
    game_result = None
    if log_type == "OUT":
        try:
            session_ctx = _session_context(shift, _checkins_for(emp, day), server_now)
            game_result = game.apply_session_xp(
                emp,
                status_key=session_ctx["status_key"] if session_ctx else "completed",
                work_date=day.isoformat(),
                checkout_deviation_minutes=(
                    session_ctx["checkout_deviation_minutes"] if session_ctx else None
                ),
                planned_duration_minutes=(
                    session_ctx["planned_duration_minutes"] if session_ctx else None
                ),
                elapsed_minutes=session_ctx["elapsed_minutes"] if session_ctx else None,
                is_overnight=bool(shift and shift.get("is_overnight")),
            )
        except Exception:
            # Gamification must never block a successful check-in.
            frappe.log_error(title="VN gamification: apply_session_xp failed", message=f"emp={emp}")

    refreshed = _checkin_result(emp, shift=shift)
    refreshed["log_type"] = log_type
    if game_result and not game_result.get("skipped"):
        refreshed["gamification"] = game_result

    # OT reminder: if checkout is after planned_end and no approved OT request
    # exists for today, surface a prompt so the employee submits one.
    if log_type == "OUT" and shift:
        try:
            pe = tz_utils.to_portal(
                datetime.fromisoformat(shift["planned_end"].replace("Z", "+00:00"))
            )
            now_p = tz_utils.to_portal(server_now)
            if now_p > pe:
                ot_hours = round((now_p - pe).total_seconds() / 3600.0, 1)
                if ot_hours > 0:
                    has_ot = frappe.db.exists(
                        "VN Overtime Request",
                        {"employee": emp, "docstatus": 1, "work_date": day},
                    )
                    if not has_ot:
                        refreshed["ot_pending_hours"] = ot_hours
                        refreshed["ot_message"] = (
                            f"Bạn có {ot_hours}h OT chưa duyệt. Hãy nộp "
                            "Overtime Request (kèm lý do) để HR duyệt và tính lương OT."
                        )
                        # Pre-fill data for the OT form (VN-local datetime-local).
                        refreshed["ot_from"] = pe.strftime("%Y-%m-%dT%H:%M")
                        refreshed["ot_to"] = now_p.strftime("%Y-%m-%dT%H:%M")
                        refreshed["ot_work_date"] = day.isoformat()
        except Exception:
            pass

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
    loc = _work_location_for(employee, tz_utils.now_in_portal().date())
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


def _work_location_for(employee: str, day: date | None = None) -> dict | None:
    """Resolve the geofence check-in location for an employee.

    Priority (highest wins):
      1. ``Shift Assignment.vn_work_location`` for an active assignment covering
         ``day`` (per-shift override — field staff / multi-site rosters).
      2. ``Employee.default_work_location`` (gege_hr custom field).
      3. ``VN HR Portal Setting.default_work_location`` (site-wide fallback).

    All lookups are guarded: ``vn_work_location`` is a Custom Field only present
    after migrate, and ``default_work_location`` likewise, so a fresh bench that
    hasn't run migrate yet never 500s.
    """
    # --- 1. Shift-assignment override for the day -----------------------------
    day = day or tz_utils.now_in_portal().date()
    loc_name = _shift_location_for_day(employee, day)

    # --- 2. Employee default --------------------------------------------------
    if not loc_name:
        try:
            loc_name = (
                frappe.db.get_value("Employee", employee, "default_work_location")
                if frappe.get_meta("Employee").has_field("default_work_location")
                else None
            )
        except Exception:
            loc_name = None

    # --- 3. Portal-setting fallback ------------------------------------------
    if not loc_name:
        loc_name = frappe.db.get_value(
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


def _shift_location_for_day(employee: str, day: date) -> str | None:
    """The ``vn_work_location`` of the employee's active Shift Assignment on ``day``.

    Returns ``None`` when no assignment covers the day, when the custom field is
    not installed yet, or when the assignment has no location pinned (so the
    caller falls back to the Employee default).
    """
    if not frappe.get_meta("Shift Assignment").has_field("vn_work_location"):
        return None
    sa = frappe.qb.DocType("Shift Assignment")
    row = (
        frappe.qb.from_(sa)
        .select(sa.vn_work_location)
        .where(sa.employee == employee)
        .where(sa.status == "Active")
        .where(sa.docstatus == 1)
        .where(sa.start_date <= day)
        .where((sa.end_date.isnull()) | (sa.end_date >= day))
        .where(sa.vn_work_location.notnull() & (sa.vn_work_location != ""))
        .limit(1)
        .run(as_dict=True)
    )
    if not row:
        return None
    return (row[0].get("vn_work_location") or "").strip() or None


@frappe.whitelist()
def my_logs(
    employee: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    """Plan §10.2 — per-day Work-Session rows for the employee's monthly view.

    The SPA ``MonthlyAttendanceView`` (status grid + daily log list) consumes
    Work-Session-shaped day rows carrying ``work_date``, ``shift_type``,
    ``planned_start/planned_end``, ``actual_checkin/actual_checkout``,
    ``late_minutes``, ``early_leave_minutes``, ``payable_day`` and the
    ``missing_checkin/has_leave/absent`` flags — none of which exist on the raw
    ``Employee Checkin`` record. Previously this returned the raw check-in rows
    (``{name, time, log_type, ...}``) so the grid could not place any cell
    (no ``work_date``) and every status resolved to ``rest`` → an empty grid.

    We now aggregate the submitted ``Attendance`` doctype into those day rows,
    reusing the same source/status logic as ``team_daily_status`` and
    ``my_monthly_summary`` so the grid, list and summary stay consistent.
    """
    emp = _resolve_employee(employee)
    today = tz_utils.now_in_portal().date()
    start = getdate(from_date) if from_date else add_days(today, -30)
    end = getdate(to_date) if to_date else today

    # ── SOURCE OF TRUTH: VN Attendance Work Session ────────────────────────
    # The Work Session is computed by the gege_hr engine (calc.py) from raw
    # Employee Checkins. It carries actual_checkin/checkout, late/early (with
    # grace + OT-compensation rules), raw_overtime_hours, approved_overtime_hours,
    # payable_day, absent/has_leave/need_review/missing_* flags — ALL the fields
    # the SPA needs. Reading it directly (instead of the core Attendance doctype)
    # guarantees the portal shows the SAME values the engine computed, including
    # OT (which Attendance does not carry).
    ws_rows = frappe.db.get_all(
        "VN Attendance Work Session",
        filters={
            "employee": emp,
            "work_date": ["between", [start, end]],
            "docstatus": ["!=", 2],
        },
        fields=[
            "name",
            "work_date",
            "shift_type",
            "planned_start",
            "planned_end",
            "actual_checkin",
            "actual_checkout",
            "late_minutes",
            "early_leave_minutes",
            "regular_hours",
            "total_actual_hours",
            "raw_overtime_hours",
            "approved_overtime_hours",
            "payable_day",
            "absent",
            "has_leave",
            "need_review",
            "missing_checkin",
            "missing_checkout",
        ],
        order_by="work_date desc",
        limit_page_length=500,
    )

    out: list[dict] = []
    for r in ws_rows:
        # Derive a Frappe-compatible status from the Work Session flags.
        if r.absent:
            status = "Absent"
        elif r.has_leave:
            status = "On Leave"
        elif flt(r.payable_day or 0) == 0.5:
            status = "Half Day"
        else:
            status = "Present"

        out.append(
            {
                "name": r.name,
                "work_date": str(r.work_date),
                "shift_type": r.shift_type or "",
                "status": status,
                "planned_start": str(r.planned_start) if r.planned_start else None,
                "planned_end": str(r.planned_end) if r.planned_end else None,
                "actual_checkin": str(r.actual_checkin) if r.actual_checkin else None,
                "actual_checkout": str(r.actual_checkout) if r.actual_checkout else None,
                "late_minutes": int(r.late_minutes or 0),
                "early_leave_minutes": int(r.early_leave_minutes or 0),
                "regular_hours": flt(r.regular_hours or 0, 2),
                "total_actual_hours": flt(r.total_actual_hours or 0, 2),
                "raw_overtime_hours": flt(r.raw_overtime_hours or 0, 4),
                "approved_overtime_hours": flt(r.approved_overtime_hours or 0, 4),
                "payable_day": flt(r.payable_day or 0, 2),
                "has_leave": bool(r.has_leave),
                "absent": bool(r.absent),
                "missing_checkin": bool(r.missing_checkin),
                "missing_checkout": bool(r.missing_checkout),
                "need_review": bool(r.need_review),
            }
        )
    return out


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


def _monthly_overtime_hours(rows: list[dict]) -> float:
    """Estimate total overtime (hours) from monthly Attendance rows.

    For each row with both ``in_time`` and ``out_time`` plus a ``shift``, the
    planned end is reconstructed from the Shift Type end time on that day; OT is
    the positive excess of ``out_time`` over that planned end. Rows lacking a
    shift or out_time contribute nothing. This is a pragmatic estimate until the
    M2 Work-Session engine ships precise figures.
    """
    if not rows:
        return 0.0

    # Cache Shift Type → (start_time, end_time) so the overnight-aware planned
    # window can be rebuilt per row with a single query regardless of month size.
    shift_names = {r.get("shift") for r in rows if r.get("shift")}
    shift_windows: dict[str, tuple] = {}
    if shift_names:
        for st in frappe.db.get_all(
            "Shift Type", {"name": ["in", list(shift_names)]}, ["name", "start_time", "end_time"]
        ):
            shift_windows[st.name] = (st.start_time, st.end_time)

    total_minutes = 0.0
    for r in rows:
        out_time = r.get("out_time")
        shift = r.get("shift")
        d = r.get("attendance_date")
        if not out_time or not shift or not d:
            continue
        win = shift_windows.get(shift)
        if not win or not win[1]:
            continue
        try:
            if isinstance(d, str):
                d = getdate(d)
            # Portal-aware planned end (overnight handled by planned_window()).
            # as_time() inside planned_window() normalises both timedelta
            # (MariaDB TIME) and datetime.time (Frappe Time) inputs.
            _, pe_local = tz_utils.planned_window(d, win[0], win[1])
            # Compare in the SAME portal frame: out_time is naive UTC per Frappe.
            out_local = tz_utils.to_portal(get_datetime(out_time))
            delta = (out_local - pe_local).total_seconds()
            if delta > 0:
                total_minutes += delta / 60.0
        except Exception:
            continue
    return flt(total_minutes / 60.0, 2)


@frappe.whitelist()
def my_monthly_summary(
    employee: str | None = None, year: int | None = None, month: int | None = None
) -> dict:
    """Plan §10.2 — monthly worked/payable/late/absent/leave/OT totals.

    Milestone-1 uses Attendance (Frappe HR) counts; the Work-Session engine
    (M2) will replace these with precise payable/OT figures. Until then OT is
    estimated per-day from out_time vs the shift planned end (see
    ``_monthly_overtime_hours``).
    """
    emp = _resolve_employee(employee)
    now = tz_utils.now_in_portal()
    y, m = _parse_year_month(year, month, now)
    start = date(y, m, 1)
    # Inclusive end = LAST day of the month. Frappe's ``between`` is inclusive on
    # both bounds, so using the first day of the next month pulls in one extra
    # day (e.g. 2026-08-01) and inflates the counts (32 "worked days" for July,
    # which only has 31). Subtract one day to land on the actual month end.
    next_first = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    end = next_first - timedelta(days=1)

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
    early_exit_days = frappe.db.count(
        "Attendance",
        {"employee": emp, "early_exit": 1, "attendance_date": ["between", [start, end]]},
    )
    # Totals are exposed at the TOP LEVEL because the SPA's MonthlyAttendanceView
    # reads ``summary.worked_days`` / ``payable_days`` / ``late_count`` /
    # ``absent_count`` / ``leave_days`` / ``overtime_hours`` directly off the
    # response object. Previously they were nested under ``summary`` (and used
    # different keys: late_days/absent/leave) so every tile resolved to 0.
    worked_days = _count("Present") + 0.5 * _count("Half Day")
    late_count = frappe.db.count(
        "Attendance",
        {"employee": emp, "late_entry": 1, "attendance_date": ["between", [start, end]]},
    )
    absent_count = _count("Absent")
    leave_days = _count("On Leave")
    overtime_hours = _monthly_overtime_hours(rows)
    return {
        "employee": emp,
        "year": y,
        "month": m,
        # Top-level totals — the SPA contract (MonthlyAttendanceView tiles).
        "worked_days": worked_days,
        "payable_days": worked_days,
        "late_count": late_count,
        "absent_count": absent_count,
        "leave_days": leave_days,
        "overtime_hours": overtime_hours,
        # Nested view kept for other consumers / future use (key names unchanged).
        "summary": {
            "present": _count("Present"),
            "absent": absent_count,
            "leave": leave_days,
            "half_day": _count("Half Day"),
            "worked_days": worked_days,
            "payable_days": worked_days,
            "late_days": late_count,
            "early_exit_days": early_exit_days,
            "overtime_hours": overtime_hours,
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
    # HR Manager / System Manager oversee the whole company, so they see every
    # active employee — independent of the (often empty) ``reports_to`` link.
    # Line Manager / HR User still only see their direct reports. This keeps the
    # team grid consistent with the per-employee monthly view, which never
    # depends on ``reports_to``.
    caller_roles = set(frappe.get_roles())
    is_company_wide = bool(caller_roles & {"HR Manager", "System Manager"})

    # IDOR fix: a company-wide caller may inspect any manager's team; everyone
    # else (Line Manager / HR User) is pinned to their OWN reports — a forged
    # ``manager`` param must not expose another team's attendance.
    if manager and is_company_wide:
        manager_emp = emp_utils.emp_name(manager)
    else:
        manager_emp = emp_utils.get_employee_for_user()
    # The SPA forwards the logged-in user's email/username as ``manager``; that
    # is not an Employee *name*, so resolve it (by user_id) before filtering on
    # ``reports_to``. Falls back to the session's own Employee when unresolved.
    if manager_emp and not frappe.db.exists("Employee", manager_emp):
        resolved = emp_utils.get_employee_for_user(manager_emp)
        manager_emp = resolved or emp_utils.get_employee_for_user() or manager_emp
    if from_date and to_date:
        start = getdate(from_date)
        end = getdate(to_date)
    else:
        today = tz_utils.now_in_portal().date()
        start = today.replace(day=1)
        end = today

    # Scope the roster to the manager's company when it can be resolved, so the
    # company-wide view does not leak cross-company employees in multi-company
    # setups. The manager's own Employee row is excluded from the roster.
    company = (
        frappe.db.get_value("Employee", manager_emp, "company")
        if manager_emp and frappe.db.exists("Employee", manager_emp)
        else None
    )

    if is_company_wide:
        member_filters = {"status": "Active"}
        if company:
            member_filters["company"] = company
        members = frappe.db.get_all(
            "Employee",
            filters=member_filters,
            fields=["name", "employee_name", "designation"],
        )
        if manager_emp:
            members = [m for m in members if m.name != manager_emp]
    else:
        members = frappe.db.get_all(
            "Employee",
            filters={"status": "Active", "reports_to": manager_emp},
            fields=["name", "employee_name", "designation"],
        )

    # Roster membership for [start, end]:
    #   (a) employees with an ACTIVE, effective Shift Assignment covering the
    #       period — the current team; OR
    #   (b) employees who have an Attendance row in the period — so historical
    #       months still show people who worked then even if their assignment has
    #       since been set Inactive.
    # An Inactive assignment ALONE (no attendance this month, not Active) does
    # NOT show: ending a shift hides the employee going forward but never
    # retroactively removes their past attendance from the grid. ``end_date`` may
    # be null (open-ended assignment).
    primary_shift: dict[str, str] = {}
    if members:
        assignments = frappe.db.get_all(
            "Shift Assignment",
            filters={
                "employee": ["in", [m["name"] for m in members]],
                "status": "Active",
                "start_date": ["<=", end],
                "docstatus": 1,
            },
            fields=["employee", "shift_type", "start_date", "end_date"],
            order_by="start_date asc",
        )
        for a in assignments:
            a_end = getdate(a.end_date) if a.end_date else None
            if a_end and a_end < start:
                continue
            if a.employee not in primary_shift:
                primary_shift[a.employee] = a.shift_type

    # Employees with real check-in history in this period (membership case b).
    # Only "Present" days count as history — late days are stored as
    # status="Present" (with late_entry=1) so they are included, but pure
    # Absent / On-Leave markers do NOT force an Inactive employee to appear.
    # Keep each one's attendance shifts to label the row when there is no Active
    # assignment (most frequent shift wins).
    att_shifts: dict[str, list] = {}
    if members:
        for r in frappe.db.get_all(
            "Attendance",
            filters={
                "employee": ["in", [m["name"] for m in members]],
                "attendance_date": ["between", [start, end]],
                "docstatus": 1,
                "status": "Present",
            },
            fields=["employee", "shift"],
        ):
            att_shifts.setdefault(r.employee, []).append(r.shift or "")

    def _shift_for_member(m):
        if m["name"] in primary_shift:
            return primary_shift[m["name"]]
        shifts = [s for s in att_shifts.get(m["name"], []) if s]
        if shifts:
            return max(set(shifts), key=shifts.count)
        return m.get("default_shift") or "Khác"

    member_shift = {m["name"]: _shift_for_member(m) for m in members}
    roster = set(primary_shift.keys()) | set(att_shifts.keys())
    members = [m for m in members if m["name"] in roster]

    # Approved Leave Applications per member/day — so leave days show correctly
    # even when the Work Session's has_leave flag isn't set by the engine.
    leave_by_emp_date: dict[str, dict[str, str]] = {}
    if members:
        for la in frappe.db.get_all(
            "Leave Application",
            filters={
                "employee": ["in", [m["name"] for m in members]],
                "from_date": ["<=", end],
                "to_date": [">=", start],
                "docstatus": 1,
                "status": "Approved",
            },
            fields=["employee", "from_date", "to_date", "leave_type"],
        ):
            d = getdate(la.from_date)
            while d <= getdate(la.to_date):
                leave_by_emp_date.setdefault(la.employee, {})[str(d)] = la.leave_type or ""
                d += timedelta(days=1)

    # Shift Type start/end windows — used both for the per-group header and to
    # translate the raw Attendance row (status="Present" even when late) into a
    # UI-friendly per-day status + real late/early minutes.
    shift_names = sorted(set(member_shift.values()))
    shift_meta: dict[str, dict] = {}
    real_shift_names = [s for s in shift_names if s and s != "Khác"]
    if real_shift_names:
        for st in frappe.db.get_all(
            "Shift Type",
            {"name": ["in", real_shift_names]},
            ["name", "start_time", "end_time"],
        ):
            shift_meta[st.name] = {"start_time": st.start_time, "end_time": st.end_time}

    shift_window_cache: dict[str, tuple] = {n: (m.get("start_time"), m.get("end_time")) for n, m in shift_meta.items()}

    def _shift_window(shift_name: str | None):
        if not shift_name:
            return None, None
        if shift_name not in shift_window_cache:
            st = frappe.db.get_value("Shift Type", shift_name, ["start_time", "end_time"], as_dict=True)
            shift_window_cache[shift_name] = (
                getattr(st, "start_time", None) if st else None,
                getattr(st, "end_time", None) if st else None,
            )
        return shift_window_cache[shift_name]

    summary = {"present": 0, "late": 0, "early": 0, "overtime": 0, "absent": 0, "on_leave": 0}
    grouped: dict[str, list] = {sn: [] for sn in shift_names}
    out_members = []
    for m in members:
        rows = frappe.db.get_all(
            "Attendance",
            filters={
                "employee": m.name,
                "attendance_date": ["between", [start, end]],
                "docstatus": 1,
            },
            fields=["attendance_date", "status", "shift", "in_time", "out_time", "late_entry", "early_exit"],
            order_by="attendance_date asc",
        )
        by_date = {str(r.attendance_date): r for r in rows}
        # Work Session is the portal's source of truth for the PAIRED IN/OUT:
        # calc.py matches punches to the shift's planned window, so overnight
        # checkouts land on the correct (start-day) row. The core Attendance
        # in_time/out_time is frequently scrambled for overnight shifts (previous
        # night's OUT, or a 1-day offset), so prefer the Work Session for the
        # displayed times + late/early and only fall back to Attendance below.
        ws_rows = frappe.db.get_all(
            "VN Attendance Work Session",
            filters={"employee": m.name, "work_date": ["between", [start, end]]},
            fields=[
                "work_date",
                "actual_checkin",
                "actual_checkout",
                "late_minutes",
                "early_leave_minutes",
                "approved_overtime_hours",
                "raw_overtime_hours",
            ],
        )
        ws_map = {str(r.work_date): r for r in ws_rows}
        days = []
        cur = start
        while cur <= end:
            att = by_date.get(str(cur))
            ws = ws_map.get(str(cur))
            if not att and not ws:
                days.append(
                    {
                        "work_date": str(cur),
                        "status": "Not marked",
                        "checkin_time": None,
                        "checkout_time": None,
                        "late_minutes": 0,
                        "early_leave_minutes": 0,
                    }
                )
                cur += timedelta(days=1)
                continue

            # Approved Leave Application for this day → "On Leave" (sync with
            # /hr/schedule which reads Leave Application directly).
            la_type = leave_by_emp_date.get(m["name"], {}).get(str(cur))
            if la_type is not None:
                days.append(
                    {
                        "work_date": str(cur),
                        "status": "On Leave",
                        "checkin_time": None,
                        "checkout_time": None,
                        "late_minutes": 0,
                        "early_leave_minutes": 0,
                        "raw_overtime_hours": 0.0,
                    }
                )
                summary["on_leave"] += 1
                cur += timedelta(days=1)
                continue

            # Work Session is the source of truth; Attendance is fallback for
            # days where the engine hasn't run yet. Handle att=None gracefully.
            disp_status = (att.status if att else "Present")
            late_min = 0
            early_min = 0
            ot_hours = 0.0
            raw_ot = 0.0
            checkin_time = (att.in_time if att else None)
            checkout_time = (att.out_time if att else None)
            shift_start, shift_end = _shift_window(getattr(att, "shift", None))
            if ws and (ws.actual_checkin or ws.actual_checkout):
                # Authoritative Work Session: correct overnight pairing, and
                # late/early already computed by calc.py (with grace/OT rules).
                checkin_time = ws.actual_checkin
                checkout_time = ws.actual_checkout
                late_min = int(ws.late_minutes or 0)
                early_min = int(ws.early_leave_minutes or 0)
                # Work Session is the portal's source of truth for approved OT
                # (calc.py applies the policy/approval rules); carry it through
                # so the team grid + summary chips can show "Tăng ca".
                # Only APPROVED OT shows in the team view (correct for payroll).
                # Raw OT (unapproved) is visible to the employee on /hr/schedule.
                ot_hours = flt(ws.approved_overtime_hours or 0, 2)
                raw_ot = flt(getattr(ws, "raw_overtime_hours", 0) or 0, 4)
                if late_min > 0:
                    disp_status = "Late"
            elif shift_start is not None and shift_end is not None:
                # No Work Session — fall back to Attendance times, compared in the
                # portal frame (overnight-aware). A punch outside this shift's
                # window is a stray/wrong-day one (common with overnight auto-
                # attendance) → hide it so we never show the previous night's
                # checkout nor a false "về sớm".
                ps_local, pe_local = tz_utils.planned_window(cur, shift_start, shift_end)
                if att.in_time:
                    in_local = tz_utils.to_portal(get_datetime(att.in_time))
                    if ps_local <= in_local <= pe_local:
                        if att.late_entry:
                            disp_status = "Late"
                        late_min = max(0, int((in_local - ps_local).total_seconds() // 60))
                    else:
                        checkin_time = None
                if att.out_time:
                    out_local = tz_utils.to_portal(get_datetime(att.out_time))
                    if out_local >= ps_local:
                        early_min = max(0, int((pe_local - out_local).total_seconds() // 60))
                    else:
                        checkout_time = None

            days.append(
                {
                    "work_date": str(cur),
                    "status": disp_status,
                    "checkin_time": checkin_time,
                    "checkout_time": checkout_time,
                    "late_minutes": late_min,
                    "early_leave_minutes": early_min,
                    "approved_overtime_hours": ot_hours,
                    "raw_overtime_hours": raw_ot,
                }
            )
            # Summary chips: count by the DERIVED late/early/OT values (not just
            # the raw Attendance flags) so the totals match what the cells show.
            # Work-Session-derived late/early are not reflected in att.late_entry,
            # so the minute thresholds are authoritative here.
            att_status = (att.status if att else disp_status)
            if att_status == "Present":
                summary["present"] += 1
                if (getattr(att, "late_entry", 0) if att else 0) or late_min > 0:
                    summary["late"] += 1
                if early_min > 0:
                    summary["early"] += 1
            elif att_status == "Absent":
                summary["absent"] += 1
            elif att_status == "On Leave":
                summary["on_leave"] += 1
            if ot_hours > 0:
                summary["overtime"] += 1
            cur += timedelta(days=1)
        out = {**m, "days": days, "shift_type": member_shift[m["name"]]}
        out_members.append(out)
        grouped.setdefault(out["shift_type"], []).append(out)

    # Build one group per shift, preserving the sorted shift order so the UI is
    # deterministic. Empty shifts (no member after Attendance filtering) are
    # still listed so the manager sees the shift exists.
    groups = []
    for sn in shift_names:
        st = shift_meta.get(sn, {})
        groups.append(
            {
                "shift_type": sn,
                "shift_start": st.get("start_time"),
                "shift_end": st.get("end_time"),
                "members": grouped.get(sn, []),
            }
        )

    return {
        "from_date": str(start),
        "to_date": str(end),
        "groups": groups,
        "members": out_members,
        "summary": summary,
    }


_EXCEPTION_FIELDS = [
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
]
# Lightweight fields for the server-side summary aggregate (DNA §6.6 A).
_EXCEPTION_SUMMARY_FIELDS = ["name", "status"]


def _exception_summary(light_rows) -> dict:
    """Per-status bucket counts over the full filtered set (SPA summary tiles)."""
    buckets = pagination.bucket_counts(light_rows, "status")
    return {
        "total": len(light_rows or []),
        "open": buckets.get("Open", 0),
        "in_progress": buckets.get("In Progress", 0),
        "escalated": buckets.get("Escalated", 0),
        "resolved": buckets.get("Resolved", 0),
        "ignored": buckets.get("Ignored", 0),
    }


# --------------------------------------------------------------------------- #
# HR admin "Chấm công" — VN Attendance Work Session directory (DNA §6.6 A)
# --------------------------------------------------------------------------- #
_WORK_SESSION_DOCTYPE = "VN Attendance Work Session"
# Fields mirror the SPA projection (useAdmin SESSION_FIELDS) — the corrected,
# real column names that the legacy getList read already used successfully.
_WORK_SESSION_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "work_date",
    "shift_type",
    "shift_instance",
    "calculation_status",
    "actual_checkin",
    "actual_checkout",
    "planned_start",
    "planned_end",
    "actual_within_shift_hours",
    "late_minutes",
    "early_leave_minutes",
    "approved_overtime_hours",
    "payable_day",
]
_WORK_SESSION_SUMMARY_FIELDS = [
    "name",
    "late_minutes",
    "early_leave_minutes",
    "approved_overtime_hours",
]


def _ws_float(value) -> float:
    """Best-effort numeric coerce for a threshold summary (never raises)."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _work_session_summary(light_rows) -> dict:
    """Threshold counts over the full filtered set (SPA summary tiles)."""
    rows = light_rows or []
    return {
        "total": len(rows),
        "late": sum(1 for r in rows if _ws_float(r.get("late_minutes")) > 0),
        "early": sum(1 for r in rows if _ws_float(r.get("early_leave_minutes")) > 0),
        "overtime": sum(1 for r in rows if _ws_float(r.get("approved_overtime_hours")) > 0),
    }


# Candidate broad-search columns (DNA §6.6 A). Text + numeric are LIKE-matched
# so typing a value/number still finds rows; datetime columns
# (actual_checkin/actual_checkout/planned_*) are deliberately excluded — Frappe
# casts the ``%q%`` literal to datetime → ``ParserError`` (DNA §6.6 A pitfall).
_WORK_SESSION_SEARCH_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "shift_type",
    "late_minutes",
    "early_leave_minutes",
    "approved_overtime_hours",
    "payable_day",
]


def _ws_search_or_filters(search: str | None) -> list | None:
    """Broad-search ``or_filters`` (list form) for the work-session directory.

    Columns are intersected with the DocType's real columns so an unmigrated
    bench never raises "column does not exist" (DNA §6.6 A — ``_safe_fields``
    philosophy). Falls back to the always-present text fields when the meta
    lookup is unavailable (e.g. doctype not yet shipped).
    """
    q = (search or "").strip()
    if not q:
        return None
    try:
        valid = set(frappe.meta.get_table_columns(_WORK_SESSION_DOCTYPE) or [])
    except Exception:
        valid = set()
    cols = [c for c in _WORK_SESSION_SEARCH_FIELDS if c in valid] if valid else [
        "name",
        "employee",
        "employee_name",
        "shift_type",
    ]
    _like = f"%{q}%"
    return [[c, "like", _like] for c in cols] or None


@frappe.whitelist()
def list_work_sessions(
    employee: str | None = None,
    search: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    status: str | None = None,
    shift_type: str | None = None,
    limit: int = 100,
    page: int = 1,
    page_size: int = 0,
    include_absent: int = 0,
) -> list[dict] | dict:
    """HR admin "Chấm công" list — ``VN Attendance Work Session`` rows.

    Filters by employee + ``work_date`` window + ``calculation_status`` +
    ``shift_type`` (DNA §6.2 — every content field has a popover filter, Law #2),
    or a free-text ``search`` (OR-matched across employee / employee_name /
    shift_type + numeric metrics — DNA §6.6 A, HR-BL-02) so the SPA broad-search
    box no longer filters an already-loaded list client-side.

    Pagination is **opt-in** (DNA §6.6 A): pass ``page`` + a positive
    ``page_size`` to receive ``{"data": [...], "total": int, "summary": {...}}``
    where ``total`` is counted via ``get_all().len`` (``db.count`` ignores
    ``or_filters``) and ``summary`` aggregates the *full* filtered set so the
    SPA summary tiles stay correct under pagination. Without ``page_size`` the
    legacy bare-list return is preserved.
    """
    frappe.only_for(["HR Manager", "HR User", "System Manager"])
    filters = []
    if employee:
        filters.append(["employee", "=", emp_utils.emp_name(employee)])
    if from_date:
        filters.append(["work_date", ">=", getdate(from_date)])
    if to_date:
        filters.append(["work_date", "<=", getdate(to_date)])
    if status and status.strip():
        filters.append(["calculation_status", "=", status.strip()])
    if shift_type and shift_type.strip():
        filters.append(["shift_type", "=", shift_type.strip()])
    # By default hide sessions an employee never clocked into (absent / off-day /
    # future-generated) — pass include_absent=1 to see them.
    if not int(include_absent or 0):
        filters.append(["actual_checkin", "is", "set"])
    or_filters = _ws_search_or_filters(search)
    limit = pagination.clamp_limit(limit, default=100)

    if page_size:
        summary = _work_session_summary(
            pagination.all_rows(
                _WORK_SESSION_DOCTYPE,
                fields=_WORK_SESSION_SUMMARY_FIELDS,
                filters=filters or None,
                or_filters=or_filters or None,
            )
        )
        page = max(1, pagination.as_int(page, 1))
        page_size = pagination.clamp_limit(page_size, default=20)
        start = (page - 1) * page_size
        try:
            rows = (
                frappe.get_all(
                    _WORK_SESSION_DOCTYPE,
                    fields=_WORK_SESSION_FIELDS,
                    filters=filters or None,
                    or_filters=or_filters or None,
                    order_by="work_date desc, name desc",
                    limit_start=start,
                    limit_page_length=page_size,
                )
                or []
            )
        except Exception:
            frappe.log_error(title="attendance.list_work_sessions failed")
            return {"data": [], "total": summary["total"], "summary": summary}
        return {"data": rows, "total": summary["total"], "summary": summary}

    return frappe.get_all(
        _WORK_SESSION_DOCTYPE,
        fields=_WORK_SESSION_FIELDS,
        filters=filters or None,
        or_filters=or_filters or None,
        order_by="work_date desc, name desc",
        limit_page_length=limit,
    )


@frappe.whitelist()
def get_work_session_filter_options() -> dict:
    """Distinct dropdown values for the gear popover (DNA §6.3 / §6.4 step 2).

    Returns ``calculation_status`` buckets + the ``Shift Type`` catalogue so the
    SPA ``SearchableSelect`` lists never render empty. Degrades to empty lists
    when the Work Session DocType / Shift Type table is not shipped yet.
    """
    frappe.only_for(["HR Manager", "HR User", "System Manager"])
    statuses: list[str] = []
    try:
        rows = frappe.get_all(
            _WORK_SESSION_DOCTYPE,
            fields=["calculation_status"],
            filters={"calculation_status": ["is", "set"]},
            group_by="calculation_status",
            order_by="calculation_status asc",
            limit_page_length=0,
        )
        statuses = [r.get("calculation_status") for r in rows if r.get("calculation_status")]
    except Exception:
        statuses = []
    shift_types: list[dict] = []
    try:
        for name in frappe.get_all("Shift Type", pluck="name", order_by="name asc") or []:
            shift_types.append({"label": name, "value": name})
    except Exception:
        shift_types = []
    return {"statuses": statuses, "shift_types": shift_types}


@frappe.whitelist()
def get_exceptions(
    status: str = "",
    employee: str = "",
    work_date: str = "",
    from_date: str = "",
    to_date: str = "",
    exception_type: str = "",
    severity: str = "",
    assigned_to: str = "",
    search: str = "",
    limit: int = 200,
    page: int = 1,
    page_size: int = 0,
) -> list[dict] | dict:
    """Plan §2.6.7 — list ``VN Attendance Exception`` rows for the HR screen.

    Honours Frappe role permissions (HR Manager / HR User have read via the
    doctype JSON). Gated by ``frappe.only_for`` (no permission bypass).

    Pagination is **opt-in** (DNA §6.6 A): pass ``page`` + a positive
    ``page_size`` to receive ``{"data": [...], "total": int, "summary": {...}}``
    where ``total`` is counted via ``get_all().len`` (``db.count`` ignores
    ``or_filters``) and ``summary`` aggregates the *full* filtered set so the
    SPA summary tiles stay correct under pagination. Without ``page_size`` the
    legacy bare-list return is preserved.
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
    # Popover filters (DNA §6.2 — gear covers every content field, server-side).
    if exception_type:
        filters.append(["exception_type", "=", exception_type])
    if severity:
        filters.append(["severity", "=", severity])
    if assigned_to:
        filters.append(["assigned_to", "=", assigned_to])
    # Broad search (DNA §6.6 D) — OR-match across the exception's text fields.
    or_filters = None
    _q = (search or "").strip()
    if _q:
        _like = f"%{_q}%"
        or_filters = [
            ["name", "like", _like],
            ["employee", "like", _like],
            ["employee_name", "like", _like],
            ["exception_type", "like", _like],
            ["description", "like", _like],
            ["assigned_to", "like", _like],
        ]
    limit = pagination.clamp_limit(limit, default=200)

    if page_size:
        summary = _exception_summary(
            pagination.all_rows(
                "VN Attendance Exception",
                fields=_EXCEPTION_SUMMARY_FIELDS,
                filters=filters or None,
                or_filters=or_filters or None,
            )
        )
        page = max(1, pagination.as_int(page, 1))
        page_size = max(1, pagination.as_int(page_size, 20))
        start = (page - 1) * page_size
        try:
            rows = (
                frappe.get_all(
                    "VN Attendance Exception",
                    fields=_EXCEPTION_FIELDS,
                    filters=filters or None,
                    or_filters=or_filters or None,
                    order_by="work_date desc, name desc",
                    limit_start=start,
                    limit_page_length=page_size,
                )
                or []
            )
        except Exception:
            frappe.log_error(title="attendance.get_exceptions failed")
            return {"data": [], "total": summary["total"], "summary": summary}
        return {"data": rows, "total": summary["total"], "summary": summary}

    return frappe.get_all(
        "VN Attendance Exception",
        fields=_EXCEPTION_FIELDS,
        filters=filters or None,
        or_filters=or_filters or None,
        order_by="work_date desc, name desc",
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

    When no matching Shift Instance exists (e.g. backfill not run yet for the
    check-in's day) we no longer drop the log silently — an "Unmatched Checkin"
    ``VN Attendance Exception`` is raised so HR can backfill + recalculate.
    """
    if not getattr(doc, "employee", None):
        return
    shift_instance = _resolve_shift_instance_for_checkin(doc)
    if not shift_instance:
        _raise_unmatched_checkin(doc)
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


def _raise_unmatched_checkin(checkin_doc) -> None:
    """Record an ``Unmatched Checkin`` exception when a punch has no Shift Instance.

    Idempotent per (employee, work_date) so a flurry of punches on an unmatched
    day only produces one exception row. Auto-resolves once a Shift Instance +
    Work Session exist (re-evaluated by the recalculation flow).
    """
    check_dt = getattr(checkin_doc, "time", None)
    if check_dt is None:
        return
    work_date = tz_utils.to_portal(get_datetime(check_dt)).date()
    employee = getattr(checkin_doc, "employee", None)
    if not employee:
        return
    # One Open exception per employee/day — skip if already present.
    if frappe.db.exists(
        "VN Attendance Exception",
        {
            "employee": employee,
            "work_date": work_date,
            "exception_type": "Unmatched Checkin",
            "status": ["in", ["Open", "In Progress", "Escalated"]],
        },
    ):
        return
    employee_name = frappe.db.get_value("Employee", employee, "employee_name") or employee
    try:
        frappe.get_doc(
            {
                "doctype": "VN Attendance Exception",
                "employee": employee,
                "employee_name": employee_name,
                "work_date": work_date,
                "exception_type": "Unmatched Checkin",
                "severity": "Medium",
                "description": (
                    f"Employee Checkin {getattr(checkin_doc, 'name', '')} tại "
                    f"{check_dt} không khớp Shift Instance nào (chưa sinh ca cho ngày này)."
                ),
                "status": "Open",
            }
        ).insert(ignore_permissions=True)
    except Exception:
        # Never let exception-logging break the check-in insert path.
        frappe.log_error(frappe.get_traceback(), "Unmatched Checkin exception log failed")


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

    # CAS guard (plan §19.3): refuse concurrent recalculation. Guarded UPDATE
    # (claim → check rows affected) instead of read-then-set, so two recalc
    # requests cannot both pass the status check and run in parallel.
    ws_name = frappe.db.get_value("VN Attendance Work Session", {"shift_instance": shift_instance})
    if ws_name:
        claimed = _db_mod.guarded_update(
            "UPDATE `tabVN Attendance Work Session`"
            " SET calculation_status = 'Recalculating'"
            " WHERE name = %(name)s AND calculation_status != 'Recalculating'",
            {"name": ws_name},
        )
        if not claimed:
            frappe.throw(_("Work Session đang được tính lại, vui lòng đợi."), frappe.ValidationError)

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


@frappe.whitelist()
def recalculate_period(
    from_date: str | None = None,
    to_date: str | None = None,
    employee: str | None = None,
    backfill: int = 1,
) -> dict:
    """Batch-recalculate Work Sessions for a date window (HR/manager action).

    Solves the root cause of the empty ``/hr/attendance`` page: existing
    Employee Checkins that never produced a Work Session because their Shift
    Instance was missing. Steps per call:

    1. Optionally backfill Shift Instances for the window
       (``shift.backfill_shift_instances``).
    2. Auto-resolve stale ``Unmatched Checkin`` exceptions now that instances
       exist.
    3. Recompute every Shift Instance in the window via
       ``calc.persist_work_session`` (skip Locked sessions).

    Returns counts: ``instances_created``, ``sessions_recalculated``,
    ``exceptions_resolved``. Safe to run repeatedly (idempotent).
    """
    frappe.only_for(["HR Manager", "HR User", "System Manager"])
    from gege_hr.gege_hr.api import shift as shift_api
    from gege_hr.gege_hr.utils import calc

    today = tz_utils.now_in_portal().date()
    start = getdate(from_date) if from_date else add_days(today, -7)
    end = getdate(to_date) if to_date else today
    if end < start:
        start, end = end, start

    instances_created = 0
    if backfill:
        instances_created = shift_api._materialise_shift_instances(
            from_date=start, to_date=end, employee=employee
        )

    si_filters = {"work_date": ["between", [start, end]], "docstatus": 1}
    if employee:
        si_filters["employee"] = employee
    shift_instances = frappe.db.get_all(
        "VN Employee Shift Instance", filters=si_filters, pluck="name"
    )

    sessions_recalculated = 0
    for si_name in shift_instances:
        # Skip Locked sessions (CAS guard in persist_work_session).
        status = frappe.db.get_value("VN Attendance Work Session", {"shift_instance": si_name}, "calculation_status")
        if status == "Locked":
            continue
        try:
            if calc.persist_work_session(si_name, calculate_mode="batch"):
                sessions_recalculated += 1
        except Exception:
            frappe.log_error(
                frappe.get_traceback(), f"Work Session recalc failed for {si_name}"
            )

    # Auto-resolve stale "Unmatched Checkin" exceptions now covered.
    exceptions_resolved = 0
    open_exc = frappe.db.get_all(
        "VN Attendance Exception",
        filters={
            "exception_type": "Unmatched Checkin",
            "status": ["in", ["Open", "In Progress", "Escalated"]],
            "work_date": ["between", [start, end]],
        },
        fields=["name", "employee", "work_date"],
    )
    for exc in open_exc:
        if employee and exc.get("employee") != employee:
            continue
        covered = frappe.db.exists(
            "VN Employee Shift Instance",
            {"employee": exc["employee"], "work_date": exc["work_date"], "docstatus": 1},
        )
        if covered:
            frappe.db.set_value(
                "VN Attendance Exception",
                exc["name"],
                {
                    "status": "Resolved",
                    "resolution_type": "Recalculate",
                    "resolution_note": "Đã sinh Shift Instance + tính lại Work Session.",
                    "resolved_by": frappe.session.user,
                    "resolved_at": get_datetime(),
                },
            )
            exceptions_resolved += 1

    audit_api.log(
        "Work Session Period Recalculate",
        company=None,
        employee=employee,
        reference_doctype="VN Attendance Work Session",
        reference_name=None,
        description=(
            f"Tính lại Work Session {start} → {end}"
            f" (employee={employee or 'all'}): "
            f"instances={instances_created}, sessions={sessions_recalculated}, "
            f"resolved={exceptions_resolved}"
        ),
    )
    return {
        "ok": True,
        "from_date": start.isoformat(),
        "to_date": end.isoformat(),
        "employee": employee,
        "instances_created": instances_created,
        "sessions_recalculated": sessions_recalculated,
        "exceptions_resolved": exceptions_resolved,
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
        missing, has_leave = frappe.db.get_value(
            "VN Attendance Work Session", ws_name, ["missing_checkin", "has_leave"]
        )
        if not missing:
            continue
        # An approved leave covering the day is a legitimate absence — marking
        # it absent zeroed the payable day and docked pay for employees who
        # were on approved leave.
        if has_leave:
            continue
        try:
            frappe.db.set_value(
                "VN Attendance Work Session", ws_name, {"absent": 1, "payable_day": 0}
            )
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


def _filter_correction_rows(rows: list[dict], search: str | None, fields: tuple[str, ...]) -> list[dict]:
    """Server-side free-text filter across the given row fields (DNA §6.6 D).

    Applied after the rows are fetched so it never risks an ``or_filters``
    "column does not exist" error on the correction list.
    """
    q = (search or "").strip().lower()
    if not q:
        return rows
    return [r for r in rows if any(q in str(r.get(k) or "").lower() for k in fields)]


@frappe.whitelist()
def my_correction_requests(
    employee: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 0,
) -> list[dict] | dict:
    """Plan §10.2 — the caller's correction requests, narrowed by work_date.

    ``search`` OR-matches a free-text query across the row's text fields
    (name / correction_type / reason / work_date / employee / employee_name),
    applied server-side (DNA §6.6 D, HR-BL-08). Managers (HR Manager/System
    Manager) may pass any ``employee``; a plain Employee is scoped to own.

    Pagination is **opt-in** (DNA §6.6 A): pass ``page`` + a positive
    ``page_size`` to receive ``{"data": [...], "total": int, "summary": None}``
    (post-query filter → ``total`` is the filtered list length); without
    ``page_size`` the legacy bare-list return is preserved.
    """
    emp = _resolve_employee(employee)
    _assert_own_correction(emp)

    filters = {"employee": emp}
    if from_date or to_date:
        filters["work_date"] = ["between", [from_date or to_date, to_date or from_date]]

    rows = frappe.db.get_all(
        CR_DOCTYPE,
        filters=filters,
        fields=_CR_LIST_FIELDS,
        order_by="work_date desc, creation desc",
        limit_page_length=pagination.MAX_PAGE_SIZE * 10,  # newest-first bound
    )
    filtered = _filter_correction_rows(
        rows,
        search,
        ("name", "correction_type", "reason", "work_date", "employee", "employee_name"),
    )
    return pagination.paginate_filtered(filtered, page=page, page_size=page_size)


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
    """FE contract → :func:`attendance_period.generate_monthly_period`.

    The RPC payload includes Frappe-internal keys (``cmd``…) that the real
    function's strict signature rejects — forward only the parameters it
    actually accepts.
    """
    import inspect

    accepted = set(inspect.signature(_ap.generate_monthly_period).parameters)
    return _ap.generate_monthly_period(**{k: v for k, v in kwargs.items() if k in accepted})


@frappe.whitelist()
def lock_monthly_period(name, reason=None):
    """FE contract → :func:`attendance_period.lock_period`."""
    return _ap.lock_period(name, reason=reason)


@frappe.whitelist()
def unlock_monthly_period(name, reason=None):
    """FE contract → :func:`attendance_period.unlock_period`."""
    return _ap.unlock_period(name, reason=reason)
