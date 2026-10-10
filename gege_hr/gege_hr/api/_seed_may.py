"""One-off helper: clean June test attendance + seed realistic May attendance
for the test employee ``nhanvien@gegeteam.xyz``.

NOT shipped product code — a data tool invoked via::

    bench --site <site> execute gege_hr.gege_hr.api._seed_may.discover
    bench --site <site> execute gege_hr.gege_hr.api._seed_may.run
    bench --site <site> execute gege_hr.gege_hr.api._seed_may.verify

It is intentionally defensive: every step prints what it did so the operator
can verify on both the employee and the manager screens.

Design notes (why only ``Attendance`` is created, not ``Employee Checkin``):
  * The employee monthly view (``my_logs``) and the manager team view
    (``team_attendance``) read exclusively from ``Attendance`` (docstatus=1).
    ``today_status`` reads raw ``Employee Checkin`` but only for *today*, so it
    is irrelevant for a historical month.
  * Inserting ``Employee Checkin`` rows would fire ``on_employee_checkin_create``
    → work-session recalc / "Unmatched Checkin" exceptions, adding noise without
    any UI benefit for May. We therefore seed the authoritative ``Attendance``
    rows, which is exactly what both screens render.
  * ``in_time``/``out_time`` are written via raw SQL as *naive local* datetimes
    so the SPA's ``formatTime()`` and ``my_logs``' planned-window math render the
    same wall-clock the employee actually worked (no UTC skew).
"""

from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta

import frappe
from frappe.utils import add_days

EMAIL = "nhanvien@gegeteam.xyz"
COMPANY = "Gege Demo"
SHIFT = "Ca Hành Chính"
WORK_LOCATION = "128"

# Work location coords supplied by the operator (Gege Demo office, HCMC).
WORK_LAT = 10.79
WORK_LON = 106.65
# Small jitter so every punch isn't pixel-identical — stays well inside the
# 70 m geofence (~0.00018 deg ≈ 20 m).
LAT_JITTER = 0.00018
LON_JITTER = 0.00018

SHIFT_START_MIN = 8 * 60  # 08:00
SHIFT_END_MIN = 17 * 60  # 17:00
LUNCH_HOURS = 1.0


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #
def portal_meta() -> dict:
    """Print the VN HR Portal Setting fields so we know what to expose in UI."""
    meta = frappe.get_meta("VN HR Portal Setting")
    fields = [
        {"fieldname": f.fieldname, "label": f.label, "fieldtype": f.fieldtype, "options": f.options}
        for f in meta.fields
        if f.fieldname
    ]
    print(json.dumps(fields, ensure_ascii=False, indent=2))
    return fields


def test_save_portal() -> dict:
    """End-to-end smoke for save_portal_setting: toggle, read, revert."""
    from gege_hr.gege_hr.api import admin

    before = admin.get_portal_setting().get("require_selfie")
    res = admin.save_portal_setting(require_selfie=1)
    after = admin.get_portal_setting().get("require_selfie")
    # revert
    admin.save_portal_setting(require_selfie=before or 0)
    out = {"before": before, "saved": res.get("saved"), "changes": res.get("changes"), "after": after}
    print(json.dumps(out, default=str, ensure_ascii=False, indent=2))
    return out


def chk() -> dict:
    """Final sanity snapshot (June empty, May count, SA intact, geofence)."""
    emp = frappe.db.get_value("Employee", {"user_id": EMAIL, "status": "Active"})
    out = {
        "employee": emp,
        "may_attendance": frappe.db.count(
            "Attendance",
            {"employee": emp, "attendance_date": ["between", [date(2026, 5, 1), date(2026, 5, 31)]]},
        ),
        "june_attendance": frappe.db.count(
            "Attendance",
            {"employee": emp, "attendance_date": ["between", [date(2026, 6, 1), date(2026, 6, 30)]]},
        ),
        "shift_assignments": frappe.db.get_all(
            "Shift Assignment",
            {"employee": emp},
            ["name", "shift_type", "status", "docstatus", "start_date", "end_date"],
        ),
        "default_work_location": frappe.db.get_value("Employee", emp, "default_work_location"),
    }
    from gege_hr.gege_hr.api import attendance as a

    out["resolved_work_location_may"] = a._work_location_for(emp, date(2026, 5, 4))
    print(json.dumps(out, default=str, ensure_ascii=False, indent=2))
    return out


def discover() -> dict:
    out: dict = {}
    emp = frappe.db.get_value("Employee", {"user_id": EMAIL, "status": "Active"})
    out["employee"] = emp
    if emp:
        out["default_work_location"] = frappe.db.get_value("Employee", emp, "default_work_location")
        out["reports_to"] = frappe.db.get_value("Employee", emp, "reports_to")
        out["company"] = frappe.db.get_value("Employee", emp, "company")
    out["vn_work_locations"] = frappe.db.get_all(
        "VN Work Location",
        ["name", "location_name", "latitude", "longitude", "allowed_radius_meters"],
    )
    out["shift_types"] = frappe.db.get_all("Shift Type", ["name", "start_time", "end_time"])
    if emp:
        out["shift_assignments"] = frappe.db.get_all(
            "Shift Assignment",
            {"employee": emp},
            ["name", "shift_type", "status", "docstatus", "start_date", "end_date", "vn_work_location"],
        )
    s = frappe.get_single("VN HR Portal Setting")
    out["portal_setting"] = {
        "require_geolocation": s.get("require_geolocation"),
        "default_work_location": s.get("default_work_location"),
    }
    if emp:
        out["checkin_count"] = frappe.db.count("Employee Checkin", {"employee": emp})
        out["attendance_count"] = frappe.db.count("Attendance", {"employee": emp})
        out["attendance_status_breakdown"] = {
            st: frappe.db.count("Attendance", {"employee": emp, "status": st})
            for st in ["Present", "Absent", "On Leave", "Half Day", "Work From Home"]
        }
    print(json.dumps(out, default=str, ensure_ascii=False, indent=2))
    return out


# --------------------------------------------------------------------------- #
# Step 1 — purge June test data
# --------------------------------------------------------------------------- #
def _purge_period(emp: str, start: date, end: date, stats: dict | None = None) -> dict:
    """Delete all attendance artefacts for ``emp`` within [start, end]."""
    stats = stats if stats is not None else {}

    atts = frappe.db.get_all(
        "Attendance",
        {"employee": emp, "attendance_date": ["between", [start, end]]},
        ["name", "docstatus"],
    )
    for a in atts:
        try:
            doc = frappe.get_doc("Attendance", a.name)
            if doc.docstatus == 1:
                doc.cancel()
            doc.delete(ignore_permissions=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  attendance delete skip {a.name}: {exc}")
    stats["attendance_deleted"] = len(atts)

    checkins = frappe.db.get_all(
        "Employee Checkin",
        {"employee": emp, "time": ["between", [start, add_days(end, 1)]]},
        ["name"],
    )
    for c in checkins:
        try:
            frappe.delete_doc("Employee Checkin", c.name, ignore_permissions=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  checkin delete skip {c.name}: {exc}")
    stats["checkin_deleted"] = len(checkins)

    for dt, date_field in [
        ("VN Attendance Raw Log", "log_time"),
        ("VN Attendance Exception", "work_date"),
    ]:
        if not frappe.db.table_exists(dt) or not frappe.get_meta(dt).has_field(date_field):
            continue
        rows = frappe.db.get_all(dt, {"employee": emp, date_field: ["between", [start, end]]}, ["name"])
        for r in rows:
            try:
                frappe.delete_doc(dt, r.name, ignore_permissions=True)
            except Exception:  # noqa: BLE001
                pass
        stats[f"{dt}_deleted"] = len(rows)

    frappe.db.commit()
    return stats


# --------------------------------------------------------------------------- #
# Step 2 — configure employee + May shift assignment + geofence
# --------------------------------------------------------------------------- #
def _configure(emp: str) -> dict:
    out: dict = {}

    if frappe.get_meta("Employee").has_field("default_work_location"):
        frappe.db.set_value("Employee", emp, "default_work_location", WORK_LOCATION)
        out["employee_default_work_location"] = WORK_LOCATION

    # Portal-wide default too, so every flow resolves the geofence consistently.
    s = frappe.get_doc("VN HR Portal Setting")
    if frappe.get_meta("VN HR Portal Setting").has_field("default_work_location"):
        s.db_set("default_work_location", WORK_LOCATION)
        out["portal_default_work_location"] = WORK_LOCATION
    if frappe.get_meta("VN HR Portal Setting").has_field("require_geolocation"):
        s.db_set("require_geolocation", 1)
        out["require_geolocation"] = 1

    # A submitted, Active May-only assignment (no overlap with the ongoing one
    # that starts 2026-06-24) so team_attendance includes the employee for May.
    may_existing = frappe.db.get_value(
        "Shift Assignment",
        {"employee": emp, "shift_type": SHIFT, "status": "Active", "start_date": date(2026, 5, 1)},
    )
    if not may_existing:
        sa = frappe.get_doc(
            {
                "doctype": "Shift Assignment",
                "employee": emp,
                "shift_type": SHIFT,
                "company": COMPANY,
                "start_date": "2026-05-01",
                "end_date": "2026-05-31",
                "status": "Active",
            }
        )
        if frappe.get_meta("Shift Assignment").has_field("vn_work_location"):
            sa.vn_work_location = WORK_LOCATION
        sa.insert(ignore_permissions=True)
        sa.submit()
        out["shift_assignment"] = sa.name
    else:
        out["shift_assignment"] = f"exists:{may_existing}"

    frappe.db.commit()
    return out


# --------------------------------------------------------------------------- #
# Step 3 — realistic May schedule
# --------------------------------------------------------------------------- #
def _may_schedule() -> list[tuple[date, dict]]:
    """Deterministic, realistic per-day plan for May 2026.

    Returns ``(date, plan)`` where plan has ``kind`` in
    {work, leave, half, absent, off} plus punch minutes (local, from midnight).
    """
    specials = {
        date(2026, 5, 1): {"kind": "off", "reason": "Labour Day"},
        date(2026, 5, 7): {"kind": "leave", "leave_type": "Sick Leave"},
        date(2026, 5, 12): {"kind": "absent"},
        date(2026, 5, 14): {
            "kind": "half",
            "leave_type": "Casual Leave",
            "in_min": 8 * 60,
            "out_min": 12 * 60,
        },
        date(2026, 5, 18): {"kind": "work", "in_min": 7 * 60 + 55, "out_min": None, "miss_out": True},
        date(2026, 5, 21): {"kind": "leave", "leave_type": "Casual Leave"},
    }

    rng = random.Random(20260501)
    days: list[tuple[date, dict]] = []
    d = date(2026, 5, 1)
    while d <= date(2026, 5, 31):
        if d.weekday() >= 5:  # Sat / Sun
            days.append((d, {"kind": "off"}))
        elif d in specials:
            days.append((d, specials[d]))
        else:
            r = rng.random()
            in_offset = rng.choice([-9, -6, -3, 0, 1, 3, 6, 9, 14, 20, 28, 35])
            in_min = SHIFT_START_MIN + in_offset
            late = in_min > SHIFT_START_MIN + 5
            if r < 0.13:  # overtime evening
                out_min = SHIFT_END_MIN + rng.choice([55, 70, 95, 125])
            elif r < 0.23:  # early leave
                out_min = rng.choice([16 * 60 + 12, 16 * 60 + 28, 16 * 60 + 44])
            else:
                out_min = SHIFT_END_MIN + rng.choice([-3, 2, 6, 12, 18, 25])
            early = out_min < SHIFT_END_MIN - 5
            days.append(
                (d, {"kind": "work", "in_min": in_min, "out_min": out_min, "late": late, "early": early})
            )
        d += timedelta(days=1)
    return days


def _min_to_local(d: date, minutes: int | None) -> str | None:
    if minutes is None:
        return None
    hh, mm = divmod(int(minutes), 60)
    return f"{d.isoformat()} {hh:02d}:{mm:02d}:00"


def _generate(emp: str) -> dict:
    rng = random.Random(20260501)
    created = []
    for d, plan in _may_schedule():
        kind = plan["kind"]
        if kind == "off":
            continue

        in_str = _min_to_local(d, plan.get("in_min")) if kind in ("work", "half") else None
        out_str = _min_to_local(d, plan.get("out_min")) if kind in ("work", "half") else None
        if kind == "work" and not plan.get("miss_out"):
            out_str = out_str  # may be None only for miss_out
        if kind == "work" and plan.get("miss_out"):
            out_str = None

        status = {
            "work": "Present",
            "half": "Half Day",
            "leave": "On Leave",
            "absent": "Absent",
        }[kind]

        # working hours (lunch deducted for full days, half lunch for half days).
        wh = 0.0
        if in_str and out_str:
            in_dt = datetime.strptime(in_str, "%Y-%m-%d %H:%M:%S")
            out_dt = datetime.strptime(out_str, "%Y-%m-%d %H:%M:%S")
            wh = (out_dt - in_dt).total_seconds() / 3600.0
            wh -= LUNCH_HOURS if kind == "work" else 0.5

        doc = frappe.get_doc(
            {
                "doctype": "Attendance",
                "employee": emp,
                "employee_name": frappe.db.get_value("Employee", emp, "employee_name"),
                "attendance_date": d.isoformat(),
                "status": status,
                "shift": SHIFT,
                "company": COMPANY,
                "in_time": in_str,
                "out_time": out_str,
                "working_hours": round(wh, 2) if wh > 0 else None,
                "late_entry": 1 if plan.get("late") else 0,
                "early_exit": 1 if plan.get("early") else 0,
            }
        )
        if kind in ("leave", "half") and plan.get("leave_type"):
            if frappe.get_meta("Attendance").has_field("leave_type"):
                doc.leave_type = plan["leave_type"]
        doc.flags.ignore_permissions = True
        doc.insert()
        doc.submit()
        created.append(doc.name)

    frappe.db.commit()

    # Force naive-local datetimes (bypass Frappe tz conversion) so the SPA and
    # my_logs render the exact wall-clock the employee worked.
    for d, plan in _may_schedule():
        if plan["kind"] not in ("work", "half"):
            continue
        in_min = plan.get("in_min")
        out_min = plan.get("out_min")
        if plan["kind"] == "work" and plan.get("miss_out"):
            out_min = None
        name = frappe.db.get_value("Attendance", {"employee": emp, "attendance_date": d})
        if not name:
            continue
        in_str = _min_to_local(d, in_min)
        out_str = _min_to_local(d, out_min)
        wh = 0.0
        if in_str and out_str:
            in_dt = datetime.strptime(in_str, "%Y-%m-%d %H:%M:%S")
            out_dt = datetime.strptime(out_str, "%Y-%m-%d %H:%M:%S")
            wh = (out_dt - in_dt).total_seconds() / 3600.0 - (LUNCH_HOURS if plan["kind"] == "work" else 0.5)
        frappe.db.sql(
            "UPDATE `tabAttendance` SET in_time=%s, out_time=%s, working_hours=%s, "
            "late_entry=%s, early_exit=%s, shift=%s WHERE name=%s",
            (
                in_str,
                out_str,
                round(wh, 2) if wh > 0 else 0.0,
                1 if plan.get("late") else 0,
                1 if plan.get("early") else 0,
                SHIFT,
                name,
            ),
        )
    frappe.db.commit()

    summary = {
        "attendance_created": len(created),
        "by_status": {
            st: frappe.db.count(
                "Attendance",
                {
                    "employee": emp,
                    "status": st,
                    "attendance_date": ["between", [date(2026, 5, 1), date(2026, 5, 31)]],
                },
            )
            for st in ["Present", "Absent", "On Leave", "Half Day"]
        },
    }
    print("generate summary:", summary)
    _ = rng  # reserved for future per-punch jitter
    return {"created": created, "summary": summary}


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def run() -> dict:
    emp = frappe.db.get_value("Employee", {"user_id": EMAIL, "status": "Active"})
    if not emp:
        frappe.throw(f"No Active Employee for {EMAIL}")
    purge: dict = {}
    # Wipe both the June test data and any prior May seed so the run is idempotent.
    _purge_period(emp, date(2026, 5, 1), date(2026, 5, 31), purge)
    _purge_period(emp, date(2026, 6, 1), date(2026, 6, 30), purge)
    cfg = _configure(emp)
    gen = _generate(emp)
    out = {"employee": emp, "purge": purge, "config": cfg, "generate": gen}
    print(json.dumps(out, default=str, ensure_ascii=False, indent=2))
    return out


def verify() -> dict:
    """Call the same API the UI uses and print a sample so we can eyeball sync."""
    from gege_hr.gege_hr.api import attendance as att_api

    emp = frappe.db.get_value("Employee", {"user_id": EMAIL, "status": "Active"})
    out: dict = {}

    # Employee monthly view source.
    logs = att_api.my_logs(emp, "2026-05-01", "2026-05-31")
    out["my_logs_count"] = len(logs)
    out["my_logs_sample"] = [
        {
            "date": r["work_date"],
            "status": r["status"],
            "in": r["actual_checkin"],
            "out": r["actual_checkout"],
            "late": r["late_minutes"],
            "early": r["early_leave_minutes"],
            "hours": r["regular_hours"],
            "shift": r["shift_type"],
        }
        for r in sorted(logs, key=lambda x: x["work_date"])
    ]
    out["my_monthly_summary"] = att_api.my_monthly_summary(emp, 2026, 5)["summary"]

    # Manager team view source (HR Manager is company-wide).
    team = att_api.team_attendance("", "2026-05-01", "2026-05-31")
    mine = next((m for m in team.get("members", []) if m.get("name") == emp), None)
    out["team_includes_employee"] = mine is not None
    if mine:
        out["team_employee_days"] = sorted(
            [
                {
                    "date": dd.get("work_date") or dd.get("date"),
                    "status": dd.get("status"),
                    "in": dd.get("actual_checkin") or dd.get("in_time"),
                    "out": dd.get("actual_checkout") or dd.get("out_time"),
                }
                for dd in mine.get("days", [])
            ],
            key=lambda x: x["date"] or "",
        )
    print(json.dumps(out, default=str, ensure_ascii=False, indent=2))
    return out
