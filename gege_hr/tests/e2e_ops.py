"""E2E fixtures + DB assertions for the check-in/out browser suite.

Invoked via ``bench --site <site> execute gege_hr.tests.e2e_ops.<fn>`` from the
Playwright helper (tests-e2e/lib.mjs). Each function prints ``ASSERT: ...``
lines consumed by the suite; every function is idempotent and scoped to the
single E2E employee so reruns never leak state.

Functions:
  setup_day()        — clean + day-shift SA (Ca E2E Ngày 08-20) yesterday→+2d
  setup_overnight()  — clean + overnight SA (Ca E2E Đêm 21-09) + seed IN yesterday
  assert_state()     — dump today's logs / work-session / tickets (offset via kwarg)
  cleanup()          — wipe every E2E trace
  run_engine()       — invoke checkout_miss.run_hourly() manually (Group C)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import frappe

EMP = "HR-EMP-00001"

_RESET_WS = {
    "actual_checkin": None,
    "actual_checkout": None,
    "late_minutes": 0,
    "early_leave_minutes": 0,
    "regular_hours": 0,
    "total_actual_hours": 0,
    "raw_overtime_hours": 0,
    "approved_overtime_hours": 0,
    "need_review": 0,
    "vn_auto_checkout": 0,
    "missing_checkout": 0,
    "has_leave": 0,
    "absent": 0,
}


def _today():
    from gege_hr.gege_hr.utils import tz

    return tz.now_in_portal().date()


def _day_str(offset=0):
    return str(_today() + timedelta(days=offset))


def _clean_e2e():
    # -3..-1: the D3b/D4 checkout-miss fixtures run at T-2 (time-independent
    # window), and a previous run's APPROVED real-OUT log at T-2 must not
    # survive — the engine's FINDING-P1 check would see a real OUT and skip
    # closing the freshly seeded open session.
    for off in (-3, -2, -1, 0, 1):
        d = _day_str(off)
        nxt = str(_today() + timedelta(days=off + 1))
        for r in frappe.get_all(
            "Employee Checkin",
            filters={"employee": EMP, "time": ["between", [d, nxt]]},
            fields=["name"],
        ):
            frappe.delete_doc("Employee Checkin", r.name, force=True, ignore_permissions=True)
        frappe.db.set_value(
            "VN Attendance Work Session",
            {"employee": EMP, "work_date": d},
            dict(_RESET_WS),
            update_modified=False,
        )
    for t in frappe.get_all(
        "VN Checkout Miss",
        filters={"employee": EMP, "work_date": [">=", _day_str(-2)]},
        fields=["name"],
    ):
        frappe.delete_doc("VN Checkout Miss", t.name, force=True, ignore_permissions=True)
    for sa in frappe.get_all("Shift Assignment", filters={"employee": EMP}, fields=["name", "docstatus"]):
        try:
            if sa.docstatus == 1:
                doc = frappe.get_doc("Shift Assignment", sa.name)
                doc.flags.ignore_permissions = True
                doc.cancel()
            frappe.delete_doc("Shift Assignment", sa.name, force=True, ignore_permissions=True)
        except Exception:
            pass
    for a in frappe.get_all(
        "VN Mobile Checkin Attempt",
        filters={"client_request_id": ["like", "e2e-%"]},
        fields=["name"],
    ):
        frappe.delete_doc("VN Mobile Checkin Attempt", a.name, force=True, ignore_permissions=True)
    # Correction Requests opened by explain runs (their duplicate-validate
    # would block every subsequent D2 re-run if left behind).
    for cr in frappe.get_all(
        "VN Attendance Correction Request",
        filters={"employee": EMP},
        fields=["name", "docstatus"],
    ):
        try:
            if cr.docstatus == 1:
                d = frappe.get_doc("VN Attendance Correction Request", cr.name)
                d.flags.ignore_permissions = True
                d.cancel()
            frappe.delete_doc(
                "VN Attendance Correction Request", cr.name, force=True, ignore_permissions=True
            )
        except Exception:
            pass
    frappe.db.commit()


def _ensure_shift_type(name, start, end):
    if frappe.db.exists("Shift Type", name):
        # UPDATE on exist — the H2 open-shift window is recomputed around the
        # current wall clock each run, so a stale window from a previous run
        # must not survive (its end + grace would already be in the past).
        frappe.db.set_value("Shift Type", name, {"start_time": start, "end_time": end}, update_modified=False)
        frappe.db.commit()
        return
    frappe.get_doc({"doctype": "Shift Type", "__newname": name, "start_time": start, "end_time": end}).insert(
        ignore_permissions=True
    )
    frappe.db.commit()


def _create_sa(shift, days_back=1, days_fwd=2, work_location=None):
    sa = frappe.get_doc(
        {
            "doctype": "Shift Assignment",
            "employee": EMP,
            "shift_type": shift,
            "start_date": _today() - timedelta(days=days_back),
            "end_date": _today() + timedelta(days=days_fwd),
            "status": "Active",
            "vn_work_location": work_location,
        }
    )
    sa.insert(ignore_permissions=True)
    sa.submit()
    frappe.db.commit()
    return sa.name


def _seed_log(log_type, portal_hour, portal_minute=0, day_offset=0):
    """Insert a checkin at a portal WALL-CLOCK time (this bench's DB frame)."""
    d = _today() + timedelta(days=day_offset)
    log = frappe.get_doc(
        {
            "doctype": "Employee Checkin",
            "employee": EMP,
            "log_type": log_type,
            "time": datetime(d.year, d.month, d.day, portal_hour, portal_minute),
            "device_id": "e2e-seed",
        }
    )
    log.insert(ignore_permissions=True)
    frappe.db.commit()
    return log.name


def setup_day():
    _clean_e2e()
    _ensure_shift_type("Ca E2E Ngày", "08:00:00", "20:00:00")
    _ensure_shift_type("Ca E2E Tối", "20:00:00", "23:00:00")
    _ensure_shift_type("Ca E2E Đêm", "21:00:00", "09:00:00")
    # Attach the HQ geofence so Group F can exercise the client-side guard.
    loc = frappe.db.get_value("VN Work Location", {}, "name")
    print(f"ASSERT: SA_DAY {_create_sa('Ca E2E Ngày', work_location=loc)}")


def setup_evening():
    """Evening day-shift SA (20:00–23:00) — BEFORE the window all afternoon, so
    Group A can assert the 'Chưa đến giờ chấm công' state + a no-op tap."""
    _clean_e2e()
    _ensure_shift_type("Ca E2E Tối", "20:00:00", "23:00:00")
    print(f"ASSERT: SA_EVENING {_create_sa('Ca E2E Tối')}")


def setup_overnight(seed_in_yesterday=True, hour=23, minute=30, day_offset=-1):
    """Overnight SA + seeded IN. ``day_offset=-2`` makes the checkout-miss
    fixture TIME-INDEPENDENT: the planned end (T-1 09:00) + 360' engine buffer
    (T-1 15:00) is always in the past, so ``run_engine`` closes the session
    and opens the ticket at any hour the suite runs."""
    _clean_e2e()
    _ensure_shift_type("Ca E2E Đêm", "21:00:00", "09:00:00")
    sa = _create_sa("Ca E2E Đêm", days_back=2, days_fwd=2)
    name = _seed_log("IN", hour, minute, day_offset=day_offset) if seed_in_yesterday else None
    print(f"ASSERT: SA_NIGHT {sa} SEED_IN {name or 'none'}")


def setup_overnight_open(start_time, end_time, in_day_offset, in_hour, in_minute):
    """H2 fixture: an overnight shift whose planned window CONTAINS the current
    wall clock (crosses midnight — computed by the caller so the run works at
    any hour), plus an IN already punched → the member is MID-SHIFT now, so the
    team view must render "Đang làm việc" (not "Thiếu chấm ra")."""
    _clean_e2e()
    _ensure_shift_type("Ca E2E Đêm Mở", start_time, end_time)
    sa = _create_sa("Ca E2E Đêm Mở", days_back=2, days_fwd=2)
    name = _seed_log("IN", in_hour, in_minute, day_offset=in_day_offset)
    print(f"ASSERT: SA_OPEN {sa} WINDOW {start_time}->{end_time} SEED_IN {name}")


def assert_state(day_offset=0):
    d = _day_str(day_offset)
    nxt = str(_today() + timedelta(days=day_offset + 1))
    logs = frappe.get_all(
        "Employee Checkin",
        filters={"employee": EMP, "time": ["between", [d, nxt]]},
        fields=["name", "time", "log_type"],
        order_by="time asc",
    )
    print(
        f"ASSERT: LOGS_{day_offset:+d} {json.dumps([{'t': str(lg.time), 'lt': lg.log_type} for lg in logs])}"
    )
    ws = frappe.db.get_value(
        "VN Attendance Work Session",
        {"employee": EMP, "work_date": d},
        [
            "actual_checkin",
            "actual_checkout",
            "late_minutes",
            "early_leave_minutes",
            "regular_hours",
            "total_actual_hours",
            "raw_overtime_hours",
            "need_review",
            "vn_auto_checkout",
            "missing_checkout",
        ],
        as_dict=True,
    )
    print(f"ASSERT: WS_{day_offset:+d} {json.dumps(ws, default=str) if ws else 'null'}")
    tk = frappe.get_all(
        "VN Checkout Miss",
        filters={"employee": EMP, "work_date": d},
        fields=["name", "status", "penalty_amount"],
    )
    print(f"ASSERT: TICKETS_{day_offset:+d} {json.dumps(tk, default=str)}")


def run_engine():
    from gege_hr.gege_hr.utils import checkout_miss

    res = checkout_miss.run_hourly()
    # The engine now commits per employee (FINDING-P4 fix); this final commit
    # covers the penalise_expired leg so bench-exit leaves nothing pending.
    frappe.db.commit()
    print(f"ASSERT: ENGINE {json.dumps(res, default=str)}")


def reset_si(from_date=None, to_date=None):
    """PHASE-1: drop stale Shift Instances (planned_* in the OLD frame) + WS so
    the next recalc regenerates them in naive wall."""
    frm = from_date or _day_str(-2)
    to = to_date or _day_str(1)
    n = 0
    for si in frappe.get_all(
        "VN Employee Shift Instance",
        filters={"employee": EMP, "work_date": ["between", [frm, to]]},
        fields=["name", "docstatus"],
    ):
        try:
            if si.docstatus == 1:
                d = frappe.get_doc("VN Employee Shift Instance", si.name)
                d.flags.ignore_permissions = True
                d.cancel()
            frappe.delete_doc("VN Employee Shift Instance", si.name, force=True, ignore_permissions=True)
            n += 1
        except Exception as e:
            print(f"ASSERT: RESET_SI_ERR {si.name} {e}")
    for ws in frappe.get_all(
        "VN Attendance Work Session",
        filters={"employee": EMP, "work_date": ["between", [frm, to]]},
        fields=["name"],
    ):
        frappe.delete_doc("VN Attendance Work Session", ws.name, force=True, ignore_permissions=True)
    frappe.db.commit()
    print(f"ASSERT: RESET_SI dropped={n}")


def seed_b2():
    """Group B2 fixture: drop today's OUTs after 15:00 (real-late + engine
    fake) and seed a clean OUT at 10:00 — inside the night shift's
    max_checkout window so the engine pairs it."""
    d0 = _day_str(0)
    nxt = str(_today() + timedelta(days=1))
    # Drop EVERY OUT today (the real-late tap + any engine fake) for a clean pair.
    for r in frappe.get_all(
        "Employee Checkin",
        filters={"employee": EMP, "log_type": "OUT", "time": ["between", [d0, nxt]]},
        fields=["name", "time", "log_type"],
    ):
        frappe.delete_doc("Employee Checkin", r.name, force=True, ignore_permissions=True)
    name = _seed_log("OUT", 10, 0, day_offset=0)
    frappe.db.commit()
    print(f"ASSERT: SEED_B2 out={name}")


def expire_ticket():
    """Force every open Checkout-Miss ticket of EMP into an expired grace
    (past deadline) so penalise_expired() can flip it (Group C6)."""
    from datetime import datetime

    n = 0
    for t in frappe.get_all(
        "VN Checkout Miss",
        filters={"employee": EMP, "status": "Pending"},
        fields=["name"],
    ):
        frappe.db.set_value(
            "VN Checkout Miss", t.name, {"grace_deadline": datetime(2026, 1, 1, 0, 0)}, update_modified=False
        )
        n += 1
    frappe.db.commit()
    print(f"ASSERT: EXPIRED_TICKETS {n}")


def clear_ins_today():
    """Drop every IN log of EMP today (Group G orphan-OUT fixture helper)."""
    d0 = _day_str(0)
    nxt = str(_today() + timedelta(days=1))
    n = 0
    for r in frappe.get_all(
        "Employee Checkin",
        filters={"employee": EMP, "log_type": "IN", "time": ["between", [d0, nxt]]},
        fields=["name"],
    ):
        frappe.delete_doc("Employee Checkin", r.name, force=True, ignore_permissions=True)
        n += 1
    frappe.db.commit()
    print(f"ASSERT: CLEARED_INS {n}")


def lock_yesterday():
    """Create + lock a VN Monthly Attendance Period covering T-1 (Group I2)."""
    frm = str(_today() - timedelta(days=1))
    to = str(_today() - timedelta(days=1))
    name = frappe.db.exists("VN Monthly Attendance Period", {"from_date": frm, "to_date": to})
    if not name:
        doc = frappe.get_doc(
            {
                "doctype": "VN Monthly Attendance Period",
                "from_date": frm,
                "to_date": to,
                "status": "Locked",
            }
        )
        doc.insert(ignore_permissions=True)
        frappe.db.commit()
        name = doc.name
    else:
        frappe.db.set_value("VN Monthly Attendance Period", name, "status", "Locked", update_modified=False)
        frappe.db.commit()
    print(f"ASSERT: LOCKED {name}")


def unlock_yesterday():
    frm = str(_today() - timedelta(days=1))
    name = frappe.db.exists("VN Monthly Attendance Period", {"from_date": frm})
    if name:
        frappe.delete_doc("VN Monthly Attendance Period", name, force=True, ignore_permissions=True)
        frappe.db.commit()
    print("ASSERT: UNLOCKED ok")


def reset_ticket_pending(work_date_offset=-1):
    """Group D helper: flip the latest EMP ticket back to Pending so
    explain_checkout_miss accepts a new explanation (D2 re-run)."""
    tk = frappe.get_all(
        "VN Checkout Miss",
        filters={
            "employee": EMP,
            "work_date": _day_str(work_date_offset),
            "status": ["in", ["Pending", "Explained", "Penalised"]],
        },
        fields=["name"],
        order_by="creation desc",
        limit=1,
    )
    if tk:
        frappe.db.set_value(
            "VN Checkout Miss", tk[0].name, {"status": "Pending", "explanation": ""}, update_modified=False
        )
        frappe.db.commit()
    print(f"ASSERT: TICKET_PENDING {tk[0].name if tk else 'none'}")


def recalc(from_date=None, to_date=None):
    """Materialise Work Sessions via the period recalculate (admin)."""
    from gege_hr.gege_hr.api import attendance as att

    frappe.set_user("Administrator")
    res = att.recalculate_period(from_date=from_date or _day_str(-1), to_date=to_date or _day_str(0))
    print(f"ASSERT: RECALC {json.dumps(res, default=str)[:400]}")


# ── FULL-APP LIFECYCLE OPS (plans/full-app-e2e-plan.md §3) ────────────────────
# Every op is idempotent + scoped to the LC employee/user passed in by the
# browser suite (never touches the legacy 42-case fixtures).


def lc_create_employee(email=None, first="E2E", last="Newcomer", reports_to="HR-EMP-00002"):
    """LC2 — create the LC Employee (minimal mandatory fields) + link the LC
    user. Employee autoname follows the site's HR-EMP-xxxxx series."""
    company = frappe.db.get_value("Employee", EMP, "company")
    doc = frappe.get_doc(
        {
            "doctype": "Employee",
            "first_name": first,
            "last_name": last,
            "company": company,
            "reports_to": reports_to,
            "date_of_birth": "1990-01-01",
            "gender": "Male",
            "date_of_joining": str(_today() - timedelta(days=3)),
            "status": "Active",
        }
    )
    doc.insert(ignore_permissions=True)
    if email:
        # Link via the admin API (NOT a raw db.set_value) so the FINDING-LC1b
        # role re-add ("Employee" role was stripped while unlinked) runs.
        frappe.set_user("Administrator")
        from gege_hr.gege_hr.api import admin as admin_api

        admin_api.link_user_to_employee(employee=doc.name, user=email)
    frappe.db.commit()
    print(f"ASSERT: LC_EMP_CREATED {doc.name} company={company}")


def _lc_drop(dt, names, cancel_first=True):
    n = 0
    for r in names:
        try:
            doc = frappe.get_doc(dt, r)
            if cancel_first and getattr(doc, "docstatus", 0) == 1:
                doc.flags.ignore_permissions = True
                doc.cancel()
            frappe.delete_doc(dt, r, force=True, ignore_permissions=True)
            n += 1
        except Exception:
            try:
                frappe.delete_doc(dt, r, force=True, ignore_permissions=True, ignore_links=True)
                n += 1
            except Exception:
                pass
    return n


def lc_purge_ghosts():
    """Drop EVERY leftover LC identity (employee_name ~ E2E%): lines, slips,
    SSA, attendance chain, SI/SA (incl. dangling rows whose Employee is gone),
    Employee + e2e.* users. Ghost SAs leak into _materialise_shift_instances
    (company-wide scan) and their SIs used to block the next run with
    'Ca này trùng giờ' (F-LC17)."""
    frappe.set_user("Administrator")
    ghosts = frappe.get_all("Employee", filters={"employee_name": ["like", "%E2E%"]}, pluck="name")
    stats = {"emps": len(ghosts)}
    stats["line"] = _lc_drop(
        "VN Payroll Review Line",
        frappe.get_all("VN Payroll Review Line", filters={"employee": ["in", ghosts or ["-"]]}, pluck="name"),
        cancel_first=False,
    )
    for dt in (
        "Salary Slip",
        "Additional Salary",
        "Salary Structure Assignment",
        "VN Attendance Correction Request",
        "VN Checkout Miss",
        "VN Attendance Work Session",
        "VN Employee Shift Instance",
        "Shift Assignment",
        "Employee Checkin",
    ):
        stats[dt] = _lc_drop(
            dt, frappe.get_all(dt, filters={"employee": ["in", ghosts or ["-"]]}, pluck="name")
        )
    stats["Employee"] = _lc_drop("Employee", ghosts)
    for dt in ("VN Employee Shift Instance", "Shift Assignment"):
        dangling = [
            r.name
            for r in frappe.get_all(dt, fields=["name", "employee"])
            if r.employee and not frappe.db.exists("Employee", r.employee)
        ]
        stats[f"{dt}~dangling"] = _lc_drop(dt, dangling)
    for u in frappe.get_all("User", filters={"email": ["like", "e2e.%"]}, pluck="name"):
        try:
            frappe.delete_doc("User", u, force=True, ignore_permissions=True)
        except Exception:
            pass
    frappe.db.commit()
    print("ASSERT: LC_PURGED " + json.dumps(stats))


def lc_state(email=None, emp=None):
    """Dump the lifecycle state of the LC employee: user/roles, employee link,
    shift assignment, work sessions in T-3..T+1, payslip rows."""
    out = {"user": None, "roles": [], "employee": None, "linked": False, "sa": 0, "ws": []}
    if email and frappe.db.exists("User", email):
        from gege_hr.gege_hr.utils import employee as emp_util

        out["user"] = email
        out["roles"] = sorted(emp_util.get_user_roles(email) or [])
    if emp and frappe.db.exists("Employee", emp):
        out["employee"] = emp
        out["linked"] = (frappe.db.get_value("Employee", emp, "user_id") or "") == (email or "")
    if emp:
        out["sa"] = frappe.db.count("Shift Assignment", {"employee": emp, "docstatus": 1})
        for ws in frappe.get_all(
            "VN Attendance Work Session",
            filters={
                "employee": emp,
                "work_date": ["between", [_day_str(-3), _day_str(1)]],
            },
            fields=[
                "work_date",
                "actual_checkin",
                "actual_checkout",
                "late_minutes",
                "regular_hours",
                "total_actual_hours",
                "raw_overtime_hours",
                "approved_overtime_hours",
            ],
            order_by="work_date asc",
        ):
            out["ws"].append({k: str(v) for k, v in ws.items()})
    frappe.db.commit()
    print("ASSERT: LC_STATE " + json.dumps(out, default=str))


def lc_wipe(email=None, emp=None):
    """Cascade-clean the LC employee: slips → payroll line/period → attendance
    chain → employee → user. Safe to call repeatedly."""
    if emp:
        # payroll side (SSA first — slip rows link it)
        for doctype in ("Salary Structure Assignment", "Additional Salary", "Salary Slip"):
            for r in frappe.get_all(doctype, filters={"employee": emp}, fields=["name", "docstatus"]):
                try:
                    if r.docstatus == 1:
                        d = frappe.get_doc(doctype, r.name)
                        d.flags.ignore_permissions = True
                        d.cancel()
                    frappe.delete_doc(doctype, r.name, force=True, ignore_permissions=True)
                except Exception:
                    pass
        for r in frappe.get_all(
            "VN Payroll Review Line", filters={"employee": emp}, fields=["name", "parent"]
        ):
            try:
                frappe.delete_doc("VN Payroll Review Line", r.name, force=True, ignore_permissions=True)
            except Exception:
                pass
        # attendance chain
        for off in (-4, -3, -2, -1, 0, 1):
            d = _day_str(off)
            nxt = str(_today() + timedelta(days=off + 1))
            for r in frappe.get_all(
                "Employee Checkin", filters={"employee": emp, "time": ["between", [d, nxt]]}, fields=["name"]
            ):
                frappe.delete_doc("Employee Checkin", r.name, force=True, ignore_permissions=True)
        for doctype in (
            "VN Attendance Work Session",
            "VN Employee Shift Instance",
            "VN Checkout Miss",
            "VN Attendance Correction Request",
        ):
            for r in frappe.get_all(doctype, filters={"employee": emp}, fields=["name", "docstatus"]):
                try:
                    if r.docstatus == 1:
                        d = frappe.get_doc(doctype, r.name)
                        d.flags.ignore_permissions = True
                        d.cancel()
                    frappe.delete_doc(doctype, r.name, force=True, ignore_permissions=True)
                except Exception:
                    pass
        for r in frappe.get_all("Shift Assignment", filters={"employee": emp}, fields=["name", "docstatus"]):
            try:
                if r.docstatus == 1:
                    d = frappe.get_doc("Shift Assignment", r.name)
                    d.flags.ignore_permissions = True
                    d.cancel()
                frappe.delete_doc("Shift Assignment", r.name, force=True, ignore_permissions=True)
            except Exception:
                pass
        try:
            frappe.delete_doc("Employee", emp, force=True, ignore_permissions=True)
        except Exception:
            pass
    if email and frappe.db.exists("User", email):
        try:
            frappe.delete_doc("User", email, force=True, ignore_permissions=True)
        except Exception:
            pass
    frappe.db.commit()
    print("ASSERT: LC_WIPED ok")


def lc_assign_salary(emp):
    """LC14 prep — copy the reference employee's Salary Structure Assignment
    (structure + base) to the LC employee. Without an assignment the slip
    generation step cannot create a Salary Slip for the newcomer."""
    ref = frappe.db.get_value(
        "Salary Structure Assignment",
        {"employee": EMP, "docstatus": 1},
        ["salary_structure", "base", "company"],
        as_dict=True,
    )
    if not ref:
        print("ASSERT: LC_SALARY none")
        return
    if frappe.db.exists("Salary Structure Assignment", {"employee": emp, "docstatus": 1}):
        print("ASSERT: LC_SALARY exists")
        return
    doc = frappe.get_doc(
        {
            "doctype": "Salary Structure Assignment",
            "employee": emp,
            "salary_structure": ref.salary_structure,
            "base": ref.base,
            "company": ref.company,
            "from_date": str(_today() - timedelta(days=4)),
        }
    )
    doc.insert(ignore_permissions=True)
    doc.submit()
    frappe.db.commit()
    print(f"ASSERT: LC_SALARY {doc.name} structure={ref.salary_structure} base={ref.base}")


def lc_wipe_payroll_period(month, year, from_date=None, to_date=None):
    """Drop the LC payroll review period (+ its lines + slips) for a month so
    the create step is re-runnable. When ``from_date``/``to_date`` are given,
    also purge Salary Slips sharing that exact window — HRMS rejects a second
    slip per employee+period even when the old slip's period row is gone."""
    n = 0
    periods = frappe.get_all(
        "VN Payroll Review Period",
        # zero-pad: the Select field stores "08", not "8" (LC13 dup bug).
        filters={"payroll_month": f"{int(month):02d}", "payroll_year": int(year)},
        pluck="name",
    )
    # Drop the periods' slips AND any slip sharing the LC window (start/end
    # 15..19): HRMS rejects a second slip for the same employee+period, so
    # leftovers from earlier runs made every later generate fall back.
    slip_n = 0
    slip_filters = [["vn_payroll_review_period", "in", periods or ["-"]]]
    if from_date and to_date:
        slip_filters.append(["start_date", "=", str(from_date)])
    for s in frappe.get_all(
        "Salary Slip",
        or_filters=slip_filters,
        fields=["name", "docstatus", "start_date", "end_date"],
    ):
        if (
            from_date
            and to_date
            and not (str(s.start_date) == str(from_date) and str(s.end_date) == str(to_date))
        ):
            continue  # period-linked but a different window — leave it
        try:
            if s.docstatus == 1:
                d = frappe.get_doc("Salary Slip", s.name)
                d.flags.ignore_permissions = True
                d.cancel()
            frappe.delete_doc("Salary Slip", s.name, force=True, ignore_permissions=True)
            slip_n += 1
        except Exception:
            pass
    errs = []
    for r in periods:
        try:
            for ln in frappe.get_all(
                "VN Payroll Review Line",
                filters={"payroll_review_period": r},
                pluck="name",
            ):
                try:
                    frappe.delete_doc("VN Payroll Review Line", ln, force=True, ignore_permissions=True)
                except Exception:
                    pass
            pdoc = frappe.get_doc("VN Payroll Review Period", r)
            if pdoc.docstatus == 1:
                pdoc.flags.ignore_permissions = True
                pdoc.cancel()
            frappe.delete_doc("VN Payroll Review Period", r, force=True, ignore_permissions=True)
            n += 1
        except Exception as e:
            errs.append(f"{r}:{type(e).__name__}")
    frappe.db.commit()
    print(f"ASSERT: LC_PERIOD_WIPED {n} slips={slip_n} errs={errs}")


def lc_payroll_prepare(month, year, from_date, to_date):
    """LC13 — wipe + create the Draft review period for the whole company of
    the reference employee, as Administrator. Prints the period name."""
    from gege_hr.gege_hr.api import payroll as pay_api

    lc_wipe_payroll_period(month, year, from_date, to_date)
    company = frappe.db.get_value("Employee", EMP, "company")
    frappe.set_user("Administrator")
    res = pay_api.create_payroll_review(
        company=company,
        payroll_month=f"{int(month):02d}",
        payroll_year=int(year),
        from_date=from_date,
        to_date=to_date,
    )
    frappe.db.commit()
    print("ASSERT: LC_PERIOD " + json.dumps(res, default=str))


def lc_payroll_calc(period):
    """LC13 — calculate the review period (Administrator)."""
    from gege_hr.gege_hr.api import payroll as pay_api

    frappe.set_user("Administrator")
    res = pay_api.calculate_payroll_review(name=period)
    frappe.db.commit()
    print("ASSERT: LC_CALC " + json.dumps(res, default=str)[:400])


def lc_payroll_line(period, emp):
    """LC13 assert — the LC employee's review line (hours + amounts)."""
    ln = frappe.db.get_value(
        "VN Payroll Review Line",
        # child-table link field (not "parent")
        {"payroll_review_period": period, "employee": emp},
        # real columns of the doctype (worked_hours/ot_hours don't exist)
        ["status", "regular_hours", "overtime_hours", "gross_pay", "net_pay"],
        as_dict=True,
    )
    frappe.db.commit()
    if ln:
        print("ASSERT: LC_LINE " + json.dumps({k: str(v) for k, v in ln.items()}))
    else:
        emps = frappe.get_all(
            "VN Payroll Review Line",
            filters={"payroll_review_period": period},
            pluck="employee",
        )
        print(f"ASSERT: LC_LINE null lines_emps={emps}")


def _cancel_all_sa(emp):
    for sa in frappe.get_all("Shift Assignment", {"employee": emp}, ["name", "docstatus"]):
        try:
            if sa.docstatus == 1:
                d = frappe.get_doc("Shift Assignment", sa.name)
                d.flags.ignore_permissions = True
                d.cancel()
            frappe.delete_doc("Shift Assignment", sa.name, force=True, ignore_permissions=True)
        except Exception:
            pass


def lc_shifts_night_then_day(emp):
    """LC fixture — SA layout: night shift T-3..T-2 (21:00→09:00), day shift
    T-1..T+2 (08:00→20:00). Date ranges never overlap, so the conflict check
    passes. Cancels every existing SA of the LC employee first."""
    _ensure_shift_type("Ca E2E Đêm", "21:00:00", "09:00:00")
    # 10:00-20:00 — the night shift ends 09:00 NEXT morning; an 08:00 day start
    # overlaps it by 1h and the Shift-Instance overlap check silently drops the
    # day SI (LC11 lost its work session entirely).
    _ensure_shift_type("Ca E2E LC Ngày", "10:00:00", "20:00:00")
    _cancel_all_sa(emp)
    night = frappe.get_doc(
        {
            "doctype": "Shift Assignment",
            "employee": emp,
            "shift_type": "Ca E2E Đêm",
            "start_date": _day_str(-3),
            "end_date": _day_str(-2),
            "status": "Active",
        }
    )
    night.insert(ignore_permissions=True)
    night.submit()
    day = frappe.get_doc(
        {
            "doctype": "Shift Assignment",
            "employee": emp,
            "shift_type": "Ca E2E LC Ngày",
            # T..T+2 (NOT T-1): the night shift's checkout window (planned_end
            # +60' = 10:00 next morning) touches a 10:00-start day SI's checkin
            # window → the SI overlap check silently drops the first day.
            "start_date": _day_str(0),
            "end_date": _day_str(2),
            "status": "Active",
        }
    )
    day.insert(ignore_permissions=True)
    day.submit()
    frappe.db.commit()
    print(f"ASSERT: LC_SHIFTS night={night.name} day={day.name}")


def lc_clear_logs_today(emp):
    """LC12 prep — drop today's checkins so the post-lock tap is a fresh IN
    that the period lock must reject (otherwise parity/dup guards answer first)."""
    d0 = _day_str(0)
    nxt = str(_today() + timedelta(days=1))
    n = 0
    for r in frappe.get_all(
        "Employee Checkin", filters={"employee": emp, "time": ["between", [d0, nxt]]}, fields=["name"]
    ):
        frappe.delete_doc("Employee Checkin", r.name, force=True, ignore_permissions=True)
        n += 1
    frappe.db.commit()
    print(f"ASSERT: LC_CLEARED_TODAY {n}")


def lc_seed_log(emp, log_type="IN", day_offset=-1, hour=8, minute=30):
    """Seed a checkin for the LC employee at a portal WALL time."""
    d = _today() + timedelta(days=day_offset)
    log = frappe.get_doc(
        {
            "doctype": "Employee Checkin",
            "employee": emp,
            "log_type": log_type,
            "time": datetime(d.year, d.month, d.day, hour, minute),
            "device_id": "e2e-lc",
        }
    )
    log.insert(ignore_permissions=True)
    frappe.db.commit()
    print(f"ASSERT: LC_SEED {log.name} {log_type} {day_offset:+d} {hour:02d}:{minute:02d}")


def lc_tickets(emp):
    """Dump the LC employee's checkout-miss tickets (T-4..T)."""
    rows = frappe.get_all(
        "VN Checkout Miss",
        filters={"employee": emp, "work_date": ["between", [_day_str(-4), _day_str(0)]]},
        fields=["name", "work_date", "status"],
        order_by="creation asc",
    )
    print("ASSERT: LC_TICKETS " + json.dumps([{k: str(v) for k, v in r.items()} for r in rows]))


def lc_lock_month(from_date, to_date=None, locked=1):
    """LC12 — lock (``locked=1``) or unlock the Monthly Attendance Period
    COVERING ``from_date``. FINDING-LC12: overlapping periods make the lock
    ambiguous (a wide Unlocked month + a narrow Locked slice both cover the
    day), so flip the WIDEST covering period — never create overlaps here."""
    covering = frappe.get_all(
        "VN Monthly Attendance Period",
        filters={"from_date": ["<=", from_date], "to_date": [">=", from_date]},
        fields=["name"],
        order_by="to_date desc",
        limit=1,
    )
    if covering:
        name = covering[0].name
        frappe.db.set_value(
            "VN Monthly Attendance Period",
            name,
            "status",
            "Locked" if locked else "Unlocked",
            update_modified=False,
        )
    else:
        doc = frappe.get_doc(
            {
                "doctype": "VN Monthly Attendance Period",
                "from_date": from_date,
                "to_date": to_date or from_date,
                "status": "Locked" if locked else "Unlocked",
            }
        )
        doc.insert(ignore_permissions=True)
        name = doc.name
    frappe.db.commit()
    print(f"ASSERT: LC_LOCKED {name} locked={int(locked)}")


def lc_payroll_finalize(period, emp=None):
    """LC14 — confirm ALL the period's lines (approve_review requires every
    line Confirmed/Approved), then approve → generate slips → publish.
    Assigns the LC employee's Salary Structure first (no slip without it)."""
    from gege_hr.gege_hr.api import payroll as pay_api

    frappe.set_user("Administrator")
    out = {}
    if emp:
        try:
            lc_assign_salary(emp)
        except Exception:
            pass
    for ln in frappe.get_all(
        "VN Payroll Review Line",
        filters={"payroll_review_period": period},
        fields=["name", "status"],
    ):
        if (ln.status or "") not in ("Confirmed", "Approved"):
            frappe.db.set_value(
                "VN Payroll Review Line", ln.name, "status", "Confirmed", update_modified=False
            )
    frappe.db.commit()
    out["approve"] = pay_api.approve_review(name=period)
    frappe.db.commit()
    out["slips"] = pay_api.generate_salary_slips(name=period)
    frappe.db.commit()
    out["publish"] = pay_api.publish_payslips(name=period)
    frappe.db.commit()
    print("ASSERT: LC_FINALIZE " + json.dumps(out, default=str)[:600])


def lc_payslips_as(email):
    """LC14 assert — call my_payslips AS the LC user; dump visible slips."""
    from gege_hr.gege_hr.api import payroll as pay_api

    frappe.set_user(email)
    try:
        rows = pay_api.my_payslips() or []
    finally:
        frappe.set_user("Administrator")
    frappe.db.commit()
    slim = [
        {k: str(r.get(k)) for k in ("name", "posting_date", "net_pay", "salary_slip", "period_name")}
        for r in rows
    ]
    print("ASSERT: LC_MYSLIPS " + json.dumps(slim, default=str))


def cleanup():
    _clean_e2e()
    print("ASSERT: CLEANED ok")


def leave_calendar_seed_cleanup():
    """Leave-calendar desk-free e2e (plans/plan-leave-calendar-desk-free):
    drop EVERY leftover seeded Leave Application (description like 'E2E-LC-%',
    any run). Submitted docs are cancelled first (mirror :func:`_lc_drop`)."""
    names = frappe.get_all("Leave Application", filters={"description": ["like", "E2E-LC-%"]}, pluck="name")
    deleted = 0
    for n in names:
        try:
            doc = frappe.get_doc("Leave Application", n)
            if doc.docstatus == 1:
                try:
                    doc.cancel()
                except Exception:
                    frappe.db.rollback()
            frappe.delete_doc("Leave Application", n, force=1, ignore_permissions=True, ignore_missing=True)
            deleted += 1
        except Exception:
            frappe.db.rollback()
    frappe.db.commit()
    print(f"ASSERT: LC_SEED_CLEANED deleted={deleted} total={len(names)}")
    return {"deleted": deleted, "total": len(names)}


def leave_status(name):
    """Leave-calendar e2e assert: raw-DB status of a Leave Application
    (frappe.client.get_value chokes on the permlevel-1 ``status`` field)."""
    st = frappe.db.get_value("Leave Application", name, "status")
    ds = frappe.db.get_value("Leave Application", name, "docstatus")
    print(f"ASSERT: LEAVE_STATUS {name} status={st} docstatus={ds}")
    return {"status": st, "docstatus": ds}


def calendar_as(user, company, year, month, statuses=None):
    """Debug probe (leave-calendar e2e): run get_leave_calendar AS ``user``
    (force refresh) to expose permission-filtered reads."""
    frappe.set_user(user)
    try:
        from gege_hr.gege_hr.api import leave_calendar as lc

        out = lc.get_leave_calendar(
            company=company, year=year, month=month, statuses=statuses, force_refresh=True
        )
    finally:
        frappe.set_user("Administrator")
    n = len((out.get("data") or {}).get("leaves") or [])
    print(f"ASSERT: CAL_AS user={user} leaves={n}")
    return {"user": user, "leaves": n}


def holiday_out_of_window(holiday_list, year, month):
    """Leave-calendar e2e: move every Holiday of ``holiday_list`` that falls
    inside (year, month) to 25/12 of the same year. HRMS apply validation
    rejects leave applications whose days are all holidays ("You need not
    apply for leave"), and the e2e seeds land in month+1 days 03–05 — the
    E2E holiday list must therefore stay clear of that window."""
    from datetime import date

    doc = frappe.get_doc("Holiday List", holiday_list)
    lo = date(int(year), int(month), 1)
    hi = date(int(year) + (1 if int(month) == 12 else 0), (int(month) % 12) + 1, 1)
    moved = 0
    for h in doc.get("holidays") or []:
        d = h.holiday_date.date() if hasattr(h.holiday_date, "date") else h.holiday_date
        if lo <= d < hi:
            h.holiday_date = date(int(year), 12, 25)
            moved += 1
    if moved:
        doc.save(ignore_permissions=True)
    frappe.db.commit()
    print(f"ASSERT: HL_OUT_OF_WINDOW moved={moved} list={holiday_list}")
    return {"moved": moved}


def shift_crud_smoke():
    """Shift Assignment full-CRUD smoke (plans/shift-assignment-frontend-crud §6).

    Exercises every new admin endpoint against the real site:
    create → get(can-matrix) → update(end_date) → amend(cancel+amended_from)
    → get(cancelled, can.delete) → bulk_end → delete ×2.
    Idempotent: scoped to EMP, future-dated spans, and everything it creates is
    deleted before returning.
    """
    from gege_hr.gege_hr.api import admin

    st = frappe.db.get_value("Shift Type", {"name": ["like", "E2E%"]}, "name") or frappe.db.get_value(
        "Shift Type", {}, "name"
    )
    assert st, "shift_crud_smoke: no Shift Type on site"
    frappe.only_for("System Manager")  # mirror the admin gate on the console user

    # A dedicated throw-away employee sidesteps every overlap/native-timing rule
    # (hrms throws OverlappingShifts even with allow_multiple on when timings
    # collide) and guarantees no linked Checkin/Attendance for cancel safety.
    company = frappe.db.get_value("Employee", {}, "company")
    emp_doc = frappe.get_doc(
        {
            "doctype": "Employee",
            "first_name": "E2E CRUD",
            "last_name": "Smoke",
            "company": company,
            "status": "Active",
            "gender": "Other",
            "date_of_birth": "1990-01-01",
            "date_of_joining": _day_str(-365),
        }
    )
    emp_doc.insert(ignore_permissions=True)
    emp = emp_doc.name
    print(f"ASSERT: CRUD_TMP_EMP {emp}")
    try:
        _shift_crud_smoke_body(admin, emp, st)
    finally:
        for sa in frappe.get_all("Shift Assignment", filters={"employee": emp}, fields=["name", "docstatus"]):
            try:
                if sa.docstatus == 1:
                    frappe.get_doc("Shift Assignment", sa.name).cancel()
                frappe.delete_doc("Shift Assignment", sa.name, force=True, ignore_permissions=True)
            except Exception:
                pass
        frappe.delete_doc("Employee", emp, force=True, ignore_permissions=True)
        frappe.db.commit()
        print(f"ASSERT: CRUD_TMP_CLEANED {emp}")


def _shift_crud_smoke_body(admin, emp, st):
    """Inner body of :func:`shift_crud_smoke` (runs with its own throw-away employee)."""

    s = _day_str(30)
    e = _day_str(40)
    doc = admin.create_shift_assignment(employee=emp, shift_type=st, start_date=s, end_date=e)
    n1 = doc["name"]
    print(f"ASSERT: CRUD_CREATE {n1}")

    d1 = admin.get_shift_assignment(n1)
    assert d1["docstatus"] == 1 and d1["can"]["edit_end_date"] and not d1["can"]["delete"]
    print("ASSERT: CRUD_GET_ACTIVE can=" + json.dumps(d1["can"]))

    up = admin.update_shift_assignment(n1, end_date=_day_str(45))
    assert str(up.get("end_date") or "") == _day_str(45)
    print("ASSERT: CRUD_UPDATE_END ok")

    am = admin.amend_shift_assignment(n1, start_date=_day_str(50), end_date=_day_str(60))
    n2 = am["name"]
    assert n2 and n2 != n1 and am["amended_from"] == n1
    old = frappe.db.get_value("Shift Assignment", n1, ["docstatus", "status", "end_date"], as_dict=True)
    assert old.docstatus == 2 and str(old.end_date) == _day_str(49), "amend must cut+cancel the old span"
    print(f"ASSERT: CRUD_AMEND {n1} -> {n2} (old cut to {old.end_date})")

    d1b = admin.get_shift_assignment(n1)
    assert d1b["display_status"] == "Cancelled" and d1b["can"]["delete"]
    print("ASSERT: CRUD_GET_CANCELLED can=" + json.dumps(d1b["can"]))

    be = admin.bulk_end_shift_assignments([n2], _day_str(59))
    assert be["ended"] == [n2] and not be["failed"], be
    print("ASSERT: CRUD_BULK_END " + json.dumps(be))

    admin.delete_shift_assignment(n2)
    admin.delete_shift_assignment(n1)
    assert not frappe.db.exists("Shift Assignment", n1)
    assert not frappe.db.exists("Shift Assignment", n2)
    frappe.db.commit()
    print("ASSERT: CRUD_DELETE_BOTH ok — smoke PASSED")


def schedule_ui_user():
    """Idempotent throw-away employee-linked user for the /hr/schedule BROWSER
    e2e (tests-e2e/schedule-deskfree.mjs part B). Creates/refreshes
    ``e2e.sched.ui@gege.test`` (pwd SchedE2e!234, role Employee) + a linked
    Active Employee with ``shift_request_approver`` set. Never touches real
    gegeteam users. ASSERT: UI_USER <email> <employee>."""
    email = "e2e.sched.ui@gege.test"
    if not frappe.db.exists("User", email):
        u = frappe.get_doc(
            {
                "doctype": "User",
                "email": email,
                "first_name": "E2E Sched UI",
                "enabled": 1,
                "new_password": "SchedE2e!234",
            }
        )
        u.insert(ignore_permissions=True)
        u.add_roles("Employee")
    else:
        frappe.set_value("User", email, "enabled", 1)
    emp = frappe.db.get_value("Employee", {"user_id": email}, "name")
    if not emp:
        company = frappe.db.get_value("Company", {}, "name")
        d = frappe.get_doc(
            {
                "doctype": "Employee",
                "__newname": "E2E-SCHED-UI",
                "first_name": "E2E",
                "last_name": "Sched UI",
                "gender": "Other",
                "date_of_birth": "1990-01-01",
                "date_of_joining": "2026-01-01",
                "status": "Active",
                "company": company,
                "user_id": email,
                "shift_request_approver": "Administrator",
            }
        )
        d.insert(ignore_permissions=True)
        emp = d.name
    frappe.db.commit()
    print(f"ASSERT: UI_USER {email} {emp}")


def schedule_deskfree_smoke():
    """Full-stack smoke for the desk-free /hr/schedule endpoints
    (plans/plan-schedule-desk-free.md §4.3) — run on the bench with THROW-AWAY
    data (own user + employee + shift types), exercising every new endpoint
    under a real employee session (``frappe.set_user``), then cleaning up.

    ASSERT lines: SCTX_CREATE / SREQ_DRAFT / SREQ_OVERLAP / SREQ_CANCEL /
    HARDEN_DENIED / APPROVE_LINK / OVERRIDE_SPLIT / CONFLICT_PREVIEW /
    SKIP_RESTORE / CLEANED.
    """
    from gege_hr.gege_hr.api import admin as admin_api, shift as shift_api

    ts = datetime.now().strftime("%H%M%S")
    email = f"e2e.sched{ts}@gege.test"
    day, night = f"E2E Sched Day {ts}", f"E2E Sched Night {ts}"
    d1, d2, d3 = _day_str(7), _day_str(8), _day_str(9)

    user = frappe.get_doc(
        {
            "doctype": "User",
            "email": email,
            "first_name": "E2E Sched",
            "enabled": 1,
            "new_password": "SchedE2e!234",
        }
    )
    user.insert(ignore_permissions=True)
    user.add_roles("Employee")
    company = frappe.db.get_value("Company", {}, "name")
    for st_name, s, e in ((day, "09:00", "17:00"), (night, "18:00", "22:00")):
        frappe.get_doc(
            {
                "doctype": "Shift Type",
                "__newname": st_name,
                "start_time": s,
                "end_time": e,
            }
        ).insert(ignore_permissions=True)
    emp_doc = frappe.get_doc(
        {
            "doctype": "Employee",
            "__newname": f"E2E-SCHED-{ts}",
            "first_name": "E2E",
            "last_name": f"Sched {ts}",
            "gender": "Other",
            "date_of_birth": "1990-01-01",
            "date_of_joining": "2026-01-01",
            "status": "Active",
            "company": company,
            "user_id": email,
            "shift_request_approver": "Administrator",
        }
    )
    emp_doc.insert(ignore_permissions=True)
    emp = emp_doc.name
    try:
        # ── Employee session: context + self-service Shift Request ──────────
        frappe.set_user(email)
        ctx = shift_api.schedule_context()
        assert ctx["viewer_employee"] == emp
        assert ctx["can"] == {"create_shift_request": True, "manage_schedule": False}
        assert ctx["approver"] and ctx["approver"]["user"]
        print("ASSERT: SCTX_CREATE " + json.dumps(ctx["can"]))

        res = shift_api.create_my_shift_request(
            shift_type=night, from_date=d1, to_date=d1, reason="e2e desk-free"
        )
        req_name = res["name"]
        rows = shift_api.my_shift_requests()
        assert any(r["name"] == req_name and r["status"] == "Draft" for r in rows)
        assert shift_api.schedule_context()["my_requests"]["pending"] == 1
        print(f"ASSERT: SREQ_DRAFT {req_name}")

        try:
            shift_api.create_my_shift_request(shift_type=night, from_date=d1, to_date=d1)
            raise AssertionError("overlap draft must be blocked")
        except Exception as exc:
            assert "trùng" in str(exc).lower(), str(exc)
        print("ASSERT: SREQ_OVERLAP draft-vs-draft blocked")

        shift_api.cancel_my_shift_request(req_name)
        assert not frappe.db.exists("Shift Request", req_name)
        assert shift_api.schedule_context()["my_requests"]["pending"] == 0
        print("ASSERT: SREQ_CANCEL deleted")

        # ── Hardening: another employee's schedule needs a manager role ─────
        other = frappe.db.get_value("Employee", {"status": "Active", "name": ["!=", emp]}, "name")
        if other:
            try:
                shift_api.my_schedule(employee=other)
                raise AssertionError("employee must not read another's schedule")
            except Exception as exc:
                assert "quyền" in str(exc).lower() or "Permission" in str(exc), str(exc)
            print(f"ASSERT: HARDEN_DENIED other={other}")

        # ── HR (Administrator): assign + approve-back-link + override day ────
        frappe.set_user("Administrator")
        sa = admin_api.create_shift_assignment(employee=emp, shift_type=day, start_date=d1, end_date=d3)
        sa_name = sa["name"]

        frappe.set_user(email)
        req2 = shift_api.create_my_shift_request(shift_type=night, from_date=_day_str(12))["name"]
        frappe.set_user("Administrator")
        try:
            admin_api.approve_shift_request(req2)
            linked = frappe.db.get_value("Shift Assignment", {"shift_request": req2}, "name")
            assert linked
            frappe.set_user(email)
            rows = shift_api.my_shift_requests()
            row2 = next(r for r in rows if r["name"] == req2)
            assert row2["status"] == "Approved" and row2["shift_assignment"] == linked
            print(f"ASSERT: APPROVE_LINK {req2} -> {linked}")
        finally:
            frappe.set_user("Administrator")

        ov = admin_api.override_day_shift_assignment(employee=emp, date=d2, shift_type=night)
        cut_end = frappe.db.get_value("Shift Assignment", sa_name, "end_date")
        assert str(cut_end) == _day_str(7) and str(_day_str(8)) == d2, (cut_end, d2)
        assert len(ov["created"]) == 2 and ov["adjusted"] == [sa_name]
        inst = frappe.db.get_value("VN Employee Shift Instance", {"employee": emp, "work_date": d2}, "name")
        assert inst, "override must backfill the day's instance immediately"
        print(f"ASSERT: OVERRIDE_SPLIT created={ov['created']} instance={inst}")

        conflicts = admin_api.check_schedule_conflicts(employee=emp, shift_type=day, from_date=d3, to_date=d3)
        assert any(c["type"] == "shift_assignment" for c in conflicts), conflicts
        print("ASSERT: CONFLICT_PREVIEW " + json.dumps(conflicts[:2]))

        flipped = shift_api.set_shift_instance_status(inst, "Skipped")
        assert flipped["status"] == "Skipped"
        restored = shift_api.set_shift_instance_status(inst, "Scheduled")
        assert restored["status"] == "Scheduled"
        print("ASSERT: SKIP_RESTORE ok")

        # ── /hr/team/schedule grid (plans/plan-team-schedule-desk-free.md §5.3) ─
        tctx = shift_api.team_schedule_context(d1, d3)
        assert tctx["can"]["view_grid"] and tctx["can"]["assign"]
        assert tctx["scope"]["member_count"] >= 1
        grid = shift_api.team_schedule_grid(d1, d3)
        assert grid["members"] and all(m["days"] for m in grid["members"])
        cell = grid["members"][0]["days"][0]
        assert {"assign", "override", "skip", "approve"} <= set(cell["can"])
        frappe.set_user(email)
        try:
            shift_api.team_schedule_context()
            raise AssertionError("employee must not open the team schedule")
        except AssertionError:
            raise
        except Exception as exc:
            assert "quyền" in str(exc).lower() or "Permission" in str(exc), str(exc)
        print(
            f"ASSERT: TEAM_GRID members={len(grid['members'])} window={grid['from_date']}..{grid['to_date']}"
        )
    finally:
        frappe.set_user("Administrator")
        for sr in frappe.get_all("Shift Request", filters={"employee": emp}, pluck="name"):
            try:
                frappe.delete_doc("Shift Request", sr, force=True, ignore_permissions=True)
            except Exception:
                pass
        for sa_row in frappe.get_all(
            "Shift Assignment", filters={"employee": emp}, fields=["name", "docstatus"]
        ):
            try:
                if sa_row.docstatus == 1:
                    frappe.get_doc("Shift Assignment", sa_row.name).cancel()
                frappe.delete_doc("Shift Assignment", sa_row.name, force=True, ignore_permissions=True)
            except Exception:
                pass
        for si in frappe.get_all("VN Employee Shift Instance", filters={"employee": emp}, pluck="name"):
            try:
                frappe.delete_doc("VN Employee Shift Instance", si, force=True, ignore_permissions=True)
            except Exception:
                pass
        for st_name in (day, night):
            try:
                frappe.delete_doc("Shift Type", st_name, force=True, ignore_permissions=True)
            except Exception:
                pass
        try:
            frappe.delete_doc("Employee", emp, force=True, ignore_permissions=True)
            frappe.delete_doc("User", email, force=True, ignore_permissions=True)
        except Exception:
            pass
        frappe.db.commit()
        print("ASSERT: CLEANED")


def team_attendance_deskfree_smoke():
    """plans/plan-team-attendance-desk-free.md §5.3 — desk-free grid smoke.

    READ-ONLY on the live site (mutations are covered bench-free by
    tests/test_team_attendance_deskfree.py TA13–TA22): context payload, the
    enhanced grid (can matrix + locked_dates + pending badges), a past-date
    member-day detail and the CSV export — all as the first HR Manager found.
    Prints ``ASSERT:`` lines for the bench wrapper.
    """
    from gege_hr.gege_hr.api import attendance as att
    from gege_hr.gege_hr.utils import tz

    hr_users = frappe.get_all(
        "Has Role",
        filters={"role": "HR Manager", "parenttype": "User"},
        pluck="parent",
        limit=1,
    )
    if not hr_users:
        print("ASSERT: no HR Manager user on this site — smoke skipped")
        return
    frappe.set_user(hr_users[0])

    today = tz.now_in_portal().date()
    start, end = today.replace(day=1), today

    ctx = att.team_attendance_context(from_date=str(start), to_date=str(end))
    assert ctx["can"]["view_grid"] is True
    assert ctx["scope"]["mode"] == "company"
    assert set(ctx["pending_approvals"]) == {"corrections", "overtime", "leaves", "checkout_misses"}
    print(f"ASSERT: context scope={ctx['scope']} can.fix_punch={ctx['can']['fix_punch']}")

    grid = att.team_attendance(manager="", from_date=str(start), to_date=str(end))
    assert "locked_dates" in grid and "period" in grid and "total_members" in grid
    members = grid.get("members") or []
    assert members, "no roster members for the current month"
    m0 = members[0]
    assert m0.get("pending") is not None
    d0 = next((d for d in m0["days"] if d.get("can")), None)
    assert d0 is not None
    assert set(d0["can"]) >= {"fix_punch", "mark_attendance", "approve_ot", "nudge"}
    print(f"ASSERT: grid members={len(members)} first={m0['name']} can_keys={len(d0['can'])}")

    past = next((d for d in reversed(m0["days"]) if d["work_date"] < str(today)), None)
    if past:
        detail = att.team_member_day_detail(employee=m0["name"], date_str=past["work_date"])
        assert detail["can"]["view_detail"] is True
        print(f"ASSERT: day-detail {m0['name']}@{past['work_date']} status={detail.get('status')}")

    csv_res = att.team_attendance_export_csv(from_date=str(start), to_date=str(end))
    assert csv_res["csv"].startswith("\ufeff")
    assert csv_res["rows"] >= len(members)
    print(f"ASSERT: export rows={csv_res['rows']} filename={csv_res['filename']}")
    print("ASSERT: team_attendance_deskfree_smoke OK")
