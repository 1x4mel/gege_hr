"""Seed random-but-deterministic June-2026 Attendance for HR-EMP-00003.

Run with::

    bench --site <site> execute gege_hr.gege_hr.seed_june2026.run

What it does
------------
1. Ensures an Employee exists for the manager ``1x4mel@gmail.com`` (Gege Demo)
   and links ``HR-EMP-00003.reports_to`` to it + sets its default shift — so the
   manager ``team_attendance`` view resolves the team member.
2. Wipes any existing June-2026 Attendance rows for the employee (idempotent).
3. Creates one submitted (docstatus=1) ``Attendance`` per *weekday* of June 2026
   (22 days) with a fixed, realistic mix:

       present_normal  x12   08:02 -> 17:18
       present_late    x 3   08:31 -> 17:25   (late_entry = 1, "đi muộn")
       present_early   x 2   07:58 -> 16:12   (early_exit = 1, "về sớm")
       halfday         x 1   08:05 -> 12:10   (Half Day)
       leave           x 1                      (On Leave)
       absent          x 3                      (Absent)

   Expected ``my_monthly_summary`` (June 2026):
       present=17  absent=3  on_leave=1  half_day=1
       worked_days=17.5   late_days=3   early_exit_days=2

All editable computed-ish fields (status / in_time / out_time / late_entry /
early_exit / working_hours) are forced via ``db.set_value`` *after* submission
so HRMS attendance validation cannot silently override them — the portal
endpoints read raw DB columns, so this is what the UI renders.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import frappe

EMP = "HR-EMP-00003"
SHIFT = "Ca Hành Chính"
COMPANY = "Gege Demo"
MANAGER_USER = "1x4mel@gmail.com"

YEAR, MONTH = 2026, 6


def _weekdays(y: int, m: int) -> list[date]:
    days: list[date] = []
    d = date(y, m, 1)
    while d.month == m:
        if d.weekday() < 5:  # Mon-Fri
            days.append(d)
        d += timedelta(days=1)
    return days


def _ensure_manager_employee() -> str:
    """Create (idempotent) the manager Employee for 1x4mel@gmail.com."""
    me = frappe.db.get_value("Employee", {"user_id": MANAGER_USER}, "name")
    if me:
        return me
    doc = frappe.get_doc(
        {
            "doctype": "Employee",
            "naming_series": "HR-EMP-",
            "first_name": "Quản Lý",
            "last_name": "Test",
            "employee_name": "Quản Lý Test",
            "company": COMPANY,
            "status": "Active",
            "user_id": MANAGER_USER,
            "gender": "Male",
            "date_of_birth": "1990-01-01",
            "date_of_joining": "2024-01-01",
        }
    )
    doc.insert(ignore_permissions=True)
    return doc.name


def run() -> dict:
    frappe.flags.in_patch = True

    # 1. manager linkage
    manager_emp = _ensure_manager_employee()
    frappe.db.set_value("Employee", EMP, "reports_to", manager_emp, update_modified=False)
    frappe.db.set_value("Employee", EMP, "default_shift", SHIFT, update_modified=False)
    # joining date must pre-date June 2026 or Attendance validation rejects it
    frappe.db.set_value("Employee", EMP, "date_of_joining", "2024-01-01", update_modified=False)

    # 2. wipe existing June-2026 attendance
    start = date(YEAR, MONTH, 1)
    end = date(YEAR, MONTH, 30)
    old = frappe.db.get_all(
        "Attendance",
        {"employee": EMP, "attendance_date": ["between", [start, end]]},
        pluck="name",
    )
    wiped = 0
    for name in old:
        try:
            frappe.delete_doc("Attendance", name, force=True, ignore_permissions=True)
            wiped += 1
        except Exception as exc:  # noqa: BLE001
            frappe.log_error(f"delete attendance {name}: {exc}")

    # 3. fixed distribution
    plan = (
        ["present_normal"] * 12
        + ["present_late"] * 3
        + ["present_early"] * 2
        + ["halfday"] * 1
        + ["leave"] * 1
        + ["absent"] * 3
    )
    rnd = random.Random(20260601)
    days = _weekdays(YEAR, MONTH)
    if len(plan) != len(days):
        raise RuntimeError(f"plan length {len(plan)} != weekdays {len(days)}")
    rnd.shuffle(plan)

    created = []
    for day, kind in zip(days, plan, strict=True):
        # insert as Absent (always validates), submit, then force real values.
        doc = frappe.get_doc(
            {
                "doctype": "Attendance",
                "employee": EMP,
                "attendance_date": day.isoformat(),
                "company": COMPANY,
                "shift": SHIFT,
                "status": "Absent",
            }
        )
        doc.insert(ignore_permissions=True)
        doc.submit()

        if kind == "present_normal":
            status, in_t, out_t, late, early, wh = "Present", "08:02:00", "17:18:00", 0, 0, 9.27
        elif kind == "present_late":
            status, in_t, out_t, late, early, wh = "Present", "08:31:00", "17:25:00", 1, 0, 8.90
        elif kind == "present_early":
            status, in_t, out_t, late, early, wh = "Present", "07:58:00", "16:12:00", 0, 1, 8.23
        elif kind == "halfday":
            status, in_t, out_t, late, early, wh = "Half Day", "08:05:00", "12:10:00", 0, 0, 4.08
        elif kind == "leave":
            status, in_t, out_t, late, early, wh = "On Leave", None, None, 0, 0, 0.0
        else:  # absent
            status, in_t, out_t, late, early, wh = "Absent", None, None, 0, 0, 0.0

        in_dt = f"{day} {in_t}" if in_t else None
        out_dt = f"{day} {out_t}" if out_t else None
        frappe.db.set_value(
            "Attendance",
            doc.name,
            {
                "status": status,
                "in_time": in_dt,
                "out_time": out_dt,
                "late_entry": late,
                "early_exit": early,
                "working_hours": wh,
            },
            update_modified=False,
        )
        created.append({"day": str(day), "kind": kind, "name": doc.name, "status": status})

    frappe.db.commit()
    return {"manager_emp": manager_emp, "wiped": wiped, "created": created}
