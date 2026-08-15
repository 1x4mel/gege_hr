"""Auto-checkout for forgotten checkouts — Chính sách A (benefit-of-the-doubt).

When an employee checks in but never checks out (especially on overnight shifts:
IN 20h day-D, should OUT 08h day-D+1), the old per-day parity logic treated the
next check-in as a fresh IN, dropping the whole shift's hours. This module:

  1. Finds work sessions that have an actual_checkin but no actual_checkout and
     are past ``planned_end + buffer``.
  2. Synthesises an ``OUT`` Employee Checkin at ``planned_end`` (marked
     ``vn_auto_generated=1``) so the payroll pairing counts regular hours up to
     the shift end — **but no overtime** (the pair ends at the planned end).
  3. Raises a ``VN Checkout Miss`` ticket (occurrence-tracked, penalty-escalated,
     grace-windowed) that the employee must explain and HR must review.

OT for a genuinely late departure is only credited later, via an approved
Attendance Correction Request — so auto-close never invents OT (anti-abuse).
All ops are idempotent (guarded on ``vn_auto_checkout`` / existing ticket).
"""
from __future__ import annotations

import datetime as dt
from datetime import timedelta

try:  # bench-free safe import (unit tests run outside a Frappe site)
    import frappe  # type: ignore
except Exception:  # pragma: no cover
    frappe = None  # type: ignore

try:
    from frappe.utils import get_datetime, now_datetime
except Exception:  # pragma: no cover
    get_datetime = None  # type: ignore
    now_datetime = None  # type: ignore


WORK_SESSION_DOCTYPE = "VN Attendance Work Session"
CHECKIN_DOCTYPE = "Employee Checkin"
MISS_DOCTYPE = "VN Checkout Miss"
SETTING_DOCTYPE = "VN HR Portal Setting"

# Public so other modules (api.payroll settings read, api.checkout_miss) use the
# SAME defaults the engine falls back to (BUG-6 fix: engine vs UI mismatch).
DEFAULTS = {
    "enabled": 1,
    "grace_hours": 24,
    "free_first_n": 2,
    "penalty_amount": 100000.0,
    "window_days": 90,
    "buffer_minutes": 360,
}

_DEFAULTS = DEFAULTS


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def _config() -> dict:
    """Read checkout-miss settings from VN HR Portal Setting (graceful default).

    NOTE: ``or`` is intentionally NOT used for the values, because ``0`` is a
    valid value (disabled flag, no freebies, zero penalty) and ``0 or default``
    would wrongly fall back to the default. ``None`` (field unset) falls back.
    """
    if frappe is None:
        return dict(_DEFAULTS)
    raw = {}
    try:
        fields = [
            "vn_cm_enabled",
            "vn_cm_grace_hours",
            "vn_cm_free_first_n",
            "vn_cm_penalty_amount",
            "vn_cm_window_days",
            "vn_cm_buffer_minutes",
        ]
        for f in fields:
            raw[f] = frappe.db.get_single_value(SETTING_DOCTYPE, f)
    except Exception:
        return dict(_DEFAULTS)

    def _i(key, field, cast=int):
        v = raw.get(field)
        return cast(v) if v is not None else _DEFAULTS[key]

    return {
        "enabled": _i("enabled", "vn_cm_enabled"),
        "grace_hours": _i("grace_hours", "vn_cm_grace_hours"),
        "free_first_n": _i("free_first_n", "vn_cm_free_first_n"),
        "penalty_amount": _i("penalty_amount", "vn_cm_penalty_amount", float),
        "window_days": _i("window_days", "vn_cm_window_days"),
        "buffer_minutes": _i("buffer_minutes", "vn_cm_buffer_minutes"),
    }


# --------------------------------------------------------------------------- #
# Occurrence counting
# --------------------------------------------------------------------------- #
def _occurrence_no(employee: str, window_days: int) -> int:
    """1-based occurrence number for this employee within ``window_days``.

    BUG-8 fix: count by ``work_date`` (consistent with the attendance window,
    not ticket creation time) and exclude cancelled tickets (docstatus 2) so a
    voided ticket doesn't escalate the next occurrence.
    """
    if frappe is None:
        return 1
    try:
        since = (now_datetime() - timedelta(days=int(window_days or 90))).date()
        # M5: waived tickets don't escalate — counting them made a single
        # waived miss push the NEXT one straight into the penalty bracket.
        count = frappe.db.count(
            MISS_DOCTYPE,
            {
                "employee": employee,
                "docstatus": ["<", 2],
                "work_date": [">=", since],
                "penalty_waived": 0,
            },
        )
        return int(count or 0) + 1
    except Exception:
        return 1


# --------------------------------------------------------------------------- #
# Find open sessions
# --------------------------------------------------------------------------- #
def _is_missing_checkout(value) -> bool:
    """True when ``actual_checkout`` is effectively absent.

    The Work-Session column is ``NOT NULL DEFAULT 0``, so a missing checkout may
    be stored as NULL (via ``frappe.db.set_value(None)``), the numeric ``0``, the
    zero datetime ``"0000-00-00 00:00:00"``, or an empty string — depending on
    how the row was written. Any of those counts as "no checkout yet".
    """
    if value is None:
        return True
    if isinstance(value, (int, float)):
        return value == 0
    s = str(value).strip()
    return s in ("", "0", "0000-00-00 00:00:00")


def _find_open_sessions(employee: str, now_utc, buffer_minutes: int) -> list[dict]:
    """Work sessions with an IN but no OUT, past ``planned_end + buffer``.

    Uses raw SQL so the NOT-NULL-DEFAULT-0 ``actual_checkout`` column (NULL, 0,
    or zero-datetime) is compared in SQL — avoiding pymysql deserialization
    errors on ``0000-00-00`` that would break ``frappe.db.get_all``.
    """
    if frappe is None:
        return []
    if not isinstance(now_utc, dt.datetime):
        now_utc = get_datetime(now_utc)
    cutoff = (now_utc - timedelta(minutes=int(buffer_minutes or 30))).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    try:
        rows = frappe.db.sql(
            """
            SELECT name, work_date, planned_start, planned_end,
                   shift_type, shift_instance, actual_checkin,
                   first_checkin_log, company
            FROM `tabVN Attendance Work Session`
            WHERE employee = %s
              AND docstatus < 2
              AND vn_auto_checkout = 0
              AND actual_checkin IS NOT NULL AND actual_checkin != ''
              AND planned_end < %s
              AND (actual_checkout IS NULL
                   OR actual_checkout = 0
                   OR actual_checkout = '0000-00-00 00:00:00'
                   OR actual_checkout = '')
            ORDER BY planned_end ASC
            LIMIT 50
            """,
            (employee, cutoff),
            as_dict=True,
        )
    except Exception:
        frappe.log_error(title="checkout_miss._find_open_sessions failed")
        return []
    return rows or []


# --------------------------------------------------------------------------- #
# Core: close one open session + raise a ticket
# --------------------------------------------------------------------------- #
def _close_session(session: dict, cfg: dict) -> str | None:
    """Synthesise the OUT log + ticket for one open session. Returns miss name."""
    if frappe is None:
        return None

    # H3 race guard: mobile_checkin and the hourly scheduler can both pick the
    # same open session. Claim it FIRST with a guarded UPDATE on
    # vn_auto_checkout (0 → 1): only the winner proceeds to create the OUT log
    # and ticket; the loser sees 0 rows and skips (previously both passed the
    # exists() check → duplicate OUT logs + duplicate penalty tickets).
    if not session.get("name"):
        return None
    from gege_hr.gege_hr.utils._db import guarded_update

    claimed = guarded_update(
        f"UPDATE `{WORK_SESSION_DOCTYPE}` SET vn_auto_checkout = 1"
        " WHERE name = %(name)s AND vn_auto_checkout = 0",
        {"name": session["name"]},
    )
    if not claimed:
        return None
    # Idempotency: a ticket already references this session → skip.
    if frappe.db.exists(MISS_DOCTYPE, {"shift_instance": session.get("shift_instance")}):
        return None

    checkout_at = get_datetime(session["planned_end"])
    employee = frappe.db.get_value(WORK_SESSION_DOCTYPE, session["name"], "employee")

    # 1. Synthesise the OUT checkin at planned_end.
    out_log = frappe.get_doc(
        {
            "doctype": CHECKIN_DOCTYPE,
            "employee": employee,
            "employee_name": frappe.db.get_value(
                WORK_SESSION_DOCTYPE, session["name"], "employee_name"
            ),
            "time": checkout_at,
            "log_type": "OUT",
            "vn_source_type": "Auto",
            "vn_auto_generated": 1,
        }
    )
    out_log.insert(ignore_permissions=True)

    # 2. Occurrence + penalty escalation.
    occ = _occurrence_no(employee, cfg["window_days"])
    free_n = int(cfg.get("free_first_n", 2))
    penalty = 0.0 if occ <= free_n else float(cfg.get("penalty_amount", 0.0))

    # 3. Raise the ticket.
    miss = frappe.get_doc(
        {
            "doctype": MISS_DOCTYPE,
            "employee": employee,
            "work_date": session.get("work_date"),
            "shift_type": session.get("shift_type"),
            "shift_instance": session.get("shift_instance"),
            "company": session.get("company"),
            "auto_checkin": session.get("first_checkin_log"),
            "auto_checkout": out_log.name,
            "auto_checkout_at": checkout_at,
            "occurrence_no": occ,
            "status": "Pending",
            "penalty_amount": penalty,
            "grace_deadline": now_datetime()
            + timedelta(hours=int(cfg.get("grace_hours", 24))),
        }
    )
    miss.insert(ignore_permissions=True)

    # 4. Mark the work session closed + link the ticket (vn_auto_checkout was
    # already flipped to 1 by the claim above — here we stamp the rest).
    frappe.db.set_value(
        WORK_SESSION_DOCTYPE,
        session["name"],
        {
            "actual_checkout": checkout_at,
            "last_checkout_log": out_log.name,
            "vn_checkout_miss": miss.name,
        },
        update_modified=False,
    )
    out_log.db_set("vn_checkout_miss", miss.name)
    # Realtime notify the employee (best-effort; never fail the close on it).
    try:
        user = frappe.db.get_value("Employee", employee, "user_id")
        if user:
            frappe.publish_realtime(
                "checkout_miss_created",
                {"ticket": miss.name, "work_date": str(session.get("work_date"))},
                user=user,
            )
    except Exception:
        pass
    return miss.name


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def auto_close_missed_checkouts(employee: str, now=None) -> list[str]:
    """Close every still-open prior session for ``employee`` and raise tickets.

    Called from ``mobile_checkin`` (on each check-in) and the daily scheduler.
    Returns the list of created ``VN Checkout Miss`` names (empty if none / off).
    Idempotent: a session already auto-closed or ticketed is skipped.
    """
    if frappe is None:
        return []
    cfg = _config()
    if not int(cfg.get("enabled", 1)):
        return []
    now_utc = get_datetime(now) if now else now_datetime()
    created: list[str] = []
    for session in _find_open_sessions(employee, now_utc, cfg["buffer_minutes"]):
        try:
            miss_name = _close_session(session, cfg)
            if miss_name:
                created.append(miss_name)
        except Exception:
            frappe.log_error(title=f"checkout_miss._close_session {session.get('name')}")
    if created:
        try:
            frappe.db.commit()
        except Exception:
            pass
    return created


def run_hourly() -> dict:
    """Hourly scheduler entry: auto-close forgotten checkouts for every active
    employee (handles those who don't return for days) + flip expired Pending
    tickets to Penalised. Returns a small summary for the scheduler log.
    """
    if frappe is None:
        return {"closed": 0, "penalised": 0}
    cfg = _config()
    if not int(cfg.get("enabled", 1)):
        return {"closed": 0, "penalised": 0, "disabled": True}
    closed_total = 0
    try:
        employees = frappe.db.get_all(
            "Employee", filters={"status": "Active"}, pluck="name"
        )
    except Exception:
        employees = []
    for emp in employees:
        try:
            closed_total += len(auto_close_missed_checkouts(emp))
        except Exception:
            frappe.log_error(title=f"checkout_miss.run_hourly {emp}")
    penalised = penalise_expired()
    return {"closed": closed_total, "penalised": penalised}


def penalise_expired(now=None) -> int:
    """Flip ``Pending`` tickets past their grace deadline → ``Penalised``.

    Run from the scheduler. Returns the count flipped.
    """
    if frappe is None:
        return 0
    now_utc = get_datetime(now) if now else now_datetime()
    try:
        names = (
            frappe.db.get_all(
                MISS_DOCTYPE,
                filters={"status": "Pending", "grace_deadline": ["<", now_utc]},
                pluck="name",
            )
            or []
        )
    except Exception:
        return 0
    # Proper Frappe flow: load + save() so validate + on_update run (the doctype's
    # own hooks/side-effects fire, e.g. recompute). This runs in a scheduler context
    # as Administrator (full perms) — no ignore_permissions / raw db.set_value bypass.
    flipped = 0
    for name in names:
        try:
            doc = frappe.get_doc(MISS_DOCTYPE, name)
            doc.status = "Penalised"
            doc.save()
            flipped += 1
        except frappe.LinkValidationError:
            # RUNTIME BUG (9.5k Error Log rows on the bench): tickets whose
            # Shift Instance / auto-checkout logs were purged by seed scripts
            # can NEVER pass doc.save() (link validation) — the scheduler
            # retried them every hour forever. Flip the status directly so
            # the ticket leaves the Pending set.
            try:
                frappe.db.set_value(MISS_DOCTYPE, name, "status", "Penalised", update_modified=False)
                flipped += 1
            except Exception:
                frappe.log_error(title="checkout_miss penalise (dead-link) failed", message=name)
        except Exception:
            frappe.log_error(title="checkout_miss penalise failed", message=name)
    # S6 fix: report the number actually flipped, not len(names) — a failed
    # save() (logged above) must not inflate the scheduler summary.
    return flipped
