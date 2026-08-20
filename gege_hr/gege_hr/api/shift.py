"""
Shift API — plan v5 §10.3.

Milestone-2 materialises VN Employee Shift Instance rows from Shift Assignment
(``generate_daily_shift_instances``) and enqueues work-session recalculation
on submit (``on_shift_instance_submit``). The read-only ``my_schedule`` /
``shift_type_options`` endpoints drive the frontend Schedule view.
"""

from __future__ import annotations

from datetime import date, timedelta

import frappe
from frappe import _
from frappe.utils import add_days, getdate

from gege_hr.gege_hr.utils import employee as emp_utils
from gege_hr.gege_hr.utils import tz as tz_utils

# ---------------------------------------------------------------------------
# Shift Type validate hook — keep Frappe-native auto-attendance windows in sync
# with the gege_hr custom fields. The portal UI edits ``vn_*`` custom fields
# (e.g. vn_max_checkout_after_end_minutes = 360) but Frappe's NATIVE auto-
# attendance reads its own fields (allow_check_out_after_shift_end_time), which
# previously stayed at the default 60 → overnight checkouts ~70 min after the
# shift end were dropped / mis-paired. Mirroring the values on every save makes
# native Attendance pairing agree with the Work-Session engine.
# ---------------------------------------------------------------------------
_NATIVE_FROM_CUSTOM = {
    "allow_check_out_after_shift_end_time": "vn_max_checkout_after_end_minutes",
    "begin_check_in_before_shift_start_time": "vn_earliest_checkin_minutes",
}


def sync_native_shift_windows(doc, method: str | None = None) -> None:
    """``Shift Type.validate`` hook — mirror gege_hr custom windows → native."""
    for native, custom in _NATIVE_FROM_CUSTOM.items():
        custom_val = getattr(doc, custom, None)
        if custom_val is None:
            continue
        try:
            setattr(doc, native, custom_val)
        except Exception:
            # Field may be absent on a stripped meta — never block the save.
            pass


@frappe.whitelist()
def my_schedule(
    employee: str | None = None, from_date: str | None = None, to_date: str | None = None
) -> list[dict]:
    """Plan §10.3 — the employee's shift instances across a window."""
    # The SPA may pass the whole Employee object as ``employee``; reduce it to
    # its name string before using it as a filter value (see utils.employee.emp_name).
    employee_name = emp_utils.emp_name(employee) if employee else None
    emp = employee_name or emp_utils.get_employee_for_user()
    if not emp:
        frappe.throw(_("Tài khoản này chưa được liên kết với nhân viên."), frappe.PermissionError)
    today = tz_utils.now_in_portal().date()
    start = getdate(from_date) if from_date else today - timedelta(days=7)
    end = getdate(to_date) if to_date else today + timedelta(days=7)

    # Milestone-1 derives the schedule from Shift Assignment (no Shift Instance
    # DocType yet). Each active assignment expands into per-day planned windows.
    out: list[dict] = []
    has_loc_field = frappe.get_meta("Shift Assignment").has_field("vn_work_location")
    sa_fields = ["name", "shift_type", "start_date", "end_date"]
    if has_loc_field:
        sa_fields.append("vn_work_location")
    assignments = frappe.db.get_all(
        "Shift Assignment",
        filters={"employee": emp, "status": "Active", "docstatus": 1, "start_date": ["<=", end]},
        fields=sa_fields,
    )
    work_location_names = {
        a.get("vn_work_location")
        for a in assignments
        if a.get("vn_work_location")
    }
    loc_label_by_name = {
        r["name"]: r["location_name"]
        for r in frappe.db.get_all(
            "VN Work Location",
            filters={"name": ["in", list(work_location_names)] or [""]},
            fields=["name", "location_name"],
        )
    } if work_location_names else {}
    day = start
    while day <= end:
        for a in assignments:
            a_start = getdate(a.start_date)
            a_end = getdate(a.end_date) if a.end_date else end
            if a_start <= day <= a_end:
                st = frappe.get_cached_doc("Shift Type", a.shift_type)
                planned_start, planned_end = tz_utils.planned_window(day, st.start_time, st.end_time)
                wl = a.get("vn_work_location") or None
                out.append(
                    {
                        "work_date": day.isoformat(),
                        "shift_type": a.shift_type,
                        "start_time": str(st.start_time),
                        "end_time": str(st.end_time),
                        "is_overnight": tz_utils.is_overnight(st.start_time, st.end_time),
                        # PHASE-1 FRAME: naive wall ISO (no Z) for the SPA.
                        "planned_start": tz_utils.wall(planned_start).isoformat(),
                        "planned_end": tz_utils.wall(planned_end).isoformat(),
                        "shift_assignment": a.name,
                        "work_location": wl,
                        "work_location_name": loc_label_by_name.get(wl) if wl else None,
                    }
                )
        day += timedelta(days=1)
    return out


@frappe.whitelist()
def shift_type_options() -> list[dict]:
    """Plan §10.3 — active shift types for selectors."""
    rows = frappe.db.get_all(
        "Shift Type",
        filters={"disabled": 0},
        fields=["name", "start_time", "end_time"],
        order_by="name",
    )
    return [
        {
            "value": r.name,
            "label": r.name,
            "description": f"{r.start_time}–{r.end_time}",
        }
        for r in rows
    ]


@frappe.whitelist()
def team_schedule(from_date: str | None = None, to_date: str | None = None) -> list[dict]:
    """Plan §10.3 — manager team schedule window."""
    frappe.only_for(["HR Manager", "HR User", "System Manager"])
    manager_emp = emp_utils.get_employee_for_user()
    today = tz_utils.now_in_portal().date()
    start = getdate(from_date) if from_date else today
    end = getdate(to_date) if to_date else today + timedelta(days=6)
    members = frappe.db.get_all(
        "Employee", filters={"status": "Active", "reports_to": manager_emp}, fields=["name", "employee_name"]
    )
    result = []
    for m in members:
        sched = my_schedule(employee=m.name, from_date=start.isoformat(), to_date=end.isoformat())
        result.append({**m, "shifts": sched})
    return result


# --------------------------------------------------------------------------- #
# Hooks — M2: materialise Shift Instances + trigger recalculation
# --------------------------------------------------------------------------- #
SHIFT_INSTANCE_HORIZON_DAYS = 14  # forward window for daily materialisation


@frappe.whitelist()
def generate_shift_instances(days: int | None = None) -> dict:
    """Manually-triggerable generator (HR UI button)."""
    frappe.only_for(["HR Manager", "HR User", "System Manager"])
    horizon = int(days or SHIFT_INSTANCE_HORIZON_DAYS)
    res = _materialise_shift_instances(horizon)
    # WP3: skip-count surfaced so one bad assignment is VISIBLE, not silent.
    return {"ok": True, "created": res["created"], "skipped": res["skipped"]}


@frappe.whitelist()
def backfill_shift_instances(
    from_date: str | None = None,
    to_date: str | None = None,
    employee: str | None = None,
) -> dict:
    """Materialise VN Employee Shift Instance rows for an arbitrary date window.

    Unlike the daily generator (which only looks forward from ``today``), this
    backfills **past** dates so that historical Employee Checkins can be matched
    to a Shift Instance and aggregated into Work Sessions. Idempotent: existing
    instance rows are skipped. Used by the ``/hr/attendance`` hotfix + the
    "Tính lại kỳ" recalculation flow.
    """
    frappe.only_for(["HR Manager", "HR User", "System Manager"])
    today = tz_utils.now_in_portal().date()
    win_start = getdate(from_date) if from_date else today
    win_end = getdate(to_date) if to_date else today
    if win_end < win_start:
        win_start, win_end = win_end, win_start
    res = _materialise_shift_instances(
        from_date=win_start, to_date=win_end, employee=employee
    )
    return {
        "ok": True,
        "from_date": win_start.isoformat(),
        "to_date": win_end.isoformat(),
        "employee": employee,
        "created": res["created"],
        "skipped": res["skipped"],
        "errors": res["errors"],
    }


def generate_daily_shift_instances(*args, **kwargs) -> int:
    """Daily scheduler → materialise VN Employee Shift Instance rows.

    Expands every active Shift Assignment into one VN Employee Shift Instance
    per calendar (portal) day for the configured horizon, computing planned
    windows + half-split + check-in/out windows via ``utils/tz``.
    Idempotent: existing instance rows are skipped.
    """
    horizon = SHIFT_INSTANCE_HORIZON_DAYS
    res = _materialise_shift_instances(horizon)
    # WP4: heartbeat only when the full pass succeeded (failures inside are
    # logged per-assignment by the WP3 guard; the job still "ran").
    try:
        from gege_hr.gege_hr.utils import health as _health

        _health.record_heartbeat(
            "shift.generate_daily_shift_instances",
            summary={"created": res.get("created"), "skipped": res.get("skipped")},
        )
    except Exception:
        pass
    return res


def _materialise_shift_instances(
    horizon: int = SHIFT_INSTANCE_HORIZON_DAYS,
    from_date=None,
    to_date=None,
    employee: str | None = None,
) -> dict:
    """Expand active Shift Assignments into per-day VN Employee Shift Instances.

    WP3 (F-LC17): ONE broken assignment (duplicate window, validation error,
    corrupt Shift Type…) must never abort the WHOLE company's materialisation.
    Each assignment is wrapped: a failure is logged (title
    ``materialise SI failed <employee>``), rolled back, counted as ``skipped``,
    and the loop continues. Returns ``{"created": n, "skipped": m,
    "errors": [<employee>...]}``.
    """
    today = tz_utils.now_in_portal().date()
    if from_date or to_date:
        # Explicit backfill window (may cover past dates).
        win_start = getdate(from_date) if from_date else today
        win_end = getdate(to_date) if to_date else add_days(today, horizon)
    else:
        win_start = today
        win_end = add_days(today, horizon)
    has_loc_field = frappe.get_meta("Shift Assignment").has_field("vn_work_location")
    sa_fields = ["name", "employee", "shift_type", "start_date", "end_date", "company"]
    if has_loc_field:
        sa_fields.append("vn_work_location")
    sa_filters = {"status": "Active", "docstatus": 1, "start_date": ["<=", win_end]}
    if employee:
        sa_filters["employee"] = employee
    assignments = frappe.db.get_all(
        "Shift Assignment",
        filters=sa_filters,
        fields=sa_fields,
    )
    created = 0
    skipped = 0
    errors: list[str] = []
    for a in assignments:
        try:
            a_start = getdate(a.start_date)
            a_end = getdate(a.end_date) if a.end_date else win_end
            day = max(a_start, win_start)
            while day <= min(a_end, win_end):
                if _ensure_shift_instance(a, day):
                    created += 1
                day = add_days(day, 1)
        except Exception:
            # WP3 guard: roll back THIS assignment's partial writes, log, and
            # keep going — other employees still get their instances (MH1).
            skipped += 1
            errors.append(a.get("employee") or a.get("name") or "?")
            try:
                frappe.log_error(
                    title=f"materialise SI failed {a.get('employee')}",
                    message=frappe.get_traceback(),
                )
            except Exception:
                pass
            try:
                frappe.db.rollback()
            except Exception:
                pass
    return {"created": created, "skipped": skipped, "errors": errors}


def _ensure_shift_instance(assignment: dict, day: date) -> bool:
    """Create the VN Employee Shift Instance for one day if not already present."""
    exists = frappe.db.exists(
        "VN Employee Shift Instance",
        {
            "employee": assignment.employee,
            "shift_type": assignment.shift_type,
            "work_date": day,
            "docstatus": ["!=", 2],
        },
    )
    if exists:
        return False

    st = frappe.get_cached_doc("Shift Type", assignment.shift_type)
    if not st.start_time or not st.end_time:
        return False

    planned_start, planned_end = tz_utils.planned_window(day, st.start_time, st.end_time)
    overnight = tz_utils.is_overnight(st.start_time, st.end_time)
    half_span = (planned_end - planned_start) / 2
    first_half_end = planned_start + half_span
    second_half_start = planned_start + half_span

    # Check-in / out windows from VN Shift Type custom fields (fall back to defaults).
    earliest_in = planned_start - timedelta(minutes=_st_int(st, "vn_earliest_checkin_minutes", 60))
    latest_in = planned_start + timedelta(minutes=_st_int(st, "vn_latest_checkin_minutes", 30))
    earliest_out = planned_end - timedelta(minutes=_st_int(st, "vn_earliest_checkout_minutes", 30))
    latest_out = planned_end + timedelta(minutes=_st_int(st, "vn_latest_checkout_minutes", 60))
    max_checkout = planned_end + timedelta(minutes=_st_int(st, "vn_max_checkout_after_end_minutes", 360))

    employee_name = frappe.db.get_value("Employee", assignment.employee, "employee_name")
    policy = frappe.db.get_value("Employee", assignment.employee, "default_attendance_policy")
    work_loc = assignment.get("vn_work_location") or None
    work_loc_name = None
    if work_loc:
        work_loc_name = frappe.db.get_value("VN Work Location", work_loc, "location_name")

    doc = frappe.get_doc(
        {
            "doctype": "VN Employee Shift Instance",
            "employee": assignment.employee,
            "employee_name": employee_name,
            "work_date": day,
            "shift_type": assignment.shift_type,
            "shift_name": assignment.shift_type,
            "source_shift_assignment": assignment.name,
            "attendance_policy": policy,
            "work_location": work_loc,
            "work_location_name": work_loc_name,
            "company": assignment.company,
            "status": "Scheduled",
            "planned_start": _frappe_dt(planned_start),
            "planned_end": _frappe_dt(planned_end),
            "is_overnight": int(overnight),
            "first_half_start": _frappe_dt(planned_start),
            "first_half_end": _frappe_dt(first_half_end),
            "second_half_start": _frappe_dt(second_half_start),
            "second_half_end": _frappe_dt(planned_end),
            "checkin_window_start": _frappe_dt(earliest_in),
            "checkin_window_end": _frappe_dt(latest_in),
            "checkout_window_start": _frappe_dt(earliest_out),
            "checkout_window_end": _frappe_dt(latest_out),
            "max_checkout_time": _frappe_dt(max_checkout),
        }
    )
    # Scheduler-driven generation (daily job) — no interactive user session, so
    # the system creates + submits the shift instance directly.
    doc.insert(ignore_permissions=True)
    try:
        doc.submit()
    except Exception:
        # Submit permissions may be missing in some test benches; leave as draft.
        pass
    return True


def _st_int(shift_type_doc, field: str, default: int) -> int:
    val = getattr(shift_type_doc, field, None)
    try:
        return int(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def _frappe_dt(dt) -> str:
    """PHASE-1 FRAME: format an aware datetime as naive PORTAL WALL storage string."""
    return tz_utils.wall(dt).strftime("%Y-%m-%d %H:%M:%S")


def on_shift_instance_submit(doc, method: str | None = None) -> None:
    """VN Employee Shift Instance on_submit → enqueue work-session recalculation."""
    try:
        frappe.enqueue(
            "gege_hr.gege_hr.utils.calc.persist_work_session",
            queue="short",
            timeout=60,
            shift_instance_name=doc.name,
            calculate_mode="batch",
        )
    except Exception:
        # Enqueue not available in some contexts (tests) → compute inline.
        from gege_hr.gege_hr.utils import calc

        calc.persist_work_session(doc.name, calculate_mode="batch")
