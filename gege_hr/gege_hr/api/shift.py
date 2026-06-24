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
    assignments = frappe.db.get_all(
        "Shift Assignment",
        filters={"employee": emp, "status": "Active", "docstatus": 1, "start_date": ["<=", end]},
        fields=["name", "shift_type", "start_date", "end_date"],
    )
    day = start
    while day <= end:
        for a in assignments:
            a_start = getdate(a.start_date)
            a_end = getdate(a.end_date) if a.end_date else end
            if a_start <= day <= a_end:
                st = frappe.get_cached_doc("Shift Type", a.shift_type)
                planned_start, planned_end = tz_utils.planned_window(day, st.start_time, st.end_time)
                out.append(
                    {
                        "work_date": day.isoformat(),
                        "shift_type": a.shift_type,
                        "start_time": str(st.start_time),
                        "end_time": str(st.end_time),
                        "is_overnight": tz_utils.is_overnight(st.start_time, st.end_time),
                        "planned_start": tz_utils.utc_iso(planned_start),
                        "planned_end": tz_utils.utc_iso(planned_end),
                        "shift_assignment": a.name,
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
    created = _materialise_shift_instances(horizon)
    return {"ok": True, "created": created}


def generate_daily_shift_instances(*args, **kwargs) -> int:
    """Daily scheduler → materialise VN Employee Shift Instance rows.

    Expands every active Shift Assignment into one VN Employee Shift Instance
    per calendar (portal) day for the configured horizon, computing planned
    windows + half-split + check-in/out windows via ``utils/tz``.
    Idempotent: existing instance rows are skipped.
    """
    horizon = SHIFT_INSTANCE_HORIZON_DAYS
    return _materialise_shift_instances(horizon)


def _materialise_shift_instances(horizon: int) -> int:
    today = tz_utils.now_in_portal().date()
    end = add_days(today, horizon)
    assignments = frappe.db.get_all(
        "Shift Assignment",
        filters={"status": "Active", "docstatus": 1, "start_date": ["<=", end]},
        fields=["name", "employee", "shift_type", "start_date", "end_date", "company"],
    )
    created = 0
    for a in assignments:
        a_start = getdate(a.start_date)
        a_end = getdate(a.end_date) if a.end_date else end
        day = max(a_start, today)
        while day <= min(a_end, end):
            if _ensure_shift_instance(a, day):
                created += 1
            day = add_days(day, 1)
    return created


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
    """Format an aware datetime as Frappe's "YYYY-MM-DD HH:MM:SS" (UTC storage)."""
    return dt.astimezone(tz_utils.ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")


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
