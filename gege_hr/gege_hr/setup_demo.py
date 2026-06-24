"""
Demo-data seeder for gege_hr.

Every remaining end-to-end verification in the handoff (Leave Cancellation flow,
Notification bell badge, per-employee attendance report) is **master-data
gated** — the bench has no ``Company`` / ``Leave Type`` / ``Employee`` /
``reports_to`` rows, so the approval matrix, notification producer and report
loaders all run on empty data.

This module creates a small, **idempotent** demo dataset so HR can finally
exercise the full portal interactively::

    bench --site <site> execute gege_hr.gege_hr.setup_demo.create_demo_data

Created demo data (only when missing — a manager's manual edits always survive
a re-run):

* **Company** "Gege Demo" (only when the site has no Company at all).
* **Leave Types** — Casual Leave / Sick Leave (paid) + Leave Without Pay.
* **Users + Employees** — one HR Manager (``hr.demo@gege.demo``) and one
  Employee (``emp.demo@gege.demo``) reporting to them, so the Line Manager →
  HR Manager approval matrix + notification inbox both resolve.
* **Leave Allocation** — Casual Leave balance for the current year so the
  Leave preview/apply/report flows have numbers to show.
* **Re-run** of :func:`gege_hr.gege_hr.setup.create_seed_data` once a Company
  exists, so the company-gated seeds (attendance policy, approval matrices,
  leave policy extension, staffing rule) attach to the demo company.

Design follows the existing seed module
([`gege_hr.gege_hr.setup`](gege_hr/gege_hr/gege_hr/setup.py:1)):

* **Pure helpers** (``*_payload``) build plain dicts — unit-testable without a
  bench (mirror the pattern of ``utils/calc.py`` / ``utils/leave.py``).
* **Frappe-aware creators** are ``bench-guarded`` (every failure is
  ``log_error`` + swallowed so the command never aborts) and idempotent.
"""

from __future__ import annotations

from datetime import date

try:  # bench-free import safety (tests run outside a Frappe site)
    import frappe  # type: ignore
except Exception:  # pragma: no cover
    frappe = None  # type: ignore

# --------------------------------------------------------------------------- #
# Demo constants — centralised so re-runs match exactly.
# --------------------------------------------------------------------------- #
DEMO_COMPANY = "Gege Demo"
DEMO_COMPANY_ABBR = "GD"
DEMO_CURRENCY = "VND"
DEMO_COUNTRY = "Vietnam"

DEMO_LEAVE_TYPES = (
    ("Casual Leave", False),  # (name, is_lwp)
    ("Sick Leave", False),
    ("Leave Without Pay", True),
)

DEMO_CASUAL_LEAVE = "Casual Leave"

# Warehouse Type masters ERPNext's Company.on_update → create_default_warehouses()
# references ("Transit" is the only one). Pre-created so a bare site can build
# the demo Company without a LinkValidationError after the row is written.
DEMO_WAREHOUSE_TYPES = ("Transit",)

# (email, first, last, gender, is_hr_manager)
DEMO_MANAGER = ("hr.demo@gege.demo", "HR", "Manager", "Male", True)
DEMO_EMPLOYEE = ("emp.demo@gege.demo", "Emp", "Demo", "Female", False)

DEMO_CASUAL_ALLOCATION = 12.0  # annual leave days granted to the demo employee

# Gender masters the demo Employees reference. On a bare site these do not exist
# and (like Warehouse Types) ``ignore_mandatory`` does not skip link validation,
# so they are pre-created before any Employee is inserted.
DEMO_GENDERS = ("Male", "Female", "Other")


# --------------------------------------------------------------------------- #
# Pure payload builders — bench-free, unit-testable.
# --------------------------------------------------------------------------- #
def company_payload(
    name: str = DEMO_COMPANY,
    abbr: str = DEMO_COMPANY_ABBR,
    currency: str = DEMO_CURRENCY,
    country: str = DEMO_COUNTRY,
) -> dict:
    """Build an ERPNext ``Company`` creation payload.

    ``abbr`` is coerced to upper-case and trimmed (ERPNext requires a non-empty
    upper-case abbreviation). ``currency``/``country`` default to VN demo
    values; callers may override for other locales.
    """
    abbr_clean = (abbr or "").strip().upper()
    if not abbr_clean:
        abbr_clean = DEMO_COMPANY_ABBR
    return {
        "doctype": "Company",
        "company_name": (name or "").strip() or DEMO_COMPANY,
        "abbr": abbr_clean,
        "default_currency": (currency or DEMO_CURRENCY).strip(),
        "country": (country or DEMO_COUNTRY).strip(),
        "is_group": 0,
    }


def leave_type_payload(name: str, is_lwp: bool = False) -> dict:
    """Build a Frappe HR ``Leave Type`` payload.

    ``is_lwp`` maps to ``is_ppl``/``is_lwp`` semantics used by the leave engine
    ([`gege_hr.gege_hr.utils.leave`](gege_hr/gege_hr/gege_hr/utils/leave.py:1))
    — Leave Without Pay does not deduct entitlement, only docks salary.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("leave_type_payload: name is required")
    payload = {
        "doctype": "Leave Type",
        "leave_type_name": name,
        "is_lwp": 1 if is_lwp else 0,
        "is_ppl": 0,
        "include_holiday": 1,
        "is_carry_forward": 1,
    }
    return payload


def user_payload(
    email: str,
    first_name: str,
    last_name: str = "",
    roles: list[str] | None = None,
) -> dict:
    """Build a Frappe ``User`` payload with optional role assignments."""
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("user_payload: email is required")
    payload = {
        "doctype": "User",
        "email": email,
        "first_name": (first_name or "").strip(),
        "last_name": (last_name or "").strip(),
        "send_welcome_email": 0,
        "enabled": 1,
    }
    if roles:
        payload["roles"] = [{"role": r, "doctype": "Has Role"} for r in roles]
    return payload


def employee_payload(
    first_name: str,
    last_name: str,
    company: str,
    email: str,
    user_id: str | None = None,
    reports_to: str | None = None,
    gender: str | None = None,
) -> dict:
    """Build a Frappe HR ``Employee`` payload.

    ``user_id`` wires the portal login → employee (the approval/notification
    resolvers look up the employee via the session user). ``reports_to`` sets
    the Line Manager link used by the default approval matrix.
    """
    first_name = (first_name or "").strip()
    last_name = (last_name or "").strip()
    if not first_name:
        raise ValueError("employee_payload: first_name is required")
    payload = {
        "doctype": "Employee",
        "first_name": first_name,
        "last_name": last_name,
        "company": (company or "").strip(),
        "prefered_email": (email or "").strip().lower(),
        "personal_email": (email or "").strip().lower(),
        "status": "Active",
        "gender": gender or "Other",
    }
    if user_id:
        payload["user_id"] = user_id
    if reports_to:
        payload["reports_to"] = reports_to
    return payload


def leave_allocation_payload(
    employee: str,
    leave_type: str,
    from_date,
    to_date,
    new_leaves_allocated: float,
) -> dict:
    """Build a Frappe HR ``Leave Allocation`` payload.

    ``from_date``/``to_date`` accept ``date`` or ISO strings (coerced via
    ``str``); Frappe stores them as ``YYYY-MM-DD``.
    """
    employee = (employee or "").strip()
    leave_type = (leave_type or "").strip()
    if not employee or not leave_type:
        raise ValueError("leave_allocation_payload: employee and leave_type are required")
    return {
        "doctype": "Leave Allocation",
        "employee": employee,
        "leave_type": leave_type,
        "from_date": str(from_date),
        "to_date": str(to_date),
        "new_leaves_allocated": float(new_leaves_allocated or 0),
        "docstatus": 1,  # submit so the balance is immediately visible
    }


def allocation_year_window(year: int | None = None, today=None) -> tuple:
    """Return ``(year, from_date, to_date)`` covering the whole calendar year.

    Defaults to the year of ``today`` (or :func:`date.today` when omitted), so
    the demo allocation always matches the period an HR tester is working in.
    """
    today = today or date.today()
    year = int(year) if year else today.year
    return year, date(year, 1, 1), date(year, 12, 31)


# --------------------------------------------------------------------------- #
# Frappe-aware creators — bench-guarded + idempotent. Not unit-tested (bench).
# --------------------------------------------------------------------------- #
def _table_ready(doctype: str) -> bool:
    try:
        return bool(frappe.db.table_exists(doctype))
    except Exception:
        return False


def _exists(doctype: str, name: str) -> bool:
    try:
        return bool(frappe.db.exists(doctype, name))
    except Exception:
        return False


def _ensure_warehouse_types() -> None:
    """Pre-create the Warehouse Type masters ERPNext's Company.on_update needs.

    ERPNext's ``Company.on_update`` → ``create_default_warehouses()`` references
    the ``Transit`` Warehouse Type. On a bare site that master does not exist
    and (unlike mandatory-field checks) ``ignore_mandatory`` does **not** skip
    link validation, so the warehouse insert raises ``LinkValidationError``
    *after* the Company row is already written — which used to make
    :func:`_ensure_company` report failure even though the Company persisted.
    Creating the master up front lets the natural ERPNext setup complete.
    Bench-guarded + idempotent.
    """
    if not _table_ready("Warehouse Type"):
        return
    for name in DEMO_WAREHOUSE_TYPES:
        if _exists("Warehouse Type", name):
            continue
        try:
            frappe.get_doc({"doctype": "Warehouse Type", "warehouse_type": name}).insert(
                ignore_permissions=True
            )
        except Exception:
            frappe.log_error(f"gege_hr demo: failed to create Warehouse Type {name}")


def _ensure_genders() -> None:
    """Pre-create Gender masters the demo Employees reference.

    Same defensive pattern as :func:`_ensure_warehouse_types`: a bare site ships
    without the standard ``Male`` / ``Female`` / ``Other`` Gender rows, and
    ``ignore_mandatory`` does not skip the Employee ``gender`` link validation,
    so an Employee insert raises ``LinkValidationError``. Bench-guarded +
    idempotent.
    """
    if not _table_ready("Gender"):
        return
    for name in DEMO_GENDERS:
        if _exists("Gender", name):
            continue
        try:
            frappe.get_doc({"doctype": "Gender", "gender": name}).insert(ignore_permissions=True)
        except Exception:
            frappe.log_error(f"gege_hr demo: failed to create Gender {name}")


def _ensure_company() -> str | None:
    """Return a usable Company, creating the demo one when the site is empty.

    ERPNext's ``Company.on_update`` runs heavy setup (chart-of-accounts, default
    warehouses, cost center, departments) once the row is written. A late
    failure there (e.g. a missing Warehouse Type master) raises *after* the
    Company row is already persisted, so even when ``insert()`` raises we fall
    back to verifying the row exists — the Company is fully usable for HR even
    if a stock-keeping side effect did not finish.
    """
    if not _table_ready("Company"):
        return None
    try:
        existing = frappe.get_all("Company", filters={"disabled": 0}, pluck="name", order_by="name", limit=1)
    except Exception:
        existing = []
    if existing:
        return existing[0]
    if _exists("Company", DEMO_COMPANY):
        return DEMO_COMPANY
    _ensure_warehouse_types()
    try:
        doc = frappe.get_doc(company_payload())
        doc.flags.ignore_permissions = True
        doc.flags.ignore_mandatory = True  # chart-of-accounts setup is heavy; skip mandatory
        doc.insert(ignore_permissions=True)
        return doc.name
    except Exception:
        frappe.log_error("gege_hr demo: failed to create demo Company")
        # Resilient fallback: the Company row is often persisted before ERPNext's
        # on_update post-processing raises — it is still a valid HR Company.
        if _exists("Company", DEMO_COMPANY):
            return DEMO_COMPANY
        return None


def _ensure_leave_types() -> list[str]:
    """Create missing demo Leave Types. Returns the list of existing names."""
    if not _table_ready("Leave Type"):
        return []
    created = []
    for name, is_lwp in DEMO_LEAVE_TYPES:
        if _exists("Leave Type", name):
            created.append(name)
            continue
        try:
            doc = frappe.get_doc(leave_type_payload(name, is_lwp))
            doc.flags.ignore_permissions = True
            doc.insert(ignore_permissions=True)
            created.append(name)
        except Exception:
            frappe.log_error(f"gege_hr demo: failed to create Leave Type {name}")
    return created


def _ensure_user(email: str, first: str, last: str, hr_manager: bool) -> str | None:
    """Return a User email, creating it (with roles) when missing."""
    if not _table_ready("User"):
        return None
    if _exists("User", email):
        return email
    roles = ["Employee"]
    if hr_manager:
        roles.extend(["HR Manager", "HR User"])
    try:
        doc = frappe.get_doc(user_payload(email, first, last, roles))
        doc.flags.ignore_permissions = True
        doc.flags.ignore_mandatory = True
        doc.insert(ignore_permissions=True)
        return email
    except Exception:
        frappe.log_error(f"gege_hr demo: failed to create User {email}")
        return None


def _ensure_employee(
    first: str, last: str, company: str, email: str, user_id, reports_to, gender
) -> str | None:
    """Return an Employee name, creating it when missing.

    Lookup is by ``user_id`` first (the portal's identity link), falling back to
    a name search so a partial pre-existing record isn't duplicated.
    """
    if not _table_ready("Employee"):
        return None
    if user_id:
        try:
            rows = frappe.get_all("Employee", filters={"user_id": user_id}, pluck="name", limit=1)
        except Exception:
            rows = []
        if rows:
            return rows[0]
    try:
        doc = frappe.get_doc(employee_payload(first, last, company, email, user_id, reports_to, gender))
        doc.flags.ignore_permissions = True
        doc.flags.ignore_mandatory = True
        doc.insert(ignore_permissions=True)
        return doc.name
    except Exception:
        frappe.log_error(f"gege_hr demo: failed to create Employee {email}")
        return None


def _ensure_leave_allocation(employee: str, leave_type: str, from_date, to_date, days: float) -> bool:
    """Submit a Leave Allocation for the employee when none exists for the year."""
    if not _table_ready("Leave Allocation") or not employee or not leave_type:
        return False
    try:
        existing = frappe.get_all(
            "Leave Allocation",
            filters={
                "employee": employee,
                "leave_type": leave_type,
                "from_date": str(from_date),
                "to_date": str(to_date),
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
        doc = frappe.get_doc(leave_allocation_payload(employee, leave_type, from_date, to_date, days))
        doc.flags.ignore_permissions = True
        doc.flags.ignore_mandatory = True
        doc.insert(ignore_permissions=True)
        doc.submit()
        return True
    except Exception:
        frappe.log_error(f"gege_hr demo: failed to allocate {leave_type} to {employee}")
        return False


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def create_demo_data() -> dict:
    """Create the demo Company / Leave Types / Users / Employees + allocation.

    Idempotent: re-running only fills gaps. After creating a Company it
    re-invokes the production seeder
    ([`create_seed_data`](gege_hr/gege_hr/gege_hr/setup.py:313)) so the
    company-gated seeds attach to the demo company. Returns a summary dict::

        {
            "company": "...",
            "leave_types": [...],
            "manager": "...",
            "employee": "...",
            "allocation": True / False,
            "reseeded": [...],
        }
    """
    summary: dict = {
        "company": None,
        "leave_types": [],
        "manager": None,
        "employee": None,
        "allocation": False,
        "reseeded": [],
    }
    if frappe is None:
        return summary

    try:
        company = _ensure_company()
    except Exception:
        frappe.log_error("gege_hr demo: company step failed")
        company = None
    if not company:
        return summary
    summary["company"] = company

    try:
        summary["leave_types"] = _ensure_leave_types()
    except Exception:
        frappe.log_error("gege_hr demo: leave type step failed")

    # Gender masters must exist before Employee inserts (link validation is not
    # skipped by ignore_mandatory).
    try:
        _ensure_genders()
    except Exception:
        frappe.log_error("gege_hr demo: gender step failed")

    # Manager first so the employee's reports_to can point at them.
    mgr_email, mgr_first, mgr_last, mgr_gender, _ = DEMO_MANAGER
    manager_user = _ensure_user(mgr_email, mgr_first, mgr_last, hr_manager=True)
    manager_emp = None
    if manager_user:
        manager_emp = _ensure_employee(
            mgr_first, mgr_last, company, mgr_email, manager_user, None, mgr_gender
        )
    summary["manager"] = manager_emp

    emp_email, emp_first, emp_last, emp_gender, _ = DEMO_EMPLOYEE
    emp_user = _ensure_user(emp_email, emp_first, emp_last, hr_manager=False)
    employee_emp = None
    if emp_user:
        employee_emp = _ensure_employee(
            emp_first, emp_last, company, emp_email, emp_user, manager_emp, emp_gender
        )
    summary["employee"] = employee_emp

    if employee_emp and DEMO_CASUAL_LEAVE in summary["leave_types"]:
        _, from_date, to_date = allocation_year_window()
        summary["allocation"] = _ensure_leave_allocation(
            employee_emp, DEMO_CASUAL_LEAVE, from_date, to_date, DEMO_CASUAL_ALLOCATION
        )

    # Now that a Company exists, let the production seeder attach its
    # company-gated defaults (policy / matrices / leave policy / staffing rule)
    # — this is exactly what unblocks the master-data-gated verifications.
    try:
        from gege_hr.gege_hr.setup import create_seed_data

        summary["reseeded"] = (create_seed_data() or {}).get("seeded", [])
    except Exception:
        frappe.log_error("gege_hr demo: re-run of create_seed_data failed")

    return summary
