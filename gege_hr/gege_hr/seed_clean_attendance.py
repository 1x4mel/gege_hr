"""Seed CLEAN attendance data (delete + recreate) — covers every calendar state.

Implements ``plans/Seed-Clean-Attendance.md``. Wipes dirty check-in/work-session
data for a date window (all or selected employees) and rebuilds a realistic,
clean dataset across the **4 shifts**, exercising every state the redesigned
``/hr/schedule`` calendar can show (on-time, early, late, early-leave, forgot
checkout, absent, OT, overnight, leave).

Design principles (mirrors the proven idempotent pattern of ``seed_checkin_data``):

* only raw ``Employee Checkin`` (IN/OUT, VN-local → UTC) + ``Shift Assignment``
  are created by hand — Work Sessions / flags are **never** set manually; the
  gege_hr engine ``recalculate_period(backfill=1)`` recomputes them consistently;
* every write is ``ignore_permissions`` + ``ignore_mandatory`` + dedup → re-runnable;
* **safety first**: ``backup=True`` dumps a JSON snapshot and ``dry_run=True``
  prints the plan without touching the DB.

Run (as the bench user)::

    # Dry-run (no writes) — review the plan first
    bench --site erp.local execute gege_hr.gege_hr.seed_clean_attendance.run \
        --kwargs "{'from_date':'2026-08-01','to_date':'2026-08-13','dry_run':true}"

    # One employee (with backup)
    bench --site erp.local execute gege_hr.gege_hr.seed_clean_attendance.run \
        --kwargs "{'from_date':'2026-08-01','to_date':'2026-08-13', \
                   'employees':['fujiwameow@gmail.com'],'backup':true}"

    # All active employees
    bench --site erp.local execute gege_hr.gege_hr.seed_clean_attendance.run \
        --kwargs "{'from_date':'2026-08-01','to_date':'2026-08-13','backup':true}"

Returns a report dict (also logged) describing backup file, rows purged/seeded,
and the recalculate summary.
"""

from __future__ import annotations

import datetime
import json
import os
from typing import Any
from zoneinfo import ZoneInfo

try:  # bench-free import safety (syntax check / unit tests outside a site)
    import frappe  # type: ignore
except Exception:  # pragma: no cover
    frappe = None  # type: ignore

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
COMPANY = "GeGe Esport"
PORTAL_TZ = "Asia/Ho_Chi_Minh"  # UTC+7, no DST
UTC = ZoneInfo("UTC")
VN = ZoneInfo(PORTAL_TZ)
BACKUP_DIR = "/home/frappe/plans"

# The 4 shifts currently in use. (name, start_time, end_time). Overnight is
# derived by the engine from start_time > end_time.
SHIFTS = [
    ("Ca Sáng", "08:00:00", "20:00:00"),
    ("Ca 9h-21h", "09:00:00", "21:00:00"),
    ("Ca Tối", "20:00:00", "08:00:00"),  # overnight
    ("Ca 21h-9h", "21:00:00", "09:00:00"),  # overnight
]

# Scenario cycle (key, offset_in_min_from_start, offset_out_min_from_end_or_None,
#                  extra: None | 'leave' | 'ot_pre' | 'ot_post').
#   None OUT  → forgot checkout; (None, None, 'absent') → no checkin (vắng);
#   'leave'   → no checkin + Leave Application Approved.
SCENARIOS = [
    ("on_time", -5, 0, None),
    ("early", -30, 0, None),
    ("early_ot", -60, 0, "ot_pre"),
    ("late", 12, 5, None),
    ("early_leave", 0, -60, None),
    ("forgot_out", 6, None, None),
    ("ot_post_unpaid", -10, 44, None),
    ("ot_post_paid", -10, 90, "ot_post"),
    ("absent", None, None, "absent"),
    ("leave", None, None, "leave"),
]


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _ok(name: str) -> bool:
    return bool(frappe and frappe.db.exists("DocType", name))


def _exists(dt: str, name: str) -> bool:
    try:
        return bool(frappe.db.exists(dt, name))
    except Exception:
        return False


def _log(msg: str) -> None:
    if not frappe:
        return
    try:
        frappe.log_error(title="seed_clean_attendance", message=str(msg)[:65000])
    except Exception:
        pass


def _to_utc_str(dt_local: datetime.datetime) -> str:
    """Local VN naive datetime → ``YYYY-MM-DD HH:MM:SS`` UTC (how Employee Checkin.time is stored)."""
    aware = dt_local.replace(tzinfo=VN)
    return aware.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _hhmm_to_time(hhmm: str) -> datetime.time:
    h, m, s = hhmm.split(":")
    return datetime.time(int(h), int(m), int(s))


def _daterange(from_date: str, to_date: str):
    d = datetime.date.fromisoformat(from_date)
    end = datetime.date.fromisoformat(to_date)
    while d <= end:
        yield d
        d += datetime.timedelta(days=1)


def _shift_window(day: datetime.date, start_hhmm: str, end_hhmm: str):
    """Return (start_dt_local, end_dt_local) for a shift on `day` (overnight → end next day)."""
    st = _hhmm_to_time(start_hhmm)
    en = _hhmm_to_time(end_hhmm)
    start_dt = datetime.datetime.combine(day, st)
    end_day = day + datetime.timedelta(days=1) if en <= st else day
    end_dt = datetime.datetime.combine(end_day, en)
    return start_dt, end_dt


def _active_employees(user_ids):
    """Return list of (employee_name, user_id). `user_ids` filters by user_id/email."""
    if not _ok("Employee"):
        return []
    filters = {"status": "Active"}
    rows = frappe.get_all("Employee", filters=filters, fields=["name", "user_id"], order_by="name")
    out = []
    for r in rows:
        uid = (r.get("user_id") or "").strip()
        if user_ids:
            if uid and uid in user_ids:
                out.append((r["name"], uid))
            elif r["name"] in user_ids:  # allow passing employee id directly
                out.append((r["name"], uid))
        else:
            out.append((r["name"], uid))
    return out


# --------------------------------------------------------------------------- #
# Step 0 — backup
# --------------------------------------------------------------------------- #
def _backup(from_date, to_date, emps) -> str | None:
    if not frappe:
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(BACKUP_DIR, f"backup_attendance_{ts}.json")
    emp_names = [e[0] for e in emps]
    snapshot: dict[str, Any] = {"window": [from_date, to_date], "employees": emp_names}
    scopes = [
        ("Employee Checkin", "employee", "time", False),
        ("VN Attendance Work Session", "employee", "work_date", False),
        ("VN Employee Shift Instance", "employee", "work_date", False),
        ("VN Overtime Request", "employee", "work_date", False),
    ]
    for dt, ef, df, _ in scopes:
        if not _ok(dt):
            continue
        try:
            rows = frappe.get_all(
                dt,
                filters={ef: ["in", emp_names], df: ["between", [from_date, to_date]]},
                fields=["*"],
                limit_page_length=0,
            )
            snapshot[dt] = rows
        except Exception as exc:  # noqa: BLE001
            snapshot[dt] = f"error: {exc}"
    # Leave Application + Shift Assignment use date ranges
    for dt, ef in [("Leave Application", "employee"), ("Shift Assignment", "employee")]:
        if not _ok(dt):
            continue
        try:
            rows = frappe.get_all(dt, filters={ef: ["in", emp_names]}, fields=["*"], limit_page_length=0)
            snapshot[dt] = rows
        except Exception as exc:  # noqa: BLE001
            snapshot[dt] = f"error: {exc}"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, ensure_ascii=False, indent=2, default=str)
    return path


# --------------------------------------------------------------------------- #
# Step 1 — scoped purge
# --------------------------------------------------------------------------- #
def _purge(from_date, to_date, emps, dry_run: bool) -> dict[str, int]:
    emp_names = [e[0] for e in emps]
    counts: dict[str, int] = {}
    # (doctype, employee_field, date_field, date_scoped). date_scoped=False → delete ALL
    # rows for the employee (used for Shift Instance so leftover out-of-window instances
    # can't overlap the freshly seeded ones during recalculate).
    specs = [
        ("Employee Checkin", "employee", "time", True),
        ("VN Attendance Work Session", "employee", "work_date", True),
        ("VN Employee Shift Instance", "employee", "work_date", False),
        ("VN Overtime Request", "employee", "work_date", True),
        ("Attendance", "employee", "attendance_date", True),
        # RUNTIME BUG: purging SI + checkins without their tickets left 327
        # orphaned VN Checkout Miss rows whose links could never validate —
        # penalise_expired then failed every hour (9.5k Error Log rows).
        ("VN Checkout Miss", "employee", "work_date", True),
    ]
    for dt, ef, df, scoped in specs:
        if not _ok(dt):
            continue
        try:
            filters = {ef: ["in", emp_names]}
            if scoped:
                filters[df] = ["between", [from_date, to_date]]
            names = frappe.get_all(dt, filters=filters, pluck="name", limit_page_length=0)
        except Exception as exc:  # noqa: BLE001
            _log(f"purge list {dt}: {exc}")
            names = []
        counts[dt] = len(names)
        if dry_run or not names:
            continue
        for name in names:
            try:
                doc = frappe.get_doc(dt, name)
                if getattr(doc, "docstatus", 0) == 1:
                    try:
                        doc.cancel()
                    except Exception:
                        # force-unsubmit so the delete can proceed
                        frappe.db.set_value(dt, name, "docstatus", 0, update_modified=False)
                frappe.delete_doc(dt, name, ignore_permissions=True, force=True)
            except Exception as exc:  # noqa: BLE001
                # last-resort raw delete (test-data cleanup; skips on_trash hooks)
                try:
                    frappe.db.delete(dt, name)
                except Exception as exc2:  # noqa: BLE001
                    _log(f"purge {dt} {name}: {exc} | db.delete: {exc2}")
    # Shift Assignment — cancel + delete active ones for these employees in/overlapping window
    if _ok("Shift Assignment"):
        try:
            sa = frappe.get_all(
                "Shift Assignment",
                filters={"employee": ["in", emp_names], "docstatus": 1},
                pluck="name",
                limit_page_length=0,
            )
        except Exception:  # noqa: BLE001
            sa = []
        counts["Shift Assignment"] = len(sa)
        if not dry_run:
            for name in sa:
                try:
                    doc = frappe.get_doc("Shift Assignment", name)
                    if doc.docstatus == 1:
                        try:
                            doc.cancel()
                        except Exception:
                            pass
                    frappe.delete_doc("Shift Assignment", name, ignore_permissions=True, force=True)
                except Exception as exc:  # noqa: BLE001
                    _log(f"purge Shift Assignment {name}: {exc}")
    # Leave Application overlapping the window
    if _ok("Leave Application"):
        try:
            la = frappe.get_all(
                "Leave Application",
                filters={
                    "employee": ["in", emp_names],
                    "to_date": [">=", from_date],
                    "from_date": ["<=", to_date],
                },
                pluck="name",
                limit_page_length=0,
            )
        except Exception:  # noqa: BLE001
            la = []
        counts["Leave Application"] = len(la)
        if not dry_run:
            for name in la:
                try:
                    frappe.delete_doc("Leave Application", name, ignore_permissions=True, force=True)
                except Exception as exc:  # noqa: BLE001
                    _log(f"purge Leave Application {name}: {exc}")
    if not dry_run:
        frappe.db.commit()
    return counts


# --------------------------------------------------------------------------- #
# Step 2 — ensure the 4 shift types
# --------------------------------------------------------------------------- #
def _ensure_shift_types() -> list[str]:
    if not _ok("Shift Type"):
        return []
    ready: list[str] = []
    for name, start, end in SHIFTS:
        if not _exists("Shift Type", name):
            try:
                doc = frappe.get_doc(
                    {"doctype": "Shift Type", "shift_type": name, "start_time": start, "end_time": end}
                )
                doc.flags.ignore_permissions = True
                doc.flags.ignore_mandatory = True
                doc.insert(ignore_permissions=True)
            except Exception as exc:  # noqa: BLE001
                _log(f"ensure Shift Type {name}: {exc}")
        # Ensure OT is ALLOWED (pre + post shift) — otherwise the engine computes
        # raw_overtime_hours = 0 even when staff work past the shift end.
        try:
            frappe.db.set_value(
                "Shift Type",
                name,
                {"vn_allow_overtime_before_shift": 1, "vn_allow_overtime_after_shift": 1},
                update_modified=False,
            )
        except Exception as exc:  # noqa: BLE001
            _log(f"set OT flags {name}: {exc}")
        ready.append(name)
    return ready


# --------------------------------------------------------------------------- #
# Step 3 — assign each employee a shift (rotate the 4)
# --------------------------------------------------------------------------- #
def _assign_shifts(from_date, to_date, emps, dry_run: bool) -> dict[str, str]:
    assignment: dict[str, str] = {}
    for idx, (emp, _uid) in enumerate(emps):
        shift = SHIFTS[idx % len(SHIFTS)][0]
        assignment[emp] = shift
        if dry_run:
            continue
        # cancel any overlapping active assignment first (purge already did, but be safe)
        try:
            existing = frappe.get_all(
                "Shift Assignment",
                filters={"employee": emp, "docstatus": 1},
                pluck="name",
                limit_page_length=0,
            )
            for name in existing:
                try:
                    d = frappe.get_doc("Shift Assignment", name)
                    if d.docstatus == 1:
                        d.cancel()
                    frappe.delete_doc("Shift Assignment", name, ignore_permissions=True, force=True)
                except Exception:
                    pass
            doc = frappe.get_doc(
                {
                    "doctype": "Shift Assignment",
                    "employee": emp,
                    "shift_type": shift,
                    "start_date": from_date,
                    "end_date": to_date,
                    "status": "Active",
                    "company": COMPANY,
                }
            )
            doc.flags.ignore_permissions = True
            doc.flags.ignore_mandatory = True
            doc.insert(ignore_permissions=True)
            try:
                doc.submit()
            except Exception as exc:  # noqa: BLE001
                _log(f"submit Shift Assignment {emp}/{shift}: {exc}")
        except Exception as exc:  # noqa: BLE001
            _log(f"assign {emp}/{shift}: {exc}")
    if not dry_run:
        frappe.db.commit()
    return assignment


# --------------------------------------------------------------------------- #
# Step 4 — seed Employee Checkin per scenario matrix
# --------------------------------------------------------------------------- #
def _make_checkin(emp: str, dt_local: datetime.datetime, log_type: str) -> bool:
    t = _to_utc_str(dt_local)
    try:
        if frappe.db.exists("Employee Checkin", {"employee": emp, "time": t, "log_type": log_type}):
            return False
    except Exception:
        pass
    try:
        doc = frappe.get_doc(
            {"doctype": "Employee Checkin", "employee": emp, "time": t, "log_type": log_type}
        )
        doc.flags.ignore_permissions = True
        doc.flags.ignore_mandatory = True
        doc.insert(ignore_permissions=True)
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"checkin {emp} {t} {log_type}: {exc}")
        return False


def _make_leave(emp: str, day: datetime.date) -> bool:
    """Best-effort single-day approved leave."""
    if not _ok("Leave Application"):
        return False
    try:
        leave_type = frappe.get_all("Leave Type", limit_page_length=1, pluck="name")
        if not leave_type:
            return False
        ds = day.isoformat()
        doc = frappe.get_doc(
            {
                "doctype": "Leave Application",
                "employee": emp,
                "leave_type": leave_type[0],
                "from_date": ds,
                "to_date": ds,
                "description": "Seed: nghỉ phép (test)",
                "status": "Approved",
            }
        )
        doc.flags.ignore_permissions = True
        doc.flags.ignore_mandatory = True
        doc.insert(ignore_permissions=True)
        try:
            doc.submit()
        except Exception:
            frappe.db.set_value("Leave Application", doc.name, "status", "Approved", update_modified=False)
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"leave {emp} {day}: {exc}")
        return False


def _make_ot(
    emp: str, day: datetime.date, from_local: datetime.datetime, to_local: datetime.datetime, pre: bool
) -> bool:
    """Best-effort Overtime Request (Pending)."""
    if not _ok("VN Overtime Request"):
        return False
    try:
        ds = day.isoformat()
        doc = frappe.get_doc(
            {
                "doctype": "VN Overtime Request",
                "employee": emp,
                "work_date": ds,
                "overtime_type": "Pre-shift" if pre else "Post-shift",
                "from_datetime": _to_utc_str(from_local),
                "to_datetime": _to_utc_str(to_local),
                "requested_hours": round((to_local - from_local).total_seconds() / 3600, 2),
                "reason": "Seed: tăng ca (test)",
            }
        )
        doc.flags.ignore_permissions = True
        doc.flags.ignore_mandatory = True
        doc.insert(ignore_permissions=True)
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"ot {emp} {day}: {exc}")
        return False


def _seed_checkins(from_date, to_date, emps, assignment, dry_run: bool) -> dict[str, int]:
    out = {"checkins": 0, "leaves": 0, "ot": 0, "plan": []}
    days = list(_daterange(from_date, to_date))
    shift_lookup = {n: (s, e) for n, s, e in SHIFTS}
    for eidx, (emp, _uid) in enumerate(emps):
        shift_name = assignment.get(emp)
        if not shift_name or shift_name not in shift_lookup:
            continue
        start_hhmm, end_hhmm = shift_lookup[shift_name]
        for didx, day in enumerate(days):
            scen = SCENARIOS[(didx + eidx) % len(SCENARIOS)]
            skey, in_off, out_off, extra = scen
            start_dt, end_dt = _shift_window(day, start_hhmm, end_hhmm)
            plan_item = {"employee": emp, "date": day.isoformat(), "shift": shift_name, "scenario": skey}
            out["plan"].append(plan_item)
            if dry_run:
                continue
            if extra == "leave":
                if _make_leave(emp, day):
                    out["leaves"] += 1
                continue
            if extra == "absent":
                continue  # no checkin → engine marks absent (shift instance exists via assignment)
            # IN
            if in_off is not None:
                in_dt = start_dt + datetime.timedelta(minutes=in_off)
                if _make_checkin(emp, in_dt, "IN"):
                    out["checkins"] += 1
            # OUT
            if out_off is not None:
                out_dt = end_dt + datetime.timedelta(minutes=out_off)
                if _make_checkin(emp, out_dt, "OUT"):
                    out["checkins"] += 1
            # OT request (best-effort)
            if extra == "ot_pre" and in_off is not None:
                _from = start_dt + datetime.timedelta(minutes=in_off)
                _to = start_dt
                if _make_ot(emp, day, _from, _to, pre=True):
                    out["ot"] += 1
            elif extra == "ot_post" and out_off is not None:
                _from = end_dt
                _to = end_dt + datetime.timedelta(minutes=out_off)
                if _make_ot(emp, day, _from, _to, pre=False):
                    out["ot"] += 1
    if not dry_run:
        frappe.db.commit()
    return out


# --------------------------------------------------------------------------- #
# Step 5 — recompute Work Sessions + Shift Instances
# --------------------------------------------------------------------------- #
def _recalc(from_date, to_date, emps, dry_run: bool):
    if dry_run:
        return {"skipped": "dry_run"}
    try:
        from gege_hr.gege_hr.api import attendance as att
    except Exception as exc:  # noqa: BLE001
        return {"error": f"import: {exc}"}
    summary = {}
    for emp, _uid in emps:
        try:
            summary[emp] = str(att.recalculate_period(from_date, to_date, employee=emp, backfill=1))
        except Exception as exc:  # noqa: BLE001
            _log(f"recalc {emp}: {exc}")
            summary[emp] = f"error: {exc}"
    # Sync Work Session → Frappe Attendance (backfill) so core reports + team
    # views stay consistent with the recalced Work Sessions.
    try:
        from gege_hr.gege_hr.api import attendance_sync

        summary["_attendance_backfill"] = str(attendance_sync.backfill_attendance(from_date, to_date))
    except Exception as exc:  # noqa: BLE001
        _log(f"attendance backfill: {exc}")
        summary["_attendance_backfill"] = f"error: {exc}"
    return summary


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #
def run(
    from_date: str | None = None,
    to_date: str | None = None,
    employees: list[str] | None = None,
    backup: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Seed clean attendance for a date window. See module docstring for usage."""
    if not frappe:
        return {"error": "frappe not available (run via bench execute)"}

    today = datetime.date.today()
    if not from_date:
        from_date = today.replace(day=1).isoformat()
    if not to_date:
        end = (
            today.replace(day=28)
            if today.month == 12
            else (today.replace(month=today.month + 1, day=1) - datetime.timedelta(days=1))
        )
        to_date = end.isoformat()

    emps = _active_employees(employees)
    report: dict[str, Any] = {
        "window": [from_date, to_date],
        "dry_run": dry_run,
        "employees": [e[0] for e in emps],
        "backup": None,
        "purged": {},
        "shift_types": [],
        "assignments": {},
        "seeded": {},
        "recalc": {},
    }
    if not emps:
        report["error"] = "no active employees matched"
        return report

    # 0. backup
    if backup and not dry_run:
        try:
            report["backup"] = _backup(from_date, to_date, emps)
        except Exception as exc:  # noqa: BLE001
            report["backup"] = f"error: {exc}"

    # 1. purge
    report["purged"] = _purge(from_date, to_date, emps, dry_run)

    # 2. shift types
    report["shift_types"] = _ensure_shift_types() if not dry_run else [n for n, _, _ in SHIFTS]

    # 3. assign shifts
    report["assignments"] = _assign_shifts(from_date, to_date, emps, dry_run)

    # 4. seed checkins
    report["seeded"] = _seed_checkins(from_date, to_date, emps, report["assignments"], dry_run)

    # 5. recompute
    report["recalc"] = _recalc(from_date, to_date, emps, dry_run)

    if not dry_run:
        frappe.db.commit()
    _log(f"seed_clean_attendance report: {json.dumps(report, ensure_ascii=False, default=str)[:65000]}")
    return report
