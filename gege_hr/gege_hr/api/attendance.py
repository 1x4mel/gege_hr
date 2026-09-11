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
from gege_hr.gege_hr.utils import (
    _db as _db_mod,
    employee as emp_utils,
    gamification as game,
    notify as notify_util,
    pagination,
    tz as tz_utils,
)
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

    PHASE-1 FRAME: ``Employee Checkin.time`` is stored as naive PORTAL WALL
    (plans/tz-frame-unification-plan.md §0 evidence). Query bounds are the
    wall-clock day window — NO UTC conversion (the old UTC bounds shifted the
    window by the portal offset and absorbed neighbouring days' logs).
    """
    employee = emp_utils.emp_name(employee)
    start = datetime.combine(day, datetime.min.time())
    end = datetime.combine(day + timedelta(days=1), datetime.min.time())
    return (
        frappe.db.get_all(
            "Employee Checkin",
            filters=[
                ["employee", "=", employee],
                ["time", ">=", start.strftime("%Y-%m-%d %H:%M:%S")],
                ["time", "<", end.strftime("%Y-%m-%d %H:%M:%S")],
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
        # PHASE-1 FRAME: emit naive PORTAL-WALL ISO (no Z). Readers parse via
        # tz.wall so legacy "…Z" strings still fold correctly.
        "planned_start": tz_utils.wall(planned_start).isoformat(),
        "planned_end": tz_utils.wall(planned_end).isoformat(),
        "work_date": day.isoformat(),
    }


def _derive_button_state(shift: dict | None, checkins: list[dict], now_local: datetime) -> str:
    """Compute the authoritative button_state (12-state machine, server side)."""
    if shift is None:
        return STATE["NO_SHIFT"]

    # Lock check (any closed monthly period covering this date).
    if _is_date_locked(shift["work_date"]):
        return STATE["LOCKED"]

    # PHASE-1 FRAME: fold both sides to naive wall before comparing.
    now_local = tz_utils.wall(now_local)
    planned_start = tz_utils.wall(datetime.fromisoformat(shift["planned_start"].replace("Z", "+00:00")))
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
    """Normalise a raw checkin ``time`` (datetime/ISO/SQL str) → portal WALL dt.

    PHASE-1 FRAME: naive values are ALREADY portal wall (the DB storage frame)
    and pass through unchanged; aware values are folded via ``tz.wall``.
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
    # PHASE-1 FRAME: naive == portal wall → pass through; aware → fold to wall.
    return tz_utils.wall(value)


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

    # PHASE-1 FRAME: everything below compares naive WALL datetimes.
    now_local = tz_utils.wall(now_local)
    first_in_raw, last_out_raw = _first_in_last_out(checkins)
    planned_start = tz_utils.wall(datetime.fromisoformat(shift["planned_start"].replace("Z", "+00:00")))
    planned_end = tz_utils.wall(datetime.fromisoformat(shift["planned_end"].replace("Z", "+00:00")))
    actual_in = _to_portal_dt(first_in_raw)
    actual_out = _to_portal_dt(last_out_raw)

    deviation = int(round((actual_in - planned_start).total_seconds() / 60.0)) if actual_in else None
    checkout_deviation = int(round((actual_out - planned_end).total_seconds() / 60.0)) if actual_out else None

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
        "actual_checkin": actual_in.isoformat() if actual_in else None,  # PORTAL WALL, no Z
        "actual_checkout": actual_out.isoformat() if actual_out else None,  # PORTAL WALL, no Z
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

    # Overnight open session — an IN from yesterday is still unpaired (the
    # employee worked past midnight and has not checked out). The NEXT tap is
    # an OUT (mobile_checkin's session parity), so the button must offer
    # CHECK-OUT even before today's shift window opens. Without this, the
    # employee sees "Chưa đến giờ chấm công" and cannot close last night's
    # shift from the app.
    if shift and button_state in (STATE["BEFORE_WINDOW"], STATE["CAN_CHECK_IN"]):
        try:
            yst_logs = _checkins_for(emp, day - timedelta(days=1))
            if _decide_log_type(list(checkins) + list(yst_logs)) == "OUT":
                button_state = STATE["CAN_CHECK_OUT"]
        except Exception:
            frappe.log_error(title="today_status: overnight parity check failed")

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


# --------------------------------------------------------------------------- #
# Cửa sổ chấm công → phiếu giải trình bắt buộc (2026-09): lượt chấm ngoài
# cửa sổ của ca (đi muộn / về sớm / đến sớm / check-out trễ) vẫn được ghi
# nhận nhưng NHÂN VIÊN PHẢI NHẬP LÝ DO — hệ thống tạo "VN Attendance
# Explanation" cho HR duyệt. Trong cửa sổ: không bắt buộc gì cả.
# --------------------------------------------------------------------------- #
def _window_violation(shift, log_type: str, at) -> dict | None:
    """Lượt chấm có vượt cửa sổ ca không? → ``{type, minutes, message}`` | None.

    * IN  muộn hơn  planned_start + vn_latest_checkin_minutes   → Late Check-in
    * IN  sớm hơn   planned_start − vn_earliest_checkin_minutes → Early Check-in (OT)
    * OUT sớm hơn   planned_end  − vn_earliest_checkout_minutes → Early Check-out
    * OUT muộn hơn  planned_end  + vn_max_checkout_after_end_minutes → Late Check-out (OT)
    """
    if not shift:
        return None
    try:
        ps = datetime.fromisoformat(str(shift.get("planned_start")).replace("Z", "+00:00"))
        pe = datetime.fromisoformat(str(shift.get("planned_end")).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    at = tz_utils.wall(at)
    if str(log_type).upper() == "IN":
        late = (at - ps).total_seconds() / 60.0
        if late > _shift_minutes("vn_latest_checkin_minutes", 30):
            m = int(late)
            return {
                "type": "Late Check-in",
                "minutes": m,
                "message": f"Đi muộn {m} phút (quá mức cho phép)",
            }
        early = (ps - at).total_seconds() / 60.0
        if early > _shift_minutes("vn_earliest_checkin_minutes", 60):
            m = int(early)
            return {
                "type": "Early Check-in (OT)",
                "minutes": m,
                "message": f"Đến sớm {m} phút — cần giải trình để tính tăng ca",
            }
        return None
    early = (pe - at).total_seconds() / 60.0
    if early > _shift_minutes("vn_earliest_checkout_minutes", 30):
        m = int(early)
        return {
            "type": "Early Check-out",
            "minutes": m,
            "message": f"Về sớm {m} phút (quá mức cho phép)",
        }
    late = (at - pe).total_seconds() / 60.0
    if late > _shift_minutes("vn_max_checkout_after_end_minutes", 360):
        m = int(late)
        return {
            "type": "Late Check-out (OT)",
            "minutes": m,
            "message": f"Check-out trễ {m} phút — cần giải trình để tính tăng ca",
        }
    return None


def _create_explanation(emp: str, day, log_type: str, shift, violation: dict, reason: str) -> str:
    """Tạo phiếu giải trình gắn với lượt chấm ngoài cửa sổ (HR duyệt sau)."""
    doc = frappe.get_doc(
        {
            "doctype": "VN Attendance Explanation",
            "employee": emp,
            "employee_name": frappe.db.get_value("Employee", emp, "employee_name"),
            "company": frappe.db.get_value("Employee", emp, "company"),
            "work_date": day.isoformat(),
            "log_type": log_type,
            "log_time": tz_utils.portal_now_str(),
            "shift_type": shift.get("shift_type") if shift else None,
            "explanation_type": violation["type"],
            "minutes_deviation": violation["minutes"],
            "reason": reason,
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


@frappe.whitelist()
def mobile_checkin(
    employee: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
    client_request_id: str | None = None,
    client_timestamp: str | None = None,
    device_id: str | None = None,
    reason: str | None = None,
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
    rate_limit(f"checkin:{emp}", max_requests=3, window_seconds=3)

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
            _("Hôm nay ({0}) thuộc kỳ công đã khoá — không thể chấm công. Liên hệ HR để mở khóa kỳ.").format(
                day.isoformat()
            )
        )

    shift = _today_shift(emp, day)
    checkins = _checkins_for(emp, day)

    # ── Overnight-aware parity ─────────────────────────────────────────────
    # Merge YESTERDAY's logs in so an overnight session opened yesterday
    # (IN 23:30) is closed by today's tap (→ OUT) instead of wrongly starting
    # a new IN that breaks the shift's hours. Stale overnight sessions were
    # already auto-closed above; their synthetic OUT is the newest log, so
    # parity correctly resolves to IN (new session).
    recent_logs = list(checkins)
    try:
        recent_logs.extend(_checkins_for(emp, day - timedelta(days=1)))
    except Exception:
        frappe.log_error(title="mobile_checkin: yesterday logs fetch failed")
    log_type = _decide_log_type(recent_logs)

    # Geofence server-side pre-check (best-effort; client already guards).
    _enforce_geofence(emp, latitude, longitude)

    server_now = tz_utils.now_in_portal()

    # ── Ngoài cửa sổ ca → BẮT BUỘC lý do (phiếu giải trình cho HR) ────────
    violation = _window_violation(shift, log_type, server_now)
    if violation and not (reason or "").strip():
        frappe.throw(
            _(
                "CẦN GIẢI TRÌNH — {0}. Nhập lý do để hoàn tất lượt chấm; phiếu giải trình sẽ gửi tới HR."
            ).format(violation["message"]),
            frappe.ValidationError,
        )

    # ── Duplicate-intent guard (at-least-once protection) ──────────────────
    # A prior request may have persisted its log but lost the HTTP response;
    # the re-tap seconds later must NOT create an OUT-right-after-IN (a
    # 0-hour "completed" day corrupts payroll). Offline replay passes: its
    # intent timestamp is far older than the last persisted log.
    #
    # FRAME NOTE — ``client_ts`` (via tz.parse_client_timestamp) is TRUE UTC,
    # while persisted ``Employee Checkin.time`` values are written with
    # ``tz.utc_now_str()`` whose frame tracks the SERVER clock (true UTC on a
    # UTC host, OS-local +7h on some benches). Derive the live offset once and
    # shift the client intent into the log frame so the delta is meaningful
    # on ANY deployment.
    intent_dt = client_ts if client_ts is not None else server_now
    if client_ts is not None:
        # client_ts is TRUE UTC; the persisted logs live in the PORTAL frame
        # (``now_in_portal`` — the frame every reader in this app compares
        # against). Shift the intent by the live portal↔UTC offset so the
        # delta against the last log is meaningful on any deployment.
        true_utc = _parse_log_dt(client_ts)
        true_utc_now = datetime.now(tz_utils.ZoneInfo("UTC")).replace(tzinfo=None)
        server_now_naive = server_now.replace(tzinfo=None) if server_now.tzinfo else server_now
        if true_utc is not None:
            intent_dt = true_utc + (server_now_naive - true_utc_now)
    last_log = max(
        (c for c in recent_logs if c.get("time")),
        key=lambda c: _parse_log_dt(c.get("time")),
        default=None,
    )
    if last_log is not None and _is_duplicate_intent(last_log.get("time"), intent_dt):
        try:
            frappe.get_doc(
                {
                    "doctype": "VN Mobile Checkin Attempt",
                    "employee": emp,
                    "client_request_id": client_request_id,
                    "device_id": device_id,
                    "client_timestamp": client_ts,
                    "server_timestamp": tz_utils.utc_now_str(),
                    "intended_log_type": log_type,
                    "status": "Duplicate",
                }
            ).insert()
            frappe.db.commit()
        except Exception:
            frappe.log_error(title="mobile_checkin: duplicate audit failed")
        return _checkin_result(
            emp,
            message=(
                "Lượt chấm vừa trước đó đã được ghi — bỏ qua lượt trùng trong "
                "vòng vài giây để tránh sai công."
            ),
        )

    # ── Self-heal warning: orphan OUT today ────────────────────────────────
    # When today holds an OUT with no IN (external device / sync artefact),
    # parity self-heals this tap as IN — but the IN's timestamp is the TAP
    # time, not the real arrival, so the day still cannot be paid correctly.
    # Flag the session for review + tell the employee to file a correction.
    selfheal_warning = None
    if log_type == "IN" and _has_out_only(checkins):
        selfheal_warning = (
            "Hôm nay có lượt RA nhưng thiếu lượt VÀO — lượt chấm này được ghi "
            "theo giờ bấm. Vui lòng nộp yêu cầu điều chỉnh (Thiếu giờ vào) với "
            "giờ vào thật để công được tính đúng."
        )
        try:
            frappe.db.set_value(
                "VN Attendance Work Session",
                {"employee": emp, "work_date": day.isoformat(), "docstatus": ["<", 2]},
                {"need_review": 1},
                update_modified=False,
            )
        except Exception:
            frappe.log_error(title="mobile_checkin: selfheal need_review failed")

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
            # PHASE-1 FRAME: stamp in PORTAL WALL (matches the live DB frame;
            # see tz.wall / plan §3). Phase-2 flips this back to utc_now_str.
            "time": tz_utils.portal_now_str(),
            "device_id": device_id or "gege_hr-mobile",
            "latitude": flt(latitude) if latitude is not None else None,
            "longitude": flt(longitude) if longitude is not None else None,
        }
    ).insert()

    # Lượt chấm ngoài cửa sổ đã có lý do → tạo phiếu giải trình cho HR duyệt.
    if violation:
        try:
            _create_explanation(emp, day, log_type, shift, violation, (reason or "").strip())
        except Exception:
            frappe.log_error(title="mobile_checkin: explanation ticket failed")

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
                planned_duration_minutes=(session_ctx["planned_duration_minutes"] if session_ctx else None),
                elapsed_minutes=session_ctx["elapsed_minutes"] if session_ctx else None,
                is_overnight=bool(shift and shift.get("is_overnight")),
            )
        except Exception:
            # Gamification must never block a successful check-in.
            frappe.log_error(title="VN gamification: apply_session_xp failed", message=f"emp={emp}")

    refreshed = _checkin_result(emp, shift=shift, message=selfheal_warning)
    refreshed["log_type"] = log_type
    if selfheal_warning:
        refreshed["need_review"] = True
    if game_result and not game_result.get("skipped"):
        refreshed["gamification"] = game_result

    # OT reminder: if checkout is after planned_end and no approved OT request
    # exists for today, surface a prompt so the employee submits one.
    if log_type == "OUT" and shift:
        try:
            pe = tz_utils.wall(datetime.fromisoformat(shift["planned_end"].replace("Z", "+00:00")))
            now_p = tz_utils.wall(server_now)
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


# Check-in parity / duplicate guards live in the bench-free pure module
# utils/checkin_parity.py (see its docstring for the R1/R2 payroll-integrity
# fixes + the full test matrix in tests/test_mobile_checkin_parity.py).
from gege_hr.gege_hr.utils.checkin_parity import (  # noqa: E402
    decide_log_type as _decide_log_type,
    has_out_only as _has_out_only,
    is_duplicate_intent as _is_duplicate_intent,
    parse_log_dt as _parse_log_dt,
)


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


def _ws_payload(r) -> dict:
    """Project ONE ``VN Attendance Work Session`` row → the SPA day-row dict.

    Shared by ``my_logs`` and ``my_day_detail`` (plan
    plans/plan-monthly-attendance-self-deskfree.md §3.1) so the month list and
    the day drawer always show the SAME engine-computed values.
    """
    # Derive a Frappe-compatible status from the Work Session flags.
    if r.absent:
        status = "Absent"
    elif r.has_leave:
        status = "On Leave"
    elif flt(r.payable_day or 0) == 0.5:
        status = "Half Day"
    else:
        status = "Present"

    return {
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
        # Engine-synthesised OUT (checkout-miss auto-close): the UI must
        # keep showing "Quên chấm ra", not a green completed day.
        "vn_auto_checkout": bool(r.vn_auto_checkout),
        "need_review": bool(r.need_review),
        "shift_instance": getattr(r, "shift_instance", None) or None,
    }


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
            "vn_auto_checkout",
        ],
        order_by="work_date desc",
        limit_page_length=500,
    )

    return [_ws_payload(r) for r in ws_rows]


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
            # PHASE-1 FRAME: out_time is naive PORTAL WALL — fold pe to wall too.
            pe_wall = tz_utils.wall(pe_local)
            out_wall = tz_utils.wall(get_datetime(out_time))
            delta = (out_wall - pe_wall).total_seconds()
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

    FIX 2026-09-11 — aggregate from VN Attendance Work Sessions (engine truth)
    instead of core ``Attendance`` rows, for two reasons found in production:
    1. The sync projection (``build_attendance_fields``) never writes
       late_entry / early_exit / working-hours onto Attendance, so every
       Attendance-based counter (đi muộn, về sớm, OT) was permanently 0.
    2. A whole-month ``backfill_attendance`` run created Present rows for
       FUTURE dates — on 11/09 the tile showed "30 ca". The stat window is
       now capped at today: a planned-only session (no punches yet) can
       never count as worked.
    """
    emp = _resolve_employee(employee)
    now = tz_utils.now_in_portal()
    y, m = _parse_year_month(year, month, now)
    start = date(y, m, 1)
    next_first = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    end = next_first - timedelta(days=1)
    # Session kế hoạch cho ngày tương lai không được tính vào thống kê.
    stat_end = min(end, now.date())

    worked_days = 0
    worked_minutes = 0.0
    late_count = 0
    late_minutes = 0.0
    early_count = 0
    early_minutes = 0.0
    overtime_hours = 0.0
    absent_count = 0
    leave_days = 0
    missing_checkout_count = 0
    if start <= stat_end:
        ws_rows = frappe.db.get_all(
            "VN Attendance Work Session",
            filters={
                "employee": emp,
                "work_date": ["between", [start, stat_end]],
                "docstatus": ["!=", 2],
            },
            fields=[
                "actual_checkin",
                "actual_checkout",
                "late_minutes",
                "early_leave_minutes",
                "total_actual_hours",
                "raw_overtime_hours",
                "approved_overtime_hours",
                "absent",
                "has_leave",
                "missing_checkout",
            ],
        )
        for r in ws_rows:
            if r.get("actual_checkin") or r.get("actual_checkout"):
                worked_days += 1
            worked_minutes += flt(r.get("total_actual_hours") or 0) * 60
            lm = flt(r.get("late_minutes") or 0)
            if lm > 0:
                late_count += 1
            late_minutes += lm
            em = flt(r.get("early_leave_minutes") or 0)
            if em > 0:
                early_count += 1
            early_minutes += em
            overtime_hours += flt(r.get("approved_overtime_hours") or r.get("raw_overtime_hours") or 0)
            if r.get("absent"):
                absent_count += 1
            if r.get("has_leave"):
                leave_days += 1
            if r.get("missing_checkout"):
                missing_checkout_count += 1

    # Legacy rows view (core Attendance) kept for compatibility consumers;
    # bounded to stat_end so seeded future rows never leak into month views.
    rows = frappe.db.get_all(
        "Attendance",
        filters={"employee": emp, "attendance_date": ["between", [start, stat_end]]},
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
        # Top-level totals — the SPA contract (AttendanceView check-in tiles +
        # DashboardView KPIs). All Work-Session aggregates, capped at today.
        "worked_days": worked_days,
        "payable_days": worked_days,
        "late_count": late_count,
        "absent_count": absent_count,
        "leave_days": leave_days,
        "overtime_hours": round(overtime_hours, 2),
        # Tile extras for the check-in screen (AttendanceView summaryRows).
        "worked_minutes": round(worked_minutes),
        "late_minutes": round(late_minutes),
        "early_leave_count": early_count,
        "early_leave_minutes": round(early_minutes),
        "missing_checkout_count": missing_checkout_count,
        # Nested view kept for other consumers / future use (key names unchanged).
        "summary": {
            "present": worked_days,
            "absent": absent_count,
            "leave": leave_days,
            "half_day": 0,
            "worked_days": worked_days,
            "payable_days": worked_days,
            "late_days": late_count,
            "early_exit_days": early_count,
            "overtime_hours": round(overtime_hours, 2),
        },
        "rows": rows,
    }


@frappe.whitelist()
def my_month_meta(employee: str | None = None, year: int | None = None, month: int | None = None) -> dict:
    """Employee self-service month metadata (plan
    plans/plan-monthly-attendance-self-deskfree.md BE-1).

    One read powering the SPA ``/hr/attendance/monthly`` page: lock state of
    the closing period, holiday dates (for gap-day synthesis), the
    Work-Session-based summary (tiles stay truthful BEFORE the HR admin
    generates core ``Attendance`` rows) and ``standard_days``.
    """
    emp = _resolve_employee(employee)
    now = tz_utils.now_in_portal()
    y, m = _parse_year_month(year, month, now)
    start = date(y, m, 1)
    next_first = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    end = next_first - timedelta(days=1)

    # ── Closing period covering the month (name/status only — no internals). ──
    period = None
    locked = False
    try:
        rows = frappe.db.get_all(
            "VN Monthly Attendance Period",
            filters=[
                ["from_date", "<=", str(end)],
                ["to_date", ">=", str(start)],
                ["docstatus", "!=", 2],
            ],
            fields=["name", "status"],
            order_by="from_date desc",
            limit_page_length=1,
        )
        if rows:
            period = {"name": rows[0].get("name"), "status": rows[0].get("status")}
            locked = (rows[0].get("status") or "") == "Locked"
    except Exception:
        period = None

    # ── Holidays of the employee's resolved Holiday List (best-effort). ──────
    holidays: list[str] = []
    try:
        from gege_hr.gege_hr.utils import calc as _calc  # lazy: pure/guarded module

        holidays = sorted(str(d) for d in _calc.load_holiday_dates(start, end, emp))
    except Exception:
        holidays = []

    holiday_set = set(holidays)
    total_days = (end - start).days + 1
    sundays = sum(1 for i in range(total_days) if (start + timedelta(days=i)).weekday() == 6)
    standard_days = total_days - sundays - len(holiday_set)

    # ── Work-Session summary (engine truth — same framing as my_logs tiles). ──
    ws_rows = frappe.db.get_all(
        "VN Attendance Work Session",
        filters={
            "employee": emp,
            "work_date": ["between", [start, end]],
            "docstatus": ["!=", 2],
        },
        fields=[
            "actual_checkin",
            "actual_checkout",
            "late_minutes",
            "payable_day",
            "absent",
            "has_leave",
            "raw_overtime_hours",
            "approved_overtime_hours",
        ],
    )
    worked_days = 0
    late_count = 0
    payable_days = 0.0
    absent_count = 0
    leave_days = 0
    overtime_hours = 0.0
    for r in ws_rows:
        if r.get("actual_checkin") or r.get("actual_checkout"):
            worked_days += 1
        if flt(r.get("late_minutes") or 0) > 0:
            late_count += 1
        payable_days += flt(r.get("payable_day") or 0)
        if r.get("absent"):
            absent_count += 1
        if r.get("has_leave"):
            leave_days += 1
        overtime_hours += flt(r.get("approved_overtime_hours") or r.get("raw_overtime_hours") or 0)

    has_attendance = bool(
        frappe.db.exists(
            "Attendance",
            {"employee": emp, "attendance_date": ["between", [start, end]]},
        )
    )

    return {
        "employee": emp,
        "year": y,
        "month": m,
        "locked": locked,
        "period": period,
        "holidays": holidays,
        "standard_days": standard_days,
        "has_attendance": has_attendance,
        "ws_summary": {
            "worked_days": worked_days,
            "payable_days": flt(payable_days, 2),
            "late_count": late_count,
            "absent_count": absent_count,
            "leave_days": leave_days,
            "overtime_hours": flt(overtime_hours, 2),
        },
    }


@frappe.whitelist()
def my_day_detail(employee: str | None = None, work_date: str | None = None) -> dict:
    """Employee self-service day detail (plan
    plans/plan-monthly-attendance-self-deskfree.md BE-2).

    Read-only 360° of ONE work_date: Work Session (same projection as
    ``my_logs``), raw punches, correction requests, overtime requests, the
    official ``Attendance`` row and the closing-period lock flag. Empty
    sections — never throws — for days with no data.
    """
    if not work_date:
        frappe.throw(_("Thiếu ngày cần tra cứu."), frappe.ValidationError)
    try:
        day = getdate(work_date)
    except Exception:
        day = None
    if not day:
        frappe.throw(_("Ngày tra cứu không hợp lệ."), frappe.ValidationError)
    emp = _resolve_employee(employee)
    return _day_detail_core(emp, day)


def _day_detail_core(emp: str, day: date) -> dict:
    """Shared read-only 360° payload of ONE member-day.

    Extracted from ``my_day_detail`` (plans/team-today-desk-free.md §2.2) so
    ``team_member_day_detail`` reuses the exact same sections — Work Session,
    raw punches, correction requests, overtime requests, the official
    ``Attendance`` row and the closing-period lock flag. Empty sections —
    never throws — for days with no data.
    """
    day_str = str(day)

    ws_rows = frappe.db.get_all(
        "VN Attendance Work Session",
        filters={"employee": emp, "work_date": day, "docstatus": ["!=", 2]},
        fields=[
            "name",
            "work_date",
            "shift_type",
            "shift_instance",
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
            "vn_auto_checkout",
        ],
        order_by="creation desc",
        limit_page_length=1,
    )
    work_session = _ws_payload(ws_rows[0]) if ws_rows else None

    # Raw punches — reuse the portal-wall day window helper (attribute rows).
    # `prev_session=True` với lượt chấm TRƯỚC giờ bắt đầu ca của ngày (ca qua
    # đêm hôm trước kết thúc sáng nay — vd Ra 10:00 sáng 10/09 thuộc phiên
    # 09/09, không phải giờ ra của ca 10/09) — để UI gắn nhãn tránh hiểu nhầm
    # "đã có giờ ra mà vẫn báo quên chấm ra".
    # Ngưỡng 4h: đến sớm hợp lý (vd 20:33 cho ca 21:00) vẫn thuộc ca này;
    # lượt chấm sáng sớm (vd Ra 10:00 trước ca 21:00 tới 11h) mới là của
    # phiên ca đêm hôm trước.
    _ps = (work_session or {}).get("planned_start")
    _ps_str = str(_ps)[:16] if _ps else None
    _cutoff = None
    if _ps_str:
        try:
            from datetime import datetime as _dt, timedelta as _td

            _cutoff = (_dt.fromisoformat(_ps_str) - _td(minutes=240)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            _cutoff = None
    punches = []
    for p in _checkins_for(emp, day):
        t = str(p.get("time") or "")[:16]
        punches.append(
            {
                "name": p.get("name"),
                "time": str(p.get("time")) if p.get("time") else None,
                "log_type": p.get("log_type"),
                "device_id": p.get("device_id"),
                "latitude": p.get("latitude"),
                "longitude": p.get("longitude"),
                "prev_session": bool(_cutoff and t and t < _cutoff),
            }
        )

    try:
        corrections = frappe.db.get_all(
            CR_DOCTYPE,
            filters={"employee": emp, "work_date": day, "docstatus": ["!=", 2]},
            fields=["name", "correction_type", "reason", "workflow_state", "docstatus"],
            order_by="creation desc",
        )
    except Exception:
        corrections = []

    try:
        overtime_requests = frappe.db.get_all(
            "VN Overtime Request",
            filters={"employee": emp, "work_date": day, "docstatus": ["!=", 2]},
            fields=["name", "from_datetime", "to_datetime", "requested_hours", "workflow_state"],
            order_by="creation desc",
        )
    except Exception:
        overtime_requests = []

    att_rows = frappe.db.get_all(
        "Attendance",
        filters={"employee": emp, "attendance_date": day, "docstatus": ["!=", 2]},
        fields=[
            "name",
            "status",
            "in_time",
            "out_time",
            "late_entry",
            "early_exit",
            "working_hours",
            "docstatus",
        ],
        order_by="docstatus desc, creation desc",
        limit_page_length=1,
    )
    attendance = dict(att_rows[0]) if att_rows else None

    return {
        "work_date": day_str,
        "locked": _is_date_locked(day_str),
        "work_session": work_session,
        "punches": punches,
        "corrections": corrections,
        "overtime_requests": overtime_requests,
        "attendance": attendance,
    }


# --------------------------------------------------------------------------- #
# Team Today — desk-free manager roster (plans/team-today-desk-free.md)
# --------------------------------------------------------------------------- #
TEAM_VIEW_ROLES = ("HR Manager", "HR User", "System Manager", "Line Manager")
TEAM_STATUS_TOKENS = (
    "Present",
    "Late",
    "Absent",
    "On Leave",
    "Half Day",
    "Week Off",
    "Work From Home",
    "Not Checked In",
)

_NUDGE_TITLES = {
    "missing_checkin": (
        "Nhắc chấm công vào",
        "Bạn chưa chấm công VÀO cho ngày {day}. Hãy chấm công ngay để ngày làm việc được tính đủ.",
    ),
    "missing_checkout": (
        "Nhắc chấm công ra",
        "Bạn chưa chấm công RA cho ngày {day}. Hãy chấm công để hoàn tất ngày làm việc.",
    ),
}


def _team_viewer() -> tuple[str | None, bool]:
    """Resolve the caller's team-view context → ``(manager_emp, is_hr)``.

    Gate (plan §2.1 — fix D2): HR Manager / HR User / System Manager may view;
    a Line Manager is scoped to their ``reports_to`` team. Any other role →
    PermissionError.
    """
    roles = set(emp_utils.get_user_roles() or [])
    if not roles & set(TEAM_VIEW_ROLES):
        frappe.throw(_("Bạn không có quyền xem tình trạng team."), frappe.PermissionError)
    is_hr = bool(roles & {"HR Manager", "HR User", "System Manager"})
    return emp_utils.get_employee_for_user(), is_hr


def _is_line_manager_of(manager_emp: str | None, employee) -> bool:
    """True khi ``employee.reports_to`` là ``manager_emp`` (scope F5)."""
    if not manager_emp or not employee:
        return False
    try:
        target = emp_utils.emp_name(employee)
        return bool(target) and frappe.db.get_value("Employee", target, "reports_to") == manager_emp
    except Exception:
        return False


def _can_view_member(manager_emp: str | None, is_hr: bool, employee) -> bool:
    """HR đọc được mọi nhân viên; Line Manager chỉ member trong team mình."""
    if not employee:
        return False
    if is_hr:
        return True
    return _is_line_manager_of(manager_emp, employee)


def _team_scope_members(manager_emp: str | None, is_hr: bool) -> list[dict]:
    """Active employees of the caller's team (one query — plan §2.1).

    HR giữ nguyên hành vi cũ (team ``reports_to`` của họ); khi HR không quản lý
    ai thì fallback toàn công ty (scope HR). Line Manager chưa link Employee
    thì không có member nào.
    """
    filters: dict = {"status": "Active"}
    if manager_emp:
        filters["reports_to"] = manager_emp
    elif not is_hr:
        return []
    return frappe.db.get_all(
        "Employee",
        filters=filters,
        fields=["name", "employee_name", "designation", "reports_to"],
    )


def _team_member_status(att, ws) -> str:
    """Pure — canonical Team Today status token for one member-day.

    ``att``: Attendance row dict (or None); ``ws``: ``_ws_payload`` dict (or
    None). Vocabulary: Present / Late / Absent / On Leave / Half Day /
    Week Off / Work From Home / Not Checked In — never "Not marked" (fix D3).
    """
    att = att or {}
    ws = ws or {}
    a_status = str(att.get("status") or "").strip()
    late = bool(att.get("late_entry")) or int(ws.get("late_minutes") or 0) > 0
    if not a_status:
        if not ws:
            return "Not Checked In"
        if ws.get("absent"):
            return "Absent"
        if ws.get("has_leave"):
            return "On Leave"
        if ws.get("missing_checkin"):
            return "Not Checked In"
        return "Late" if late else "Present"
    if a_status == "Present":
        return "Late" if late else "Present"
    return a_status


def summarize_team(members) -> dict:
    """Pure — summary card counts over the (already filtered) member rows."""
    counts = {
        "total": len(members or []),
        "present": 0,
        "late": 0,
        "absent": 0,
        "on_leave": 0,
        "not_checked_in": 0,
    }
    for m in members or []:
        st = str((m or {}).get("status") or "Not Checked In")
        if st == "Present":
            counts["present"] += 1
        elif st == "Late":
            counts["late"] += 1
        elif st == "Absent":
            counts["absent"] += 1
        elif st in ("On Leave", "Half Day"):
            counts["on_leave"] += 1
        elif st == "Not Checked In":
            counts["not_checked_in"] += 1
    return counts


def _filter_team_rows(rows, search, status):
    """Pure — server-side broad search + status token filter (DNA §6.6 D)."""
    q = str(search or "").strip().lower()
    st = str(status or "").strip()
    out = []
    for r in rows or []:
        if q:
            hay = f"{(r.get('employee_name') or '')} {(r.get('name') or '')}".lower()
            if q not in hay:
                continue
        if st and str(r.get("status") or "") != st:
            continue
        out.append(r)
    return out


def _team_member_row(member, att, ws_payload, shift_name, pending, has_cm, leave) -> dict:
    """Pure-ish assembly — one roster row from the batched contexts."""
    ws = ws_payload or {}
    att = att or {}
    status = _team_member_status(att, ws)
    return {
        "name": member.get("name"),
        "employee": member.get("name"),
        "employee_name": member.get("employee_name"),
        "designation": member.get("designation"),
        "status": status,
        "shift_type_name": shift_name or ws.get("shift_type") or "",
        "in_time": str(att["in_time"]) if att.get("in_time") else None,
        "out_time": str(att["out_time"]) if att.get("out_time") else None,
        "checkin_time": ws.get("actual_checkin"),
        "checkout_time": ws.get("actual_checkout"),
        "late_minutes": int(ws.get("late_minutes") or 0),
        "early_leave_minutes": int(ws.get("early_leave_minutes") or 0),
        "late_entry": bool(att.get("late_entry")),
        "early_exit": bool(att.get("early_exit")),
        "pending_counts": pending or {"corrections": 0, "overtime": 0, "leaves": 0},
        "has_checkout_miss": bool(has_cm),
        "leave_application": leave,
    }


def _team_attendance_by(emps: list[str], day) -> dict:
    rows = frappe.db.get_all(
        "Attendance",
        filters={"employee": ["in", emps], "attendance_date": day, "docstatus": ["!=", 2]},
        fields=["name", "employee", "status", "in_time", "out_time", "late_entry", "early_exit"],
        order_by="docstatus desc, creation desc",
    )
    by: dict = {}
    for r in rows:
        by.setdefault(r.get("employee"), r)
    return by


def _team_ws_by(emps: list[str], day) -> dict:
    rows = frappe.db.get_all(
        "VN Attendance Work Session",
        filters={"employee": ["in", emps], "work_date": day, "docstatus": ["!=", 2]},
        fields=[
            "name",
            "employee",
            "work_date",
            "shift_type",
            "shift_instance",
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
            "vn_auto_checkout",
        ],
        order_by="creation desc",
    )
    by: dict = {}
    for r in rows:
        by.setdefault(r.get("employee"), _ws_payload(r))
    return by


def _covering_shift_assignments(emps: list[str], day) -> list[dict]:
    """Active Shift Assignments of the team that may cover ``day``."""
    if not emps:
        return []
    try:
        return frappe.db.get_all(
            "Shift Assignment",
            filters={
                "employee": ["in", emps],
                "status": "Active",
                "docstatus": 1,
                "start_date": ["<=", day],
            },
            fields=["name", "employee", "shift_type", "start_date", "end_date"],
            order_by="start_date desc",
        )
    except Exception:
        return []


def _team_shift_names_by(emps: list[str], day) -> dict:
    """``{employee: shift_type}`` — the newest assignment covering ``day``."""
    by: dict = {}
    day_s = str(day)
    for r in _covering_shift_assignments(emps, day):
        emp = r.get("employee")
        if not emp or emp in by:
            continue
        end = r.get("end_date")
        if end and str(end) < day_s:
            continue
        by[emp] = r.get("shift_type")
    return by


def _team_pending_counts(emps: list[str]) -> dict:
    """Pending correction / OT / leave counts per member (badge data)."""
    out = {e: {"corrections": 0, "overtime": 0, "leaves": 0} for e in emps}
    specs = (
        ("VN Attendance Correction Request", "workflow_state", "corrections"),
        ("VN Overtime Request", "workflow_state", "overtime"),
        ("Leave Application", "status", "leaves"),
    )
    for doctype, state_field, key in specs:
        try:
            rows = frappe.db.get_all(
                doctype,
                filters={"employee": ["in", emps], "docstatus": ["!=", 2]},
                fields=["employee", state_field],
            )
        except Exception:
            continue
        for r in rows:
            emp = r.get("employee")
            if emp not in out:
                continue
            state = str(r.get(state_field) or "").strip().lower()
            if "pending" in state or state == "open":
                out[emp][key] += 1
    return out


def _team_checkout_miss_by(emps: list[str]) -> dict:
    """``{employee: latest open VN Checkout Miss row}`` (badge flag)."""
    if not emps:
        return {}
    try:
        rows = frappe.db.get_all(
            "VN Checkout Miss",
            filters={"employee": ["in", emps], "docstatus": ["!=", 2]},
            fields=["name", "employee", "status", "work_date"],
            order_by="creation desc",
        )
    except Exception:
        return {}
    by: dict = {}
    for r in rows:
        by.setdefault(r.get("employee"), r)
    return by


def _team_leave_by(emps: list[str], day) -> dict:
    """``{employee: approved Leave Application covering ``day``}``."""
    if not emps:
        return {}
    try:
        rows = frappe.db.get_all(
            "Leave Application",
            filters={
                "employee": ["in", emps],
                "from_date": ["<=", day],
                "to_date": [">=", day],
                "status": "Approved",
                "docstatus": 1,
            },
            fields=["name", "employee", "leave_type", "from_date", "to_date"],
        )
    except Exception:
        return {}
    by: dict = {}
    for r in rows:
        by.setdefault(
            r.get("employee"),
            {
                "name": r.get("name"),
                "leave_type": r.get("leave_type"),
                "from_date": str(r.get("from_date") or ""),
                "to_date": str(r.get("to_date") or ""),
            },
        )
    return by


def _publish_team_today(manager_emp: str | None = None) -> None:
    """Best-effort realtime ping cho các tab ``/team/today`` đang mở (§2.6)."""
    try:
        frappe.publish_realtime(
            "team_today_changed", {"manager": manager_emp or ""}, user=frappe.session.user
        )
    except Exception:
        pass


@frappe.whitelist()
def team_daily_status(
    date_str: str | None = None,
    search: str | None = None,
    status: str | None = None,
) -> dict:
    """Plan §10.2 + plans/team-today-desk-free.md §2.1 — manager snapshot v2.

    One row per team member for a day, batched (no N+1). Returns
    ``{work_date, locked, summary, members}`` — the dict shape the SPA
    ``fetchTeamDailyStatus`` always expected (fix D1). ``search`` /
    ``status`` filter server-side (backlog BL); the status vocabulary is the
    canonical Team Today tokens (fix D3 — never "Not marked").
    """
    manager_emp, is_hr = _team_viewer()
    day = getdate(date_str) if date_str else tz_utils.now_in_portal().date()
    token = str(status or "").strip()
    if token and token not in TEAM_STATUS_TOKENS:
        frappe.throw(_("Trạng thái lọc không hợp lệ."), frappe.ValidationError)
    members = _team_scope_members(manager_emp, is_hr)
    emps = [m.get("name") for m in members]
    att_by = _team_attendance_by(emps, day)
    ws_by = _team_ws_by(emps, day)
    shift_by = _team_shift_names_by(emps, day)
    pending_by = _team_pending_counts(emps)
    cm_by = _team_checkout_miss_by(emps)
    leave_by = _team_leave_by(emps, day)
    rows = [
        _team_member_row(
            m,
            att_by.get(m.get("name")),
            ws_by.get(m.get("name")),
            shift_by.get(m.get("name")),
            pending_by.get(m.get("name")),
            cm_by.get(m.get("name")),
            leave_by.get(m.get("name")),
        )
        for m in members
    ]
    rows = _filter_team_rows(rows, search, token)
    return {
        "work_date": str(day),
        "locked": _is_date_locked(str(day)),
        "summary": summarize_team(rows),
        "members": rows,
    }


def team_day_can(*, status, locked, is_hr, is_lm_of, ws=None, is_today=False) -> dict:
    """Pure — server-driven action matrix cho Team Today drawer (plan §2.2).

    FE chỉ render theo matrix này; BE là nguồn sự thật duy nhất.
    """
    ws = ws or {}
    manage = bool(is_hr or is_lm_of) and not bool(locked)
    needs_checkin_nudge = str(status) == "Not Checked In"
    needs_checkout_nudge = str(status) in ("Present", "Late") and bool(ws.get("missing_checkout"))
    return {
        "view_detail": True,
        "fix_punch": manage,
        # mark_attendance yêu cầu thêm is_hr: ``mark_attendance_bulk``
        # (Employee Attendance Tool parity) chỉ mở cho HR roles — LM sẽ bị
        # 403 nếu gọi (P2: nới gate theo scope reports_to nếu cần).
        "mark_attendance": bool(is_hr) and not bool(locked),
        "request_correction": manage,
        "override_shift": manage and bool(is_today),
        "nudge": needs_checkin_nudge or needs_checkout_nudge,
        "open_360": bool(is_hr),
        "export": bool(is_hr or is_lm_of),
    }


def _team_att_cell_can(
    *,
    is_hr: bool,
    is_lm_of: bool,
    locked: bool = False,
    is_future: bool = False,
    has_punch: bool = False,
    raw_ot: float = 0.0,
    open_cm: bool = False,
    is_ot_approver: bool = False,
) -> dict:
    """Pure — per-day-cell action matrix cho /hr/team/attendance (plan
    plan-team-attendance-desk-free.md §4 WP2).

    Mirror semantics của :func:`team_day_can` (Team-Today drawer) nhưng cho
    RANGE grid: mỗi ô (member × day) mang một khối ``can`` riêng do BE tính từ
    role + scope + lock state + day state. FE chỉ render nút theo matrix —
    BE là nguồn sự thật duy nhất; lock-guard phía server vẫn là cổng cuối.

    Rules (plan §4 WP2):
      * ``locked``  → mọi cờ mutation = false với MỌI role;
      * ``is_future`` → fix_punch / delete_punch / mark_attendance /
        create_request = false (chưa đến ngày, không có gì để sửa);
      * Line Manager chỉ được thao tác trên member ``reports_to`` mình
        (``is_lm_of``) — member ngoài team mọi cờ manage = false;
      * ``approve_ot`` yêu cầu role OT approver (HR Manager / System Manager,
        parity ``OT_APPROVER_ROLES``) VÀ session có raw OT > 0;
      * ``resolve_checkout_miss`` yêu cầu HR + ticket đang mở.
    """
    manage = bool(is_hr or is_lm_of)
    writable_day = manage and not bool(locked) and not bool(is_future)
    try:
        raw_ot_hours = float(raw_ot or 0)
    except (TypeError, ValueError):
        raw_ot_hours = 0.0
    return {
        "view_detail": True,
        "fix_punch": writable_day,
        "delete_punch": writable_day and bool(has_punch),
        "mark_attendance": bool(is_hr) and not bool(locked) and not bool(is_future),
        "create_request": writable_day,
        "approve_ot": bool(is_ot_approver) and raw_ot_hours > 0 and not bool(locked),
        "resolve_checkout_miss": bool(is_hr) and bool(open_cm) and not bool(locked),
        "recalc": bool(is_hr) and not bool(locked),
        "nudge": manage,
    }


def _shift_meta_one(employee: str, day) -> dict | None:
    """Shift Assignment covering ``day`` of ONE member (drawer meta)."""
    for r in _covering_shift_assignments([employee], day):
        end = r.get("end_date")
        if not end or str(end) >= str(day):
            return {
                "name": r.get("name"),
                "shift_type": r.get("shift_type"),
                "start_date": str(r.get("start_date") or ""),
                "end_date": str(end or ""),
            }
    return None


def _leave_meta_one(employee: str, day) -> dict | None:
    return _team_leave_by([employee], day).get(employee)


def _checkout_miss_meta_one(employee: str, work_date: str | None = None) -> dict | None:
    """Ticket "quên chấm ra" MỚI NHẤT của nhân viên — lọc theo ngày drawer.

    FIX 2026-09-11: trước đây trả ticket bất kể ngày → ticket của phiên khác
    (vd 03/09 ca đêm) hiện trên drawer của MỌI ngày (02/09 thiếu công cũng
    hiện "Quên chấm ra" gây hiểu nhầm). Giờ chỉ hiện khi ngày drawer khớp
    work_date của ticket, hoặc ngày hôm sau (ca qua đêm kết thúc sáng hôm sau).
    """
    r = _team_checkout_miss_by([employee]).get(employee)
    if not r:
        return None
    if work_date:
        cm_day = str(r.get("work_date") or "")
        try:
            from frappe.utils import getdate as _gd

            next_day = (_gd(cm_day) + timedelta(days=1)).isoformat() if cm_day else None
        except Exception:
            next_day = None
        if work_date not in (cm_day, next_day):
            return None
    return {
        "name": r.get("name"),
        "status": r.get("status"),
        "work_date": str(r.get("work_date") or ""),
    }


def _member_pending_lists(employee: str) -> dict:
    """Pending correction/OT docs of ONE member (drawer badges, light)."""
    out = {"corrections": [], "overtime": []}
    specs = (
        ("VN Attendance Correction Request", "corrections", "work_date"),
        ("VN Overtime Request", "overtime", "from_datetime"),
    )
    for doctype, key, date_field in specs:
        try:
            rows = frappe.db.get_all(
                doctype,
                filters={"employee": employee, "docstatus": ["!=", 2]},
                fields=["name", "workflow_state", date_field],
                order_by="creation desc",
                limit_page_length=10,
            )
        except Exception:
            continue
        for r in rows:
            state = str(r.get("workflow_state") or "").strip().lower()
            if "pending" in state:
                out[key].append({"name": r.get("name"), "state": r.get("workflow_state")})
    return out


@frappe.whitelist()
def team_member_day_detail(employee: str | None = None, date_str: str | None = None) -> dict:
    """plans/team-today-desk-free.md §2.2 — chi tiết 1 member-day cho drawer.

    Tái dùng ``_day_detail_core`` (đúng payload self-service) + enrich shift /
    leave / checkout-miss / pending approvals + can matrix server-driven.
    Gate: HR đọc ai cũng được; Line Manager chỉ member ``reports_to`` mình.
    """
    manager_emp, is_hr = _team_viewer()
    if not employee:
        frappe.throw(_("Thiếu nhân viên cần tra cứu."), frappe.ValidationError)
    emp = emp_utils.emp_name(employee)
    if not _can_view_member(manager_emp, is_hr, emp):
        frappe.throw(_("Bạn chỉ được xem nhân viên trong team của mình."), frappe.PermissionError)
    day = getdate(date_str) if date_str else tz_utils.now_in_portal().date()
    core = _day_detail_core(emp, day)
    ws = core.get("work_session") or {}
    status = _team_member_status(core.get("attendance"), ws)
    is_today = str(day) == str(tz_utils.now_in_portal().date())
    return {
        **core,
        "employee": emp,
        "status": status,
        "shift": _shift_meta_one(emp, day),
        "leave_application": _leave_meta_one(emp, day),
        "checkout_miss": _checkout_miss_meta_one(emp, work_date=date_str or str(day)),
        "pending_approvals": _member_pending_lists(emp),
        "can": team_day_can(
            status=status,
            locked=bool(core.get("locked")),
            is_hr=is_hr,
            is_lm_of=_is_line_manager_of(manager_emp, emp),
            ws=ws,
            is_today=is_today,
        ),
    }


@frappe.whitelist()
def nudge_team_member(
    employee: str | None = None,
    kind: str | None = None,
    date_str: str | None = None,
) -> dict:
    """plans/team-today-desk-free.md §2.3 — nhắc member check-in/out.

    Tạo 1 VN Notification (best-effort) + audit 1 dòng + realtime ping. Chống
    spam bằng ``rate_limit`` 1 lần / employee / kind / ngày / 10 phút (F8).
    """
    manager_emp, is_hr = _team_viewer()
    kind = str(kind or "").strip()
    if kind not in _NUDGE_TITLES:
        frappe.throw(_("Loại nhắc không hợp lệ."), frappe.ValidationError)
    if not employee:
        frappe.throw(_("Thiếu nhân viên cần nhắc."), frappe.ValidationError)
    emp = emp_utils.emp_name(employee)
    if not _can_view_member(manager_emp, is_hr, emp):
        frappe.throw(_("Bạn chỉ được nhắc nhân viên trong team của mình."), frappe.PermissionError)
    day = getdate(date_str) if date_str else tz_utils.now_in_portal().date()
    core = _day_detail_core(emp, day)
    ws = core.get("work_session") or {}
    status = _team_member_status(core.get("attendance"), ws)
    can = team_day_can(
        status=status,
        locked=bool(core.get("locked")),
        is_hr=is_hr,
        is_lm_of=_is_line_manager_of(manager_emp, emp),
        ws=ws,
        is_today=str(day) == str(tz_utils.now_in_portal().date()),
    )
    if not can["nudge"]:
        frappe.throw(
            _("Nhân viên này hiện không cần nhắc (đã đủ check-in/out hoặc trạng thái không áp dụng)."),
            frappe.ValidationError,
        )
    rate_limit(f"nudge:{emp}:{kind}:{day}", max_requests=1, window_seconds=600)
    title, body = _NUDGE_TITLES[kind]
    name = notify_util.push_notification(
        employee=emp,
        notification_type="Reminder",
        title=title,
        message=body.format(day=str(day)),
        action_url="/hr/attendance",
    )
    try:
        audit_api.log(
            "Manual Override",
            employee=emp,
            work_date=str(day),
            company=frappe.db.get_value("Employee", emp, "company"),
            description=f"Nudge {kind} ngày {day}",
        )
    except Exception:
        pass
    _publish_team_today(manager_emp)
    return {"name": name, "employee": emp, "kind": kind, "work_date": str(day)}


def _csv_cell(v) -> str:
    s = str(v if v is not None else "")
    if any(ch in s for ch in (",", '"', "\n")):
        return '"' + s.replace('"', '""') + '"'
    return s


@frappe.whitelist()
def export_team_day_csv(
    date_str: str | None = None,
    search: str | None = None,
    status: str | None = None,
) -> dict:
    """plans/team-today-desk-free.md §2.4 — CSV (UTF-8 BOM) của bảng team ngày."""
    res = team_daily_status(date_str=date_str, search=search, status=status)
    header = [
        "Mã NV",
        "Tên",
        "Chức danh",
        "Ca",
        "Giờ vào",
        "Giờ ra",
        "Muộn (phút)",
        "Về sớm (phút)",
        "Trạng thái",
        "Đơn chờ",
    ]
    lines = [",".join(header)]
    for m in res["members"]:
        pc = m.get("pending_counts") or {}
        pending = sum(int(pc.get(k) or 0) for k in ("corrections", "overtime", "leaves"))
        cells = [
            m.get("name"),
            m.get("employee_name"),
            m.get("designation"),
            m.get("shift_type_name"),
            m.get("checkin_time") or "",
            m.get("checkout_time") or "",
            m.get("late_minutes"),
            m.get("early_leave_minutes"),
            m.get("status"),
            pending,
        ]
        lines.append(",".join(_csv_cell(c) for c in cells))
    return {
        "filename": f"team-{res['work_date']}.csv",
        "csv": "\ufeff" + "\n".join(lines),
        "rows": len(res["members"]),
    }


TEAM_ATTENDANCE_MAX_DAYS = 62


def _locked_days_between(start, end) -> tuple[set[str], dict]:
    """Days of ``[start, end]`` covered by a **Locked** ``VN Monthly Attendance
    Period`` + the header period block (plan-team-attendance-desk-free §4 WP1/WP2).

    Returns ``({locked iso dates}, {"name", "status"} | {})`` — a Locked row
    wins the header slot, otherwise the newest overlapping period shows.
    Best-effort: never raises (bench stubs / missing doctype → empty).
    """
    locked: set[str] = set()
    try:
        rows = frappe.db.get_all(
            "VN Monthly Attendance Period",
            filters={"from_date": ["<=", end], "to_date": [">=", start]},
            fields=["name", "from_date", "to_date", "status"],
            order_by="from_date desc",
        )
    except Exception:
        return locked, {}
    rows = rows or []
    first_any: dict = rows[0] if rows else {}
    for row in rows:
        if str(row.get("status") or "") != "Locked":
            continue
        d = max(getdate(row.get("from_date")), start)
        stop = min(getdate(row.get("to_date")), end)
        while d <= stop:
            locked.add(str(d))
            d += timedelta(days=1)
    header = next(
        (r for r in rows if str(r.get("status") or "") == "Locked"),
        first_any,
    )
    return locked, ({"name": header.get("name"), "status": header.get("status")} if header else {})


def _member_matches_status_token(member_row: dict, token: str) -> bool:
    """Pure — does any day of a built member row match the SPA status token
    (OnTime / Late / Early / Overtime / Missing / Leave)? Mirrors the FE
    ``metaMatchesStatus`` semantics in ``useTeamMatrix.js`` — overlapping by
    design (a Late day with OT matches both tokens)."""
    t = (token or "").strip()
    if not t:
        return True
    for day in member_row.get("days") or []:
        status = str(day.get("status") or "")
        late = int(day.get("late_minutes") or 0)
        early = int(day.get("early_leave_minutes") or 0)
        ot = float(day.get("approved_overtime_hours") or 0)
        if t == "OnTime":
            if status == "Present" and late <= 0:
                return True
        elif t == "Late":
            if late > 0 or status == "Late":
                return True
        elif t == "Early":
            if early > 0:
                return True
        elif t == "Overtime":
            if ot > 0:
                return True
        elif t == "Missing":
            if status in ("Not marked", "Absent"):
                return True
        elif t == "Leave":
            if status in ("On Leave", "Half Day"):
                return True
    return False


@frappe.whitelist()
def team_attendance(
    manager: str = "",
    from_date: str = "",
    to_date: str = "",
    search: str = "",
    status_filter: str = "",
    shift_type: str = "",
    page: int = 1,
    page_size: int = 60,
) -> dict:
    """Plan §10.2 / §13 — manager's team attendance across a date range.

    Returns one member row carrying per-day presence plus a period summary.
    Honours Frappe role permissions (HR / Line Manager); reads ``Employee`` and
    ``Attendance`` only. Defaults to the current month when no range is given.

    Desk-free enhancements (plans/plan-team-attendance-desk-free.md §4 WP2):
    every day-cell additionally carries ``locked`` / ``work_session`` /
    ``open_checkout_miss`` / a server-driven ``can`` matrix; every member row
    carries ``pending`` badges; ``locked_dates`` + ``period`` describe the
    monthly-attendance-period lock state. ``search`` / ``shift_type`` /
    ``status_filter`` / ``page`` / ``page_size`` are optional server-side
    filters (backward compatible — the legacy response keys are unchanged).
    """
    frappe.only_for(["HR Manager", "HR User", "System Manager", "Line Manager"])
    # HR Manager / System Manager oversee the whole company, so they see every
    # active employee — independent of the (often empty) ``reports_to`` link.
    # Line Manager / HR User still only see their direct reports. This keeps the
    # team grid consistent with the per-employee monthly view, which never
    # depends on ``reports_to``.
    caller_roles = set(frappe.get_roles())
    is_company_wide = bool(caller_roles & {"HR Manager", "System Manager"})
    # Capability-matrix role facts (plan §4 WP2). ``is_hr`` mirrors the manage
    # roles of ``admin._require_attendance_editor_for``; OT approval follows
    # ``attendance_admin_ops.OT_APPROVER_ROLES`` (HR Manager / System Manager).
    is_hr = bool(caller_roles & {"HR Manager", "System Manager", "HR User"})
    is_ot_approver = bool(caller_roles & {"HR Manager", "System Manager"})
    # Non-company-wide viewers (Line Manager / HR User) only ever see their own
    # reports → every roster member is "lm_of" by construction.
    lm_of_roster = not is_company_wide

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
    today_portal = tz_utils.now_in_portal().date()
    if from_date and to_date:
        start = getdate(from_date)
        end = getdate(to_date)
    else:
        start = today_portal.replace(day=1)
        end = today_portal
    # Window clamp (plan WP2): a range grid must stay bounded — week views that
    # roll into the next month stay covered while absurd windows are rejected.
    if (end - start).days > TEAM_ATTENDANCE_MAX_DAYS:
        frappe.throw(
            _("Khoảng thời gian tối đa là {0} ngày.").format(TEAM_ATTENDANCE_MAX_DAYS),
            frappe.ValidationError,
        )

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

    # Desk-free filter (plan WP2): free-text search over the roster identity.
    q = (search or "").strip().lower()
    if q:
        members = [
            m
            for m in members
            if q in str(m.get("employee_name") or "").lower()
            or q in str(m.get("name") or "").lower()
            or q in str(m.get("designation") or "").lower()
        ]

    # Server-driven badges + lock context (plan WP2). All batched — one query
    # per source, never per member.
    emp_names = [m["name"] for m in members]
    pending_counts = _team_pending_counts(emp_names) if emp_names else {}
    checkout_miss_by = _team_checkout_miss_by(emp_names) if emp_names else {}
    locked_dates, period_info = _locked_days_between(start, end)

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

    shift_window_cache: dict[str, tuple] = {
        n: (m.get("start_time"), m.get("end_time")) for n, m in shift_meta.items()
    }

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

    # BATCHED window reads (plan WP2 — kills the per-member N+1): one
    # Attendance query + one Work-Session query for the whole roster, grouped
    # in Python. The day derivation below is byte-identical to the legacy loop.
    att_by_emp: dict[str, dict] = {}
    if members:
        for r in frappe.db.get_all(
            "Attendance",
            filters={
                "employee": ["in", emp_names],
                "attendance_date": ["between", [start, end]],
                "docstatus": 1,
            },
            fields=[
                "employee",
                "attendance_date",
                "status",
                "shift",
                "in_time",
                "out_time",
                "late_entry",
                "early_exit",
            ],
            order_by="attendance_date asc",
        ):
            att_by_emp.setdefault(r.employee, {})[str(r.attendance_date)] = r
    ws_by_emp: dict[str, dict] = {}
    if members:
        for r in frappe.db.get_all(
            "VN Attendance Work Session",
            filters={"employee": ["in", emp_names], "work_date": ["between", [start, end]]},
            fields=[
                "name",
                "employee",
                "work_date",
                "actual_checkin",
                "actual_checkout",
                "late_minutes",
                "early_leave_minutes",
                "approved_overtime_hours",
                "raw_overtime_hours",
                "missing_checkout",
                "vn_auto_checkout",
            ],
        ):
            ws_by_emp.setdefault(r.employee, {})[str(r.work_date)] = r

    for m in members:
        by_date = att_by_emp.get(m["name"], {})
        # Work Session is the portal's source of truth for the PAIRED IN/OUT:
        # calc.py matches punches to the shift's planned window, so overnight
        # checkouts land on the correct (start-day) row. The core Attendance
        # in_time/out_time is frequently scrambled for overnight shifts (previous
        # night's OUT, or a 1-day offset), so prefer the Work Session for the
        # displayed times + late/early and only fall back to Attendance below.
        ws_map = ws_by_emp.get(m["name"], {})
        days = []
        cur = start
        while cur <= end:
            att = by_date.get(str(cur))
            ws = ws_map.get(str(cur))
            # Server-driven cell context (plan WP2): lock flag, engine refs and
            # the capability matrix for THIS (member × day) cell.
            cur_locked = str(cur) in locked_dates
            cm_row = checkout_miss_by.get(m["name"])
            cm_open_today = bool(cm_row and str(getattr(cm_row, "work_date", "") or "") == str(cur))
            cell_can = _team_att_cell_can(
                is_hr=is_hr,
                is_lm_of=lm_of_roster,
                locked=cur_locked,
                is_future=cur > today_portal,
                has_punch=bool(ws and (ws.actual_checkin or ws.actual_checkout)),
                raw_ot=flt(getattr(ws, "raw_overtime_hours", 0) or 0, 4) if ws else 0.0,
                open_cm=cm_open_today,
                is_ot_approver=is_ot_approver,
            )
            cell_extras = {
                "locked": cur_locked,
                "work_session": getattr(ws, "name", None) if ws else None,
                "open_checkout_miss": cm_row.get("name") if cm_open_today else None,
                "can": cell_can,
            }
            if not att and not ws:
                days.append(
                    {
                        "work_date": str(cur),
                        "status": "Not marked",
                        "checkin_time": None,
                        "checkout_time": None,
                        "late_minutes": 0,
                        "early_leave_minutes": 0,
                        **cell_extras,
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
                        **cell_extras,
                    }
                )
                summary["on_leave"] += 1
                cur += timedelta(days=1)
                continue

            # Work Session is the source of truth; Attendance is fallback for
            # days where the engine hasn't run yet. Handle att=None gracefully.
            disp_status = att.status if att else "Present"
            late_min = 0
            early_min = 0
            ot_hours = 0.0
            raw_ot = 0.0
            checkin_time = att.in_time if att else None
            checkout_time = att.out_time if att else None
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
                # PHASE-1 FRAME: Attendance in/out are naive WALL — fold the
                # planned window to wall so the comparisons stay same-frame.
                ps_local, pe_local = tz_utils.planned_window(cur, shift_start, shift_end)
                ps_local = tz_utils.wall(ps_local)
                pe_local = tz_utils.wall(pe_local)
                if att.in_time:
                    in_local = tz_utils.wall(get_datetime(att.in_time))
                    if ps_local <= in_local <= pe_local:
                        if att.late_entry:
                            disp_status = "Late"
                        late_min = max(0, int((in_local - ps_local).total_seconds() // 60))
                    else:
                        checkin_time = None
                if att.out_time:
                    out_local = tz_utils.wall(get_datetime(att.out_time))
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
                    # `missing_checkout` = "session has no OUT yet" — calc.py
                    # sets it for EVERY session lacking an OUT, including one
                    # that is MID-SHIFT right now. The team grid therefore must
                    # NOT read it as "forgot to check out"; it decides via the
                    # shift-end window. `vn_auto_checkout` is the actual
                    # engine-synthesised fake-OUT marker (auto-close) — the UI
                    # masks that OUT time as '--:--' and renders orange.
                    "missing_checkout": bool(ws and ws.missing_checkout),
                    "vn_auto_checkout": bool(ws and ws.vn_auto_checkout),
                    **cell_extras,
                }
            )
            # Summary chips: count by the DERIVED late/early/OT values (not just
            # the raw Attendance flags) so the totals match what the cells show.
            # Work-Session-derived late/early are not reflected in att.late_entry,
            # so the minute thresholds are authoritative here.
            att_status = att.status if att else disp_status
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
        pc = pending_counts.get(m["name"], {}) or {}
        pending_badges = {
            "corrections": int(pc.get("corrections") or 0),
            "overtime": int(pc.get("overtime") or 0),
            "leaves": int(pc.get("leaves") or 0),
            "checkout_misses": 1 if checkout_miss_by.get(m["name"]) else 0,
        }
        out = {**m, "days": days, "shift_type": member_shift[m["name"]], "pending": pending_badges}
        out_members.append(out)

    # Post-build filters (plan WP2): shift group + derived status token over
    # the built day set, then member paging — ``summary`` above stays computed
    # on the FULL filtered set (DNA §6.6 A parity).
    if shift_type:
        out_members = [o for o in out_members if o.get("shift_type") == shift_type]
    if status_filter:
        token = status_filter.strip()
        out_members = [o for o in out_members if _member_matches_status_token(o, token)]
    try:
        page_i = max(1, int(page or 1))
    except (TypeError, ValueError):
        page_i = 1
    try:
        size = int(page_size or 60)
    except (TypeError, ValueError):
        size = 60
    size = max(1, min(size, 200))
    total_members = len(out_members)
    out_members = out_members[(page_i - 1) * size : page_i * size]
    grouped = {sn: [] for sn in shift_names}
    for o in out_members:
        grouped.setdefault(o["shift_type"], []).append(o)

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
        # Desk-free additions (plan WP2) — additive, legacy keys untouched.
        "locked_dates": sorted(locked_dates),
        "period": period_info,
        "page": page_i,
        "page_size": size,
        "total_members": total_members,
    }


@frappe.whitelist()
def team_attendance_context(manager: str = "", from_date: str = "", to_date: str = "") -> dict:
    """plans/plan-team-attendance-desk-free.md §4 WP1 — viewer context for the
    ``/hr/team/attendance`` toolbar: scope, capability matrix, period-lock
    state, pending-approval counts and filter options. One round-trip before
    the grid loads; the FE renders chrome (lock banner, buttons) from this
    payload only — BE là nguồn sự thật duy nhất.
    """
    frappe.only_for(["HR Manager", "HR User", "System Manager", "Line Manager"])
    caller_roles = set(frappe.get_roles())
    is_company_wide = bool(caller_roles & {"HR Manager", "System Manager"})
    is_hr = bool(caller_roles & {"HR Manager", "System Manager", "HR User"})
    is_lm = bool(caller_roles & {"Line Manager"})
    is_ot_approver = bool(caller_roles & {"HR Manager", "System Manager"})

    # IDOR-safe viewer resolution — identical semantics to team_attendance().
    if manager and is_company_wide:
        manager_emp = emp_utils.emp_name(manager)
    else:
        manager_emp = emp_utils.get_employee_for_user()
    if manager_emp and not frappe.db.exists("Employee", manager_emp):
        resolved = emp_utils.get_employee_for_user(manager_emp)
        manager_emp = resolved or emp_utils.get_employee_for_user() or manager_emp

    today_portal = tz_utils.now_in_portal().date()
    start = getdate(from_date) if from_date else today_portal.replace(day=1)
    end = getdate(to_date) if to_date else today_portal
    if end < start:
        start, end = end, start

    # Roster in scope — the same filters the grid itself applies.
    if is_company_wide:
        member_filters = {"status": "Active"}
        company = (
            frappe.db.get_value("Employee", manager_emp, "company")
            if manager_emp and frappe.db.exists("Employee", manager_emp)
            else None
        )
        if company:
            member_filters["company"] = company
        members = frappe.db.get_all("Employee", filters=member_filters, fields=["name"])
        if manager_emp:
            members = [m for m in members if m.get("name") != manager_emp]
        scope_mode = "company"
    else:
        members = frappe.db.get_all(
            "Employee",
            filters={"status": "Active", "reports_to": manager_emp},
            fields=["name"],
        )
        scope_mode = "team"
    emps = [m.get("name") for m in members]

    # Pending approvals within the viewer's scope (plan WP1).
    pending_counts = _team_pending_counts(emps) if emps else {}
    checkout_miss_by = _team_checkout_miss_by(emps) if emps else {}
    pending_approvals = {
        "corrections": sum(int(v.get("corrections") or 0) for v in pending_counts.values()),
        "overtime": sum(int(v.get("overtime") or 0) for v in pending_counts.values()),
        "leaves": sum(int(v.get("leaves") or 0) for v in pending_counts.values()),
        "checkout_misses": len(checkout_miss_by),
    }

    locked_dates, period_info = _locked_days_between(start, end)

    # Filter options for the gear popover (shift types + departments).
    shift_types = sorted(
        str(r.get("name") or "") for r in frappe.db.get_all("Shift Type", fields=["name"]) if r.get("name")
    )
    departments = sorted(
        {
            str(r.get("department") or "").strip()
            for r in frappe.db.get_all("Employee", filters={"status": "Active"}, fields=["department"])
            if str(r.get("department") or "").strip()
        }
    )

    can = {
        "view_grid": True,
        "fix_punch": bool(is_hr or is_lm),
        "delete_punch": bool(is_hr or is_lm),
        "mark_attendance": bool(is_hr or is_lm),
        "create_request": bool(is_hr or is_lm),
        "approve_ot": is_ot_approver,
        "resolve_checkout_miss": is_hr,
        "recalc": is_hr,
        "generate": is_hr,
        "nudge": True,
        "export": True,
        "manage_period": bool(caller_roles & {"HR Manager", "System Manager"}),
    }
    return {
        "viewer_employee": manager_emp,
        "scope": {"mode": scope_mode, "member_count": len(emps)},
        "can": can,
        "period": period_info,
        "locked_dates": sorted(locked_dates),
        "pending_approvals": pending_approvals,
        "filters": {"shift_types": shift_types, "departments": departments},
        "window": {
            "from_date": str(start),
            "to_date": str(end),
            "max_days": TEAM_ATTENDANCE_MAX_DAYS,
        },
    }


def _publish_team_attendance(employee: str | None = None, work_date=None) -> None:
    """Best-effort realtime ping (plan WP9) — open ``/hr/team/attendance`` tabs
    refetch the affected member row (event ``gege_hr:team_attendance_updated``).
    NEVER raises into the mutation flow. Pattern: ``_publish_team_today``."""
    try:
        frappe.publish_realtime(
            "gege_hr:team_attendance_updated",
            {"employee": employee, "work_date": str(work_date or "")},
        )
    except Exception:
        pass


@frappe.whitelist()
def team_attendance_export_csv(
    manager: str = "",
    from_date: str = "",
    to_date: str = "",
    search: str = "",
    status_filter: str = "",
    shift_type: str = "",
) -> dict:
    """Plan WP9 — CSV (UTF-8 BOM) of the CURRENT grid view. Export == grid:
    the endpoint walks ``team_attendance`` itself (single source of truth),
    so the file can never drift from what the manager sees. Gate + scope ride
    the grid endpoint's own checks (HR/LM only)."""
    res = team_attendance(
        manager=manager,
        from_date=from_date,
        to_date=to_date,
        search=search,
        status_filter=status_filter,
        shift_type=shift_type,
        page=1,
        page_size=200,
    )
    header = [
        "Mã NV",
        "Tên",
        "Ca",
        "Ngày",
        "Trạng thái",
        "Giờ vào",
        "Giờ ra",
        "Muộn (phút)",
        "Về sớm (phút)",
        "OT duyệt (h)",
        "Khoá",
    ]
    lines = [",".join(header)]
    for g in res["groups"]:
        for m in g["members"]:
            for d in m.get("days", []):
                lines.append(
                    ",".join(
                        _csv_cell(c)
                        for c in (
                            m.get("name"),
                            m.get("employee_name"),
                            g.get("shift_type"),
                            d.get("work_date"),
                            d.get("status"),
                            d.get("checkin_time") or "",
                            d.get("checkout_time") or "",
                            d.get("late_minutes"),
                            d.get("early_leave_minutes"),
                            d.get("approved_overtime_hours") or 0,
                            "Locked" if d.get("locked") else "",
                        )
                    )
                )
    month = str(res.get("from_date") or "")[:7] or "export"
    return {
        "filename": f"team-attendance-{month}.csv",
        "csv": "\ufeff" + "\n".join(lines),
        "rows": len(lines) - 1,
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
    cols = (
        [c for c in _WORK_SESSION_SEARCH_FIELDS if c in valid]
        if valid
        else [
            "name",
            "employee",
            "employee_name",
            "shift_type",
        ]
    )
    _like = f"%{pagination.escape_like(q)}%"
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
        _like = f"%{pagination.escape_like(_q)}%"
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
    materialise_skipped = 0
    if backfill:
        mat = shift_api._materialise_shift_instances(from_date=start, to_date=end, employee=employee)
        instances_created = mat.get("created", 0)
        materialise_skipped = mat.get("skipped", 0)

    si_filters = {"work_date": ["between", [start, end]], "docstatus": 1}
    if employee:
        si_filters["employee"] = employee
    shift_instances = frappe.db.get_all("VN Employee Shift Instance", filters=si_filters, pluck="name")

    sessions_recalculated = 0
    errors: list[str] = []  # WP3 (MH2): per-SI failures surface to the UI
    for si_name in shift_instances:
        # Skip Locked sessions (CAS guard in persist_work_session).
        status = frappe.db.get_value(
            "VN Attendance Work Session", {"shift_instance": si_name}, "calculation_status"
        )
        if status == "Locked":
            continue
        try:
            if calc.persist_work_session(si_name, calculate_mode="batch"):
                sessions_recalculated += 1
        except Exception:
            errors.append(si_name)
            frappe.log_error(frappe.get_traceback(), f"Work Session recalc failed for {si_name}")
            try:
                frappe.db.rollback()  # don't poison the remaining batch
            except Exception:
                pass

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
        "materialise_skipped": materialise_skipped,
        "sessions_recalculated": sessions_recalculated,
        "exceptions_resolved": exceptions_resolved,
        "errors": errors,  # WP3: SI names that failed — UI links to exceptions
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
            frappe.db.set_value("VN Attendance Work Session", ws_name, {"absent": 1, "payable_day": 0})
        except Exception:
            continue
    # WP4: heartbeat ONLY after a full successful pass (HC4).
    try:
        from gege_hr.gege_hr.utils import health as _health

        _health.record_heartbeat(
            "attendance.auto_mark_absent_job",
            summary={"instances": len(instances)},
        )
    except Exception:
        pass


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
    """HR/Manager may read anyone; a plain Employee only their own row.

    WP5 (plans/plan-team-attendance-desk-free.md): a ``Line Manager`` may also
    create/read on behalf of their OWN ``reports_to`` members — the inline
    "Tạo giải trình" quick-action in the team-attendance drawer. Cross-team
    on-behalf stays refused.
    """
    if _is_hr_manager():
        return
    own = emp_utils.get_employee_for_user()
    target = emp_utils.emp_name(employee)
    if own == target:
        return
    roles = set(emp_utils.get_user_roles() or [])
    if roles & {"Line Manager"}:
        try:
            reports_to = frappe.db.get_value("Employee", target, "reports_to")
        except Exception:
            reports_to = None
        if reports_to and reports_to == own:
            return
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
        # WP5: the ownership gate here (HR ∪ LM-of-own ∪ self) is stricter than
        # the generic ``_resolve_employee`` pinning — resolve plainly afterwards
        # so a Line Manager may file on behalf of their own report.
        _assert_own_correction(employee)
        emp = emp_utils.emp_name(employee)
    else:
        emp = _resolve_employee(None)

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
            val = kwargs.get(opt)
            if opt == "attachment":
                val = str(val)
                # Only files uploaded through Frappe (/files/...).
                if not val.startswith(("/files/", "/private/files/")):
                    frappe.throw(_("Tệp đính kèm không hợp lệ."), frappe.ValidationError)
            doc.set(opt, val)

    doc.insert()
    # Move into the approval pipeline (Draft → Pending Manager). Best-effort:
    # stays Draft if the workflow isn't seeded yet.
    send_for_approval(doc)
    # Realtime ping for open /hr/correction tabs (plans/correction-deskfree §3.4).
    # Best-effort — never fails the submit over a publish/import error.
    try:
        from gege_hr.gege_hr.api.correction import _publish_correction

        _publish_correction(doc)
    except Exception:
        pass
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
    # Realtime ping for open /hr/correction tabs (plans/correction-deskfree §3.4).
    # Best-effort — never fails the cancel over a publish/import error.
    try:
        from gege_hr.gege_hr.api.correction import _publish_correction

        _publish_correction(doc)
    except Exception:
        pass
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
def get_monthly_period_list(company=None, year=None, status=None, search=None):
    """FE contract → :func:`attendance_period.periods`.

    ``search`` (plan-lock-desk-free BE-4) performs the server-side broad
    LIKE so the SPA search box can hit the DB directly (HR-BL-10).
    """
    return _ap.periods(
        company=company or None,
        status=status or None,
        year=year or None,
        search=search or None,
    )


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


@frappe.whitelist()
def confirm_monthly_line(name):
    """FE contract → :func:`attendance_period.confirm_line` (BE-5)."""
    return _ap.confirm_line(name)


@frappe.whitelist()
def confirm_all_monthly_lines(period):
    """FE contract → :func:`attendance_period.confirm_all_lines` (BE-5)."""
    return _ap.confirm_all_lines(period)


@frappe.whitelist()
def adjust_monthly_line(name, values=None, reason=None):
    """FE contract → :func:`attendance_period.adjust_line` (BE-5).

    ``values`` may arrive as a JSON string (form-encoded POST) — the core
    endpoint parses both.
    """
    return _ap.adjust_line(name, values=values, reason=reason)


@frappe.whitelist()
def delete_monthly_period(name):
    """FE contract → :func:`attendance_period.delete_period` (BE-5, Draft only)."""
    return _ap.delete_period(name)


@frappe.whitelist()
def get_lock_logs(period):
    """FE contract → :func:`attendance_period.lock_logs` (BE-5)."""
    return _ap.lock_logs(period)
