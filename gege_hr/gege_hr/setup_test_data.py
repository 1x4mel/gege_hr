"""Test-dataset seeder for gege_hr — multi-role HR frontend test campaign.

Creates a **deterministic, idempotent** set of test accounts + master data so the
HR portal (`/hr/*`) can be exercised interactively by multiple roles (see
``plans/hr-frontend-test-plan.md`` §2 and ``plans/hr-frontend-test-checklist.md``
§S1/S2).

Run (as the bench user)::

    # S1 — accounts + employees + master data (shift types, holiday list, allocations)
    bench --site <site> execute gege_hr.gege_hr.setup_test_data.create_test_dataset

    # S2 — realistic attendance (current month + June-2026) for the test employee
    bench --site <site> execute gege_hr.gege_hr.setup_test_data.seed_attendance

Design mirrors the proven pattern of ``setup_demo.py`` / ``seed_june2026.py``:

* every creator is **idempotent** (skips when the record already exists),
* every write is ``ignore_permissions`` + ``ignore_mandatory`` (seeds run as
  Administrator and must not abort on optional HRMS validation),
* failures are ``log_error`` + swallowed so the command always returns a report.

Reuses the real company ``GeGe Esport`` and the standard Leave Types that already
exist on a migrated site (no demo company is created).
"""

from __future__ import annotations

import datetime
from typing import Any

try:  # bench-free import safety (unit tests run outside a Frappe site)
    import frappe  # type: ignore
except Exception:  # pragma: no cover
    frappe = None  # type: ignore

# --------------------------------------------------------------------------- #
# Constants — centralised so re-runs match exactly.
# --------------------------------------------------------------------------- #
COMPANY = "GeGe Esport"

# (email, first, last, gender, password, [roles], needs_employee)
# 6 role-specific accounts. Passwords satisfy typical strength policy
# (≥8 chars, upper+lower+digit+symbol).
TEST_ACCOUNTS = [
    (
        "hr.employee@gege.test",
        "Nhân Viên",
        "Test",
        "Female",
        "Emp12345!",
        ["Employee"],
        True,
    ),
    (
        "hr.manager@gege.test",
        "Quản Lý",
        "Test",
        "Male",
        "Mgr12345!",
        ["Line Manager"],
        True,
    ),
    (
        "hr.user@gege.test",
        "HR",
        "User",
        "Male",
        "Hru12345!",
        ["HR User"],
        True,
    ),
    (
        "hr.manager2@gege.test",
        "HR",
        "Manager",
        "Female",
        "Hrm12345!",
        ["HR Manager"],
        True,
    ),
    (
        "payroll.user@gege.test",
        "Payroll",
        "User",
        "Male",
        "Pau12345!",
        ["Payroll User"],
        False,
    ),
    (
        "payroll.manager@gege.test",
        "Payroll",
        "Manager",
        "Female",
        "Pam12345!",
        ["Payroll Manager"],
        False,
    ),
]

# Employee linkage plan: each key is the account email, value = reports_to account
# email (the Line Manager / HR Manager the employee reports to).
REPORTS_TO = {
    "hr.employee@gege.test": "hr.manager@gege.test",
    "hr.manager@gege.test": "hr.manager2@gege.test",
    "hr.user@gege.test": "hr.manager2@gege.test",
    "hr.manager2@gege.test": None,
}

DEFAULT_SHIFT_BY_EMAIL = {
    "hr.employee@gege.test": "Ca Hành Chính",
    "hr.manager@gege.test": "Ca Sáng",
    "hr.user@gege.test": "Ca Hành Chính",
    "hr.manager2@gege.test": "Ca Hành Chính",
}

# Shift Types: (name, start_time, end_time). Times are "HH:MM:SS".
SHIFT_TYPES = [
    ("Ca Hành Chính", "08:00:00", "17:00:00"),
    ("Ca Sáng", "06:00:00", "14:00:00"),
    ("Ca Chiều", "14:00:00", "22:00:00"),
    ("Ca Đêm", "22:00:00", "06:00:00"),
]

# Vietnamese public holidays 2026 (name, date) — enough to exercise Holiday List.
HOLIDAY_LIST_NAME = "Lễ VN 2026"
HOLIDAYS_2026 = [
    ("Tết Dương lịch", "2026-01-01"),
    ("Tết Nguyên Đán", "2026-02-17"),
    ("Tết Nguyên Đán", "2026-02-18"),
    ("Tết Nguyên Đán", "2026-02-19"),
    ("Tết Nguyên Đán", "2026-02-20"),
    ("Giỗ tổ Hùng Vương", "2026-03-31"),
    ("Giải phóng miền Nam", "2026-04-30"),
    ("Quốc tế Lao động", "2026-05-01"),
    ("Quốc khánh", "2026-09-02"),
    ("Quốc khánh", "2026-09-03"),
]

# Attendance scenario employee + month for S2.
ATT_EMP_EMAIL = "hr.employee@gege.test"
ATT_MONTHS = [(2026, 6)]  # June 2026 — matches the seed_june2026 spec


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _ok(name: str) -> bool:
    return bool(frappe.db.exists("DocType", name))


def _exists(dt: str, name: str) -> bool:
    try:
        return bool(frappe.db.exists(dt, name))
    except Exception:
        return False


def _log(msg: str) -> None:
    if frappe:
        frappe.log_error(msg)


# --------------------------------------------------------------------------- #
# S1.1 — Users + roles + password
# --------------------------------------------------------------------------- #
def _ensure_user(email: str, first: str, last: str, gender: str, pwd: str, roles: list[str]) -> bool:
    """Create (or update roles/password of) a test User. Idempotent."""
    if not _ok("User"):
        return False
    try:
        if _exists("User", email):
            # Re-apply roles + password so a re-run fixes a half-provisioned user.
            doc = frappe.get_doc("User", email)
            doc.flags.ignore_permissions = True
            doc.flags.ignore_mandatory = True
            doc.set("roles", [{"role": r, "doctype": "Has Role"} for r in roles])
            # new_password triggers a re-hash on save.
            try:
                doc.new_password = pwd
            except Exception:
                pass
            doc.save(ignore_permissions=True)
            return True
        doc = frappe.get_doc(
            {
                "doctype": "User",
                "email": email,
                "first_name": first,
                "last_name": last,
                "gender": gender,
                "send_welcome_email": 0,
                "enabled": 1,
                "new_password": pwd,
                "roles": [{"role": r, "doctype": "Has Role"} for r in roles],
            }
        )
        doc.flags.ignore_permissions = True
        doc.flags.ignore_mandatory = True
        doc.insert(ignore_permissions=True)
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"setup_test_data: failed to create/update User {email}: {exc}")
        return False


# --------------------------------------------------------------------------- #
# S1.1 — Employees (linked to user_id)
# --------------------------------------------------------------------------- #
def _ensure_employee(email: str, first: str, last: str, gender: str) -> str | None:
    """Return the Employee name for ``email``'s user_id, creating it if missing."""
    if not _ok("Employee"):
        return None
    try:
        rows = frappe.get_all("Employee", filters={"user_id": email}, pluck="name", limit=1)
    except Exception:
        rows = []
    if rows:
        return rows[0]
    try:
        doc = frappe.get_doc(
            {
                "doctype": "Employee",
                "naming_series": "HR-EMP-",
                "first_name": first,
                "last_name": last,
                "company": COMPANY,
                "user_id": email,
                "personal_email": email,
                "prefered_email": email,
                "status": "Active",
                "gender": gender,
                "date_of_birth": "1992-01-01",
                "date_of_joining": "2024-01-01",
            }
        )
        doc.flags.ignore_permissions = True
        doc.flags.ignore_mandatory = True
        doc.insert(ignore_permissions=True)
        return doc.name
    except Exception as exc:  # noqa: BLE001
        _log(f"setup_test_data: failed to create Employee for {email}: {exc}")
        return None


# --------------------------------------------------------------------------- #
# S1.2 — Master data
# --------------------------------------------------------------------------- #
def _ensure_shift_types() -> list[str]:
    if not _ok("Shift Type"):
        return []
    created: list[str] = []
    for name, start, end in SHIFT_TYPES:
        if _exists("Shift Type", name):
            created.append(name)
            continue
        try:
            doc = frappe.get_doc(
                {
                    "doctype": "Shift Type",
                    "name": name,
                    "start_time": start,
                    "end_time": end,
                }
            )
            doc.flags.ignore_permissions = True
            doc.flags.ignore_mandatory = True
            doc.insert(ignore_permissions=True)
            created.append(name)
        except Exception as exc:  # noqa: BLE001
            _log(f"setup_test_data: failed to create Shift Type {name}: {exc}")
    return created


def _ensure_holiday_list() -> str | None:
    if not _ok("Holiday List"):
        return None
    try:
        if _exists("Holiday List", HOLIDAY_LIST_NAME):
            return HOLIDAY_LIST_NAME
        doc = frappe.get_doc(
            {
                "doctype": "Holiday List",
                "holiday_list_name": HOLIDAY_LIST_NAME,
                "from_date": "2026-01-01",
                "to_date": "2026-12-31",
                "holidays": [
                    {"holiday_date": d, "description": n} for n, d in HOLIDAYS_2026
                ],
            }
        )
        doc.flags.ignore_permissions = True
        doc.flags.ignore_mandatory = True
        doc.insert(ignore_permissions=True)
        return doc.name
    except Exception as exc:  # noqa: BLE001
        _log(f"setup_test_data: failed to create Holiday List: {exc}")
        return None


def _ensure_leave_allocation(employee: str, leave_type: str, days: float) -> bool:
    if not _ok("Leave Allocation") or not employee or not _exists("Leave Type", leave_type):
        return False
    year = datetime.date.today().year
    from_date = f"{year}-01-01"
    to_date = f"{year}-12-31"
    try:
        existing = frappe.get_all(
            "Leave Allocation",
            filters={
                "employee": employee,
                "leave_type": leave_type,
                "from_date": from_date,
                "to_date": to_date,
                "docstatus": 1,
            },
            pluck="name",
            limit=1,
        )
    except Exception:
        existing = []
    if existing:
        return False
    try:
        doc = frappe.get_doc(
            {
                "doctype": "Leave Allocation",
                "employee": employee,
                "leave_type": leave_type,
                "from_date": from_date,
                "to_date": to_date,
                "new_leaves_allocated": days,
            }
        )
        doc.flags.ignore_permissions = True
        doc.flags.ignore_mandatory = True
        doc.insert(ignore_permissions=True)
        doc.submit()
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"setup_test_data: failed Leave Allocation {employee}/{leave_type}: {exc}")
        return False


def _ensure_shift_assignment(employee: str, shift_type: str) -> bool:
    if not _ok("Shift Assignment") or not employee or not _exists("Shift Type", shift_type):
        return False
    try:
        existing = frappe.get_all(
            "Shift Assignment",
            filters={"employee": employee, "shift_type": shift_type, "docstatus": 1},
            pluck="name",
            limit=1,
        )
    except Exception:
        existing = []
    if existing:
        return False
    try:
        doc = frappe.get_doc(
            {
                "doctype": "Shift Assignment",
                "employee": employee,
                "shift_type": shift_type,
                "start_date": "2024-01-01",
                "status": "Active",
                "company": COMPANY,
            }
        )
        doc.flags.ignore_permissions = True
        doc.flags.ignore_mandatory = True
        doc.insert(ignore_permissions=True)
        doc.submit()
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"setup_test_data: failed Shift Assignment {employee}/{shift_type}: {exc}")
        return False


# --------------------------------------------------------------------------- #
# S1 entry point
# --------------------------------------------------------------------------- #
def create_test_dataset() -> dict[str, Any]:
    """Create accounts + employees + master data (idempotent). Returns a report."""
    report: dict[str, Any] = {"users": [], "employees": {}, "shift_types": [], "holiday_list": None}

    # Users
    for email, first, last, gender, pwd, roles, _needs_emp in TEST_ACCOUNTS:
        ok = _ensure_user(email, first, last, gender, pwd, roles)
        report["users"].append({"email": email, "roles": roles, "ok": ok})

    # Employees (for the 4 accounts that need one) — collect name by email.
    emp_by_email: dict[str, str] = {}
    for email, first, last, gender, _pwd, _roles, needs_emp in TEST_ACCOUNTS:
        if not needs_emp:
            continue
        name = _ensure_employee(email, first, last, gender)
        if name:
            emp_by_email[email] = name
    report["employees"] = emp_by_email

    # reports_to + default_shift (second pass, now that all employees exist).
    for email, manager_email in REPORTS_TO.items():
        emp = emp_by_email.get(email)
        if not emp:
            continue
        reports_to = emp_by_email.get(manager_email) if manager_email else None
        shift = DEFAULT_SHIFT_BY_EMAIL.get(email)
        try:
            if reports_to:
                frappe.db.set_value("Employee", emp, "reports_to", reports_to, update_modified=False)
            if shift and _exists("Shift Type", shift):
                frappe.db.set_value("Employee", emp, "default_shift", shift, update_modified=False)
        except Exception as exc:  # noqa: BLE001
            _log(f"setup_test_data: link reports_to/shift for {emp}: {exc}")

    # Master data
    report["shift_types"] = _ensure_shift_types()
    report["holiday_list"] = _ensure_holiday_list()

    # Leave allocations + shift assignments for the test employees.
    report["leave_allocations"] = []
    report["shift_assignments"] = []
    for email in emp_by_email:
        emp = emp_by_email[email]
        report["leave_allocations"].append(
            {"employee": emp, "casual": _ensure_leave_allocation(emp, "Casual Leave", 12.0)}
        )
        report["leave_allocations"].append(
            {"employee": emp, "sick": _ensure_leave_allocation(emp, "Sick Leave", 12.0)}
        )
        shift = DEFAULT_SHIFT_BY_EMAIL.get(email)
        if shift:
            report["shift_assignments"].append(
                {"employee": emp, "shift": shift, "ok": _ensure_shift_assignment(emp, shift)}
            )

    frappe.db.commit()
    return report


# --------------------------------------------------------------------------- #
# S2 — Attendance (current month + June-2026) for the test employee
# --------------------------------------------------------------------------- #
ATT_PLAN = (
    ["present_normal"] * 12
    + ["present_late"] * 3
    + ["present_early"] * 2
    + ["halfday"] * 1
    + ["leave"] * 1
    + ["absent"] * 3
)  # 22 weekdays — matches seed_june2026 spec


def _weekdays(year: int, month: int) -> list[datetime.date]:
    days: list[datetime.date] = []
    d = datetime.date(year, month, 1)
    while d.month == month:
        if d.weekday() < 5:
            days.append(d)
        d += datetime.timedelta(days=1)
    return days


def _attendance_row(day: datetime.date, kind: str) -> dict[str, Any]:
    """Return the forced column values for a given attendance kind."""
    if kind == "present_normal":
        return {"status": "Present", "in": "08:02:00", "out": "17:18:00", "late": 0, "early": 0, "wh": 9.27}
    if kind == "present_late":
        return {"status": "Present", "in": "08:31:00", "out": "17:25:00", "late": 1, "early": 0, "wh": 8.90}
    if kind == "present_early":
        return {"status": "Present", "in": "07:58:00", "out": "16:12:00", "late": 0, "early": 1, "wh": 8.23}
    if kind == "halfday":
        return {"status": "Half Day", "in": "08:05:00", "out": "12:10:00", "late": 0, "early": 0, "wh": 4.08}
    if kind == "leave":
        return {"status": "On Leave", "in": None, "out": None, "late": 0, "early": 0, "wh": 0.0}
    return {"status": "Absent", "in": None, "out": None, "late": 0, "early": 0, "wh": 0.0}


def seed_attendance() -> dict[str, Any]:
    """Seed realistic attendance for the test employee across configured months.

    Mirrors ``seed_june2026.run``: insert as Absent (always validates) → submit →
    force the real columns via ``db.set_value`` so HRMS validation cannot override.
    Idempotent (wipes the employee's existing rows for the month first).
    """
    if not frappe:
        return {}
    emp = frappe.db.get_value("Employee", {"user_id": ATT_EMP_EMAIL}, "name")
    if not emp:
        return {"error": f"no employee for {ATT_EMP_EMAIL} — run create_test_dataset first"}
    shift = DEFAULT_SHIFT_BY_EMAIL.get(ATT_EMP_EMAIL)

    created: list[dict[str, Any]] = []
    for year, month in ATT_MONTHS:
        start = datetime.date(year, month, 1)
        end = datetime.date(year, month, 28) if month != 2 else datetime.date(year, month, 28)
        # wipe existing rows for this employee/month
        try:
            old = frappe.get_all(
                "Attendance",
                {"employee": emp, "attendance_date": ["between", [start, end]]},
                pluck="name",
            )
            for name in old:
                try:
                    frappe.delete_doc("Attendance", name, force=True, ignore_permissions=True)
                except Exception:
                    pass
        except Exception as exc:  # noqa: BLE001
            _log(f"setup_test_data: wipe attendance {emp}/{year}-{month}: {exc}")

        days = _weekdays(year, month)
        plan = list(ATT_PLAN)
        # trim/pad plan to the actual weekday count for the month
        if len(plan) > len(days):
            plan = plan[: len(days)]
        while len(plan) < len(days):
            plan.append("present_normal")

        for day, kind in zip(days, plan):
            row = _attendance_row(day, kind)
            try:
                doc = frappe.get_doc(
                    {
                        "doctype": "Attendance",
                        "employee": emp,
                        "attendance_date": day.isoformat(),
                        "company": COMPANY,
                        "shift": shift,
                        "status": "Absent",
                    }
                )
                doc.flags.ignore_permissions = True
                doc.insert(ignore_permissions=True)
                doc.submit()
                in_dt = f"{day} {row['in']}" if row["in"] else None
                out_dt = f"{day} {row['out']}" if row["out"] else None
                frappe.db.set_value(
                    "Attendance",
                    doc.name,
                    {
                        "status": row["status"],
                        "in_time": in_dt,
                        "out_time": out_dt,
                        "late_entry": row["late"],
                        "early_exit": row["early"],
                        "working_hours": row["wh"],
                    },
                    update_modified=False,
                )
                created.append({"day": str(day), "kind": kind, "status": row["status"]})
            except Exception as exc:  # noqa: BLE001
                _log(f"setup_test_data: attendance {emp} {day} ({kind}): {exc}")

    frappe.db.commit()
    return {"employee": emp, "months": ATT_MONTHS, "created": created}


# --------------------------------------------------------------------------- #
# S2-extend — Leave Applications (pending + approved) for the test employee.
# --------------------------------------------------------------------------- #
def _leave_application(
    employee: str, leave_type: str, from_date: str, to_date: str, status: str
) -> str | None:
    """Insert + submit a Leave Application, forcing ``status`` afterwards.

    Mirrors the Attendance seed pattern: Frappe HR validation runs, but the final
    status is forced via ``db.set_value`` so HRMS overrides cannot mask the
    intended test state (Open → ApprovalInbox; Approved → Leave list + calendar).
    """
    try:
        existing = frappe.db.get_all(
            "Leave Application",
            filters={
                "employee": employee,
                "leave_type": leave_type,
                "from_date": from_date,
                "to_date": to_date,
                "docstatus": 1,
            },
            pluck="name",
            limit=1,
        )
        if existing:
            return existing[0]
    except Exception:
        pass
    try:
        doc = frappe.get_doc(
            {
                "doctype": "Leave Application",
                "employee": employee,
                "leave_type": leave_type,
                "from_date": from_date,
                "to_date": to_date,
                "posting_date": from_date,
                "description": f"Seed ({status}) — setup_test_data",
                "company": COMPANY,
            }
        )
        doc.flags.ignore_permissions = True
        doc.flags.ignore_mandatory = True
        doc.insert(ignore_permissions=True)
        try:
            doc.submit()
        except Exception:
            frappe.log_error(title=f"seed Leave Application submit ({status})")
        frappe.db.set_value("Leave Application", doc.name, "status", status, update_modified=False)
        return doc.name
    except Exception as exc:  # noqa: BLE001
        _log(f"setup_test_data: Leave Application {employee}/{leave_type}: {exc}")
        return None


def seed_leave_applications() -> dict[str, Any]:
    """Seed 1 pending (Casual) + 1 approved (Sick) Leave Application for the test employee."""
    if not frappe:
        return {}
    emp = frappe.db.get_value("Employee", {"user_id": ATT_EMP_EMAIL}, "name")
    if not emp:
        return {"error": f"no employee for {ATT_EMP_EMAIL}"}
    today = datetime.date.today()
    # pending: 2 days a couple weeks ahead (Casual); approved: 1 day last week (Sick)
    pending_from = (today + datetime.timedelta(days=14)).isoformat()
    pending_to = (today + datetime.timedelta(days=15)).isoformat()
    approved_from = (today - datetime.timedelta(days=7)).isoformat()
    approved_to = approved_from
    result = {
        "employee": emp,
        "pending": _leave_application(emp, "Casual Leave", pending_from, pending_to, "Open"),
        "approved": _leave_application(emp, "Sick Leave", approved_from, approved_to, "Approved"),
    }
    frappe.db.commit()
    return result


# --------------------------------------------------------------------------- #
# S2-extend — VN Monthly Attendance Period (current month) so Payroll/Lock views
# have a period. Uses gege_hr's own generator (aggregates VN Work Sessions).
# --------------------------------------------------------------------------- #
def seed_attendance_period() -> dict[str, Any]:
    """Create + generate the current-month VN Monthly Attendance Period."""
    if not frappe:
        return {}
    try:
        from gege_hr.gege_hr.api import attendance_period as ap
    except Exception as exc:  # noqa: BLE001
        return {"error": f"attendance_period import failed: {exc}"}
    today = datetime.date.today()
    month_name = f"{today.month:02d}"  # "08" — vn_monthly_attendance_period validates f"{start.month:02d}"
    from_date = today.replace(day=1).isoformat()
    # last day of month
    if today.month == 12:
        last = today.replace(day=31)
    else:
        last = today.replace(month=today.month + 1, day=1) - datetime.timedelta(days=1)
    to_date = last.isoformat()
    try:
        res = ap.generate_monthly_period(
            company=COMPANY,
            payroll_month=month_name,
            payroll_year=today.year,
            from_date=from_date,
            to_date=to_date,
        )
        return {"month": month_name, "year": today.year, "result": str(res)}
    except Exception as exc:  # noqa: BLE001
        _log(f"setup_test_data: generate_monthly_period {month_name}/{today.year}: {exc}")
        return {"error": f"{exc}"}


# --------------------------------------------------------------------------- #
# S2-extend — VN Overtime + Correction requests (drafts) for the test employee.
# my_overtime_requests / my_correction_requests filter by employee+date only (no
# workflow_state filter), so draft (docstatus 0) rows surface in the UI lists.
# Drafts avoid the submit-time OT-window/hours validation, so seeding is reliable.
# --------------------------------------------------------------------------- #
def _latest_work_session(employee: str) -> dict[str, Any] | None:
    try:
        rows = frappe.db.get_all(
            "VN Attendance Work Session",
            filters={"employee": employee},
            fields=["name", "work_date", "shift_instance", "shift_type"],
            order_by="work_date desc",
            limit=1,
        )
    except Exception:
        return None
    return rows[0] if rows else None


def seed_requests() -> dict[str, Any]:
    """Seed 1 VN Overtime + 1 VN Attendance Correction draft for the test employee."""
    if not frappe:
        return {}
    emp_row = frappe.db.get_value("Employee", {"user_id": ATT_EMP_EMAIL}, ["name", "employee_name"])
    if not emp_row:
        return {"error": f"no employee for {ATT_EMP_EMAIL}"}
    emp, emp_name = emp_row
    ws = _latest_work_session(emp)
    out: dict[str, Any] = {"employee": emp, "work_session": ws["name"] if ws else None, "overtime": None, "correction": None}
    if not ws:
        out["error"] = "no work session — run generate_shift_instances + check-in first"
        return out
    work_date = str(ws["work_date"])
    shift_instance = ws.get("shift_instance")

    # Overtime (Post-shift, 1.5h)
    try:
        if not frappe.db.exists(
            "VN Overtime Request", {"employee": emp, "work_date": work_date, "overtime_type": "Post-shift"}
        ):
            doc = frappe.get_doc(
                {
                    "doctype": "VN Overtime Request",
                    "employee": emp,
                    "employee_name": emp_name,
                    "work_date": work_date,
                    "shift_instance": shift_instance,
                    "work_session": ws["name"],
                    "company": COMPANY,
                    "overtime_type": "Post-shift",
                    "from_datetime": f"{work_date} 17:00:00",
                    "to_datetime": f"{work_date} 18:30:00",
                    "requested_hours": 1.5,
                    "reason": "Seed OT — setup_test_data",
                }
            )
            doc.flags.ignore_permissions = True
            doc.flags.ignore_mandatory = True
            doc.insert(ignore_permissions=True)
            out["overtime"] = doc.name
    except Exception as exc:  # noqa: BLE001
        _log(f"setup_test_data: VN Overtime Request: {exc}")
        out["overtime_error"] = str(exc)

    # Correction (Wrong Time)
    try:
        if not frappe.db.exists(
            "VN Attendance Correction Request",
            {"employee": emp, "work_date": work_date, "correction_type": "Wrong Time"},
        ):
            doc = frappe.get_doc(
                {
                    "doctype": "VN Attendance Correction Request",
                    "employee": emp,
                    "employee_name": emp_name,
                    "work_date": work_date,
                    "shift_instance": shift_instance,
                    "work_session": ws["name"],
                    "company": COMPANY,
                    "correction_type": "Wrong Time",
                    "current_checkin_time": f"{work_date} 08:30:00",
                    "requested_checkin_time": f"{work_date} 08:02:00",
                    "reason": "Seed correction — setup_test_data",
                }
            )
            doc.flags.ignore_permissions = True
            doc.flags.ignore_mandatory = True
            doc.insert(ignore_permissions=True)
            out["correction"] = doc.name
    except Exception as exc:  # noqa: BLE001
        _log(f"setup_test_data: VN Correction Request: {exc}")
        out["correction_error"] = str(exc)

    frappe.db.commit()
    return out


# --------------------------------------------------------------------------- #
# S2-extend — VN Salary Advance (policy + request) + VN Leave Handover task.
# --------------------------------------------------------------------------- #
ADVANCE_POLICY = "Chính sách ứng lương GD"


def _ensure_advance_policy() -> str | None:
    if not _ok("VN Salary Advance Policy"):
        return None
    if _exists("VN Salary Advance Policy", ADVANCE_POLICY):
        return ADVANCE_POLICY
    try:
        doc = frappe.get_doc(
            {"doctype": "VN Salary Advance Policy", "policy_name": ADVANCE_POLICY, "company": COMPANY}
        )
        doc.flags.ignore_permissions = True
        doc.flags.ignore_mandatory = True
        doc.insert(ignore_permissions=True)
        return doc.name
    except Exception as exc:  # noqa: BLE001
        _log(f"setup_test_data: Advance Policy: {exc}")
        return None


def seed_advance_and_handover() -> dict[str, Any]:
    """Seed a Salary Advance Policy + 1 advance request + 1 handover task."""
    if not frappe:
        return {}
    emp_row = frappe.db.get_value("Employee", {"user_id": ATT_EMP_EMAIL}, ["name", "employee_name"])
    out: dict[str, Any] = {"advance_policy": None, "advance": None, "handover": None}
    if not emp_row:
        return out
    emp, emp_name = emp_row
    today = datetime.date.today().isoformat()

    # Policy
    out["advance_policy"] = _ensure_advance_policy()

    # Advance request
    try:
        if not frappe.db.exists("VN Salary Advance Request", {"employee": emp, "requested_amount": 2000000}):
            doc = frappe.get_doc(
                {
                    "doctype": "VN Salary Advance Request",
                    "employee": emp,
                    "employee_name": emp_name,
                    "company": COMPANY,
                    "posting_date": today,
                    "requested_amount": 2000000,
                    "reason": "Seed advance — setup_test_data",
                    "salary_advance_policy": out["advance_policy"],
                }
            )
            doc.flags.ignore_permissions = True
            doc.flags.ignore_mandatory = True
            doc.insert(ignore_permissions=True)
            out["advance"] = doc.name
    except Exception as exc:  # noqa: BLE001
        _log(f"setup_test_data: Advance Request: {exc}")
        out["advance_error"] = str(exc)

    # Handover task — link the approved Leave Application, from test01 → test02.
    try:
        approved_la = frappe.db.get_value(
            "Leave Application",
            {"employee": emp, "status": "Approved"},
            "name",
            order_by="creation desc",
        )
        to_emp = frappe.db.get_value("Employee", {"user_id": "hr.manager@gege.test"}, "name")
        if approved_la and to_emp and not frappe.db.exists(
            "VN Leave Handover Task", {"leave_application": approved_la, "from_employee": emp}
        ):
            doc = frappe.get_doc(
                {
                    "doctype": "VN Leave Handover Task",
                    "leave_application": approved_la,
                    "from_employee": emp,
                    "to_employee": to_emp,
                    "handover_date": today,
                    "description": "Seed handover — setup_test_data",
                    "status": "Pending",
                }
            )
            doc.flags.ignore_permissions = True
            doc.flags.ignore_mandatory = True
            doc.insert(ignore_permissions=True)
            out["handover"] = doc.name
    except Exception as exc:  # noqa: BLE001
        _log(f"setup_test_data: Handover Task: {exc}")
        out["handover_error"] = str(exc)

    frappe.db.commit()
    return out


# --------------------------------------------------------------------------- #
# S2-extend — VN Payroll Review Period (from the locked attendance period) so
# PayrollReview/Export/Payslips have a period. Chains gege_hr's own API:
# confirm_all_lines → lock_period → create_payroll_review → calculate_payroll_review.
# --------------------------------------------------------------------------- #
def seed_payroll() -> dict[str, Any]:
    if not frappe:
        return {}
    out: dict[str, Any] = {}
    try:
        from gege_hr.gege_hr.api import attendance_period as ap
        from gege_hr.gege_hr.api import payroll as py
    except Exception as exc:  # noqa: BLE001
        return {"error": f"import failed: {exc}"}

    today = datetime.date.today()
    month = f"{today.month:02d}"
    year = today.year
    att_period = frappe.db.get_value(
        "VN Monthly Attendance Period",
        {"company": COMPANY, "payroll_month": month, "payroll_year": year},
        "name",
    )
    if not att_period:
        return {"error": f"no attendance period {month}/{year} — run seed_attendance_period first"}
    out["attendance_period"] = att_period

    for step, fn, arg in [
        ("confirm", ap.confirm_all_lines, att_period),
        ("lock", lambda n: ap.lock_period(n, reason="seed"), att_period),
    ]:
        try:
            out[step] = str(fn(arg))
        except Exception as exc:  # noqa: BLE001
            _log(f"setup_test_data: payroll {step} {att_period}: {exc}")
            out[f"{step}_error"] = str(exc)

    from_date = today.replace(day=1).isoformat()
    if today.month == 12:
        last = today.replace(day=31)
    else:
        last = today.replace(month=today.month + 1, day=1) - datetime.timedelta(days=1)
    to_date = last.isoformat()

    try:
        # lock_period auto-creates the Draft review — reuse it when present so
        # re-seeding never trips the (company, month, year) duplicate guard.
        pr_name = frappe.db.get_value(
            "VN Payroll Review Period",
            {
                "company": COMPANY,
                "payroll_month": month,
                "payroll_year": year,
                "docstatus": ["<", 2],
            },
            "name",
        )
        if not pr_name:
            rev = py.create_payroll_review(
                company=COMPANY,
                payroll_month=month,
                payroll_year=year,
                from_date=from_date,
                to_date=to_date,
                attendance_period=att_period,
            )
            pr_name = rev.get("name") if isinstance(rev, dict) else None
        out["payroll_review"] = pr_name
        if pr_name:
            try:
                out["calculate"] = str(py.calculate_payroll_review(pr_name))
            except Exception as exc:  # noqa: BLE001
                _log(f"setup_test_data: calculate_payroll_review {pr_name}: {exc}")
                out["calculate_error"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        _log(f"setup_test_data: create_payroll_review: {exc}")
        out["payroll_review_error"] = str(exc)

    frappe.db.commit()
    return out


# --------------------------------------------------------------------------- #
# S2-extend — Salary Structure + Assignment so PayrollReview gross > 0.
# resolve_base_salary() reads Salary Structure Assignment.base.
# --------------------------------------------------------------------------- #
SALARY_STRUCTURE = "Lương CB VN"
BASE_SALARY = 10_000_000  # VND/month per test employee


def _ensure_salary_structure() -> str | None:
    if not _ok("Salary Structure"):
        return None
    if _exists("Salary Structure", SALARY_STRUCTURE):
        try:
            doc = frappe.get_doc("Salary Structure", SALARY_STRUCTURE)
            if doc.docstatus != 1:
                doc.flags.ignore_permissions = True
                doc.submit()
        except Exception:
            pass
        return SALARY_STRUCTURE
    try:
        doc = frappe.get_doc(
            {
                "doctype": "Salary Structure",
                "name": SALARY_STRUCTURE,
                "company": COMPANY,
                "payroll_frequency": "Monthly",
                "currency": "VND",
                "earnings": [
                    {
                        "doctype": "Salary Detail",
                        "salary_component": "Basic",
                        "amount": 0,
                        "depends_on_payment_days": 1,
                    }
                ],
            }
        )
        doc.flags.ignore_permissions = True
        doc.flags.ignore_mandatory = True
        doc.insert(ignore_permissions=True)
        doc.submit()
        return doc.name
    except Exception as exc:  # noqa: BLE001
        _log(f"setup_test_data: Salary Structure: {exc}")
        return None


def _ensure_salary_assignment(employee: str, structure: str) -> str | None:
    try:
        existing = frappe.db.get_value(
            "Salary Structure Assignment",
            {"employee": employee, "salary_structure": structure, "docstatus": 1},
            "name",
        )
        if existing:
            return existing
    except Exception:
        pass
    try:
        doc = frappe.get_doc(
            {
                "doctype": "Salary Structure Assignment",
                "employee": employee,
                "salary_structure": structure,
                "company": COMPANY,
                "from_date": "2024-01-01",
                "base": BASE_SALARY,
            }
        )
        doc.flags.ignore_permissions = True
        doc.flags.ignore_mandatory = True
        doc.insert(ignore_permissions=True)
        doc.submit()
        return doc.name
    except Exception as exc:  # noqa: BLE001
        _log(f"setup_test_data: Salary Structure Assignment {employee}: {exc}")
        return None


def seed_salary_structure() -> dict[str, Any]:
    """Create Salary Structure + assignment (base 10M) for all test employees."""
    if not frappe:
        return {}
    structure = _ensure_salary_structure()
    out: dict[str, Any] = {"structure": structure, "assignments": {}}
    if not structure:
        return out
    for email in DEFAULT_SHIFT_BY_EMAIL:  # the 4 employees
        emp = frappe.db.get_value("Employee", {"user_id": email}, "name")
        if emp:
            out["assignments"][emp] = _ensure_salary_assignment(emp, structure)
    frappe.db.commit()
    return out


# --------------------------------------------------------------------------- #
# S2-extend — VN Attendance Work Session (full month, payable_day=1) so the
# PayrollReview gross > 0. aggregate_work_sessions() sums payable_day +
# regular_hours from the stored rows, so we force those via db.set_value after
# insert (the WS validate recomputes from raw logs, so a plain insert would
# yield payable_day=0 like the real "missing check-in" rows).
# --------------------------------------------------------------------------- #
WS_MONTH = (0, 0)  # (0,0) → current month, set in seed_work_sessions


def _ws_for(employee: str, work_date, shift_instance: str, shift_type: str) -> str | None:
    """Insert a draft work session and force a full payable day."""
    try:
        if frappe.db.exists(
            "VN Attendance Work Session",
            {"employee": employee, "work_date": work_date},
        ):
            return None  # already present
    except Exception:
        pass
    try:
        doc = frappe.get_doc(
            {
                "doctype": "VN Attendance Work Session",
                "employee": employee,
                "work_date": str(work_date),
                "company": COMPANY,
                "shift_instance": shift_instance,
                "shift_type": shift_type,
            }
        )
        doc.flags.ignore_permissions = True
        doc.flags.ignore_mandatory = True
        doc.insert(ignore_permissions=True)
    except Exception as exc:  # noqa: BLE001
        _log(f"setup_test_data: WS insert {employee} {work_date}: {exc}")
        return None
    # Force a full present day regardless of missing raw logs.
    frappe.db.set_value(
        "VN Attendance Work Session",
        doc.name,
        {
            "scheduled_regular_hours": 9.0,
            "regular_hours": 9.0,
            "payable_regular_hours": 9.0,
            "payable_day": 1.0,
            "total_actual_hours": 9.0,
            "actual_within_shift_hours": 9.0,
            "actual_checkin": f"{work_date} 08:02:00",
            "actual_checkout": f"{work_date} 17:18:00",
            "missing_checkin": 0,
            "missing_checkout": 0,
            "absent": 0,
            "calculation_status": "Calculated",
        },
        update_modified=False,
    )
    return doc.name


def seed_work_sessions() -> dict[str, Any]:
    """Seed a full month of payable work sessions for every test employee, then
    re-run calculate_payroll_review so the payroll period gross > 0."""
    if not frappe:
        return {}
    today = datetime.date.today()
    out: dict[str, Any] = {"created": 0, "calculate": None}

    # all weekdays of the current month
    d = today.replace(day=1)
    if today.month == 12:
        month_end = today.replace(day=31)
    else:
        month_end = today.replace(month=today.month + 1, day=1) - datetime.timedelta(days=1)
    days: list[datetime.date] = []
    while d <= month_end:
        if d.weekday() < 5:
            days.append(d)
        d += datetime.timedelta(days=1)

    # every test employee that has a shift instance
    for email in DEFAULT_SHIFT_BY_EMAIL:
        emp = frappe.db.get_value("Employee", {"user_id": email}, "name")
        if not emp:
            continue
        si = frappe.db.get_value(
            "VN Employee Shift Instance", {"employee": emp}, "name", order_by="shift_date desc"
        )
        shift = DEFAULT_SHIFT_BY_EMAIL.get(email)
        for day in days:
            if _ws_for(emp, day, si, shift):
                out["created"] += 1

    frappe.db.commit()

    # re-run the payroll review calc for the current-month period (if any)
    month = f"{today.month:02d}"
    pr_name = frappe.db.get_value(
        "VN Payroll Review Period",
        {"company": COMPANY, "payroll_month": month, "payroll_year": today.year},
        "name",
    )
    if pr_name:
        try:
            from gege_hr.gege_hr.api import payroll as py

            out["calculate"] = str(py.calculate_payroll_review(pr_name))
        except Exception as exc:  # noqa: BLE001
            _log(f"setup_test_data: re-calculate after WS seed: {exc}")
            out["calculate_error"] = str(exc)
    return out


# --------------------------------------------------------------------------- #
# S2-extend — Employee Checkin (IN/OUT pairs) → recalculate_period rebuilds Work
# Sessions with a real payable_day → PayrollReview gross > 0. The gege_hr engine
# (calc.persist_work_session) derives payable_day from the check-in/out times vs
# the shift, so this is the correct path (forcing payable_day via db.set_value
# does NOT work — _employee_work_sessions aggregates the engine-computed value).
# --------------------------------------------------------------------------- #
def seed_checkins() -> dict[str, Any]:
    """Seed IN/OUT Employee Checkins for every submitted Aug Shift Instance of
    the test employees, then recalculate Work Sessions + the payroll period."""
    if not frappe:
        return {}
    out: dict[str, Any] = {"checkins": 0, "recalculate": None, "payroll": None}
    try:
        from gege_hr.gege_hr.api import attendance as att
        from gege_hr.gege_hr.api import payroll as py
    except Exception as exc:  # noqa: BLE001
        return {"error": f"import: {exc}"}

    today = datetime.date.today()
    start = today.replace(day=1).isoformat()
    if today.month == 12:
        end = today.replace(day=31).isoformat()
    else:
        end = (today.replace(month=today.month + 1, day=1) - datetime.timedelta(days=1)).isoformat()

    for email in DEFAULT_SHIFT_BY_EMAIL:
        emp = frappe.db.get_value("Employee", {"user_id": email}, "name")
        if not emp:
            continue
        # submitted shift instances in the window
        sis = frappe.db.get_all(
            "VN Employee Shift Instance",
            {"employee": emp, "docstatus": 1, "work_date": ["between", [start, end]]},
            ["name", "work_date", "shift_type"],
        )
        for si in sis:
            d = str(si["work_date"])
            for log_type, hhmm in (("IN", "08:02:00"), ("OUT", "17:18:00")):
                # skip if a checkin already exists for this employee/time/log
                exists = frappe.db.exists(
                    "Employee Checkin",
                    {"employee": emp, "time": f"{d} {hhmm}", "log_type": log_type},
                )
                if exists:
                    continue
                try:
                    doc = frappe.get_doc(
                        {
                            "doctype": "Employee Checkin",
                            "employee": emp,
                            "time": f"{d} {hhmm}",
                            "log_type": log_type,
                            "shift": si["shift_type"],
                        }
                    )
                    doc.flags.ignore_permissions = True
                    doc.flags.ignore_mandatory = True
                    doc.insert(ignore_permissions=True)
                    out["checkins"] += 1
                except Exception as exc:  # noqa: BLE001
                    _log(f"setup_test_data: Employee Checkin {emp} {d} {log_type}: {exc}")

    frappe.db.commit()

    # rebuild Work Sessions from the new checkins
    try:
        out["recalculate"] = str(att.recalculate_period(start, end, backfill=0))
    except Exception as exc:  # noqa: BLE001
        _log(f"setup_test_data: recalculate_period: {exc}")
        out["recalculate_error"] = str(exc)

    # re-run the payroll review calc
    pr_name = frappe.db.get_value(
        "VN Payroll Review Period",
        {"company": COMPANY, "payroll_month": f"{today.month:02d}", "payroll_year": today.year},
        "name",
    )
    if pr_name:
        try:
            out["payroll"] = str(py.calculate_payroll_review(pr_name))
        except Exception as exc:  # noqa: BLE001
            out["payroll_error"] = str(exc)
    return out


if __name__ == "__main__":  # pragma: no cover
    create_test_dataset()
