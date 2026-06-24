"""
Admin API — HR Manager self-service (Zero-Frappe plan).

These endpoints let an HR Manager run **onboarding and master-data management
entirely inside the HR app** — creating Frappe Users, assigning portal roles,
linking a User to an Employee, and creating Shift Assignments — without ever
opening the Frappe desk.

Design (see ``plans/zero-frappe-hr-plan.md``):

* Every endpoint is permission-gated to ``HR Manager`` / ``System Manager``
  via :func:`frappe.only_for` (decision D6 — User/role/shift assignment are
  sensitive; HR User is NOT enough, to prevent privilege escalation).
* Only the whitelisted :data:`PORTAL_ROLES` may be assigned; ``remove_roles``
  never strips ``System Manager`` (safety against HR locking out admins).
* Every mutation is recorded as a ``Manual Override`` :doc:`VN Audit Event`
  via :mod:`gege_hr.gege_hr.api.audit` (NĐ 13/2023 traceability).
* Shift Assignment is a submittable Frappe DocType — :func:`create_shift_assignment`
  inserts + submits so the daily scheduler
  (:func:`gege_hr.gege_hr.api.shift.generate_daily_shift_instances`)
  materialises ``VN Employee Shift Instance`` rows.

Pure helpers (``_require_hr_admin``, ``_default_company``) are bench-aware and
live here rather than in ``utils/`` because they only make sense behind a
``@frappe.whitelist`` boundary.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import getdate

from gege_hr.gege_hr.api import audit as audit_api

# Roles HR Manager may assign to a user. Whitelisting here (on top of Frappe's
# own Role permissions) prevents an HR Manager from minting a System Manager
# or any arbitrary Frappe role — a privilege-escalation guard (plan risk §9).
PORTAL_ROLES = [
    "Employee",
    "Line Manager",
    "HR User",
    "HR Manager",
    "Payroll User",
    "Payroll Manager",
]

HR_ADMIN_ROLES = ["HR Manager", "System Manager"]


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _require_hr_admin() -> None:
    """Centralized permission gate for every admin endpoint.

    ``frappe.only_for`` throws ``frappe.PermissionError`` if the session user
    lacks any of ``HR_ADMIN_ROLES`` — mirroring the pattern used across the
    gege_hr API modules (plan §5.3).
    """
    frappe.only_for(HR_ADMIN_ROLES)


def _default_company() -> str | None:
    """Resolve a company to attribute audit events to when no employee/context.

    Priority: VN HR Portal Setting default → first company. Returns ``None``
    when no company exists (fresh install) — callers tolerate this gracefully.
    """
    try:
        company = frappe.db.get_single_value("VN HR Portal Setting", "default_company")
        if company:
            return company
    except Exception:
        pass
    try:
        return frappe.db.get_value("Company", {}, "name", order_by="name asc")
    except Exception:
        return None


def _company_for_employee(employee: str) -> str | None:
    """Resolve the company of an Employee (or fall back to the default)."""
    if employee:
        try:
            company = frappe.db.get_value("Employee", employee, "company")
            if company:
                return company
        except Exception:
            pass
    return _default_company()


# Standard table columns present on every DocType — always safe to project.
_META_COLUMNS = frozenset(
    {
        "name",
        "creation",
        "modified",
        "modified_by",
        "owner",
        "docstatus",
        "idx",
        "parent",
        "parentfield",
        "parenttype",
        "_user_tags",
    }
)


def _safe_fields(doctype: str, fields) -> list[str]:
    """Return the subset of ``fields`` that actually exist on ``doctype``.

    Always includes ``name``. Guards the generic list paths against
    client-requested fields that have no DB column (e.g. ``is_paid_leave`` on a
    Leave Type whose column was never created) which would otherwise raise
    ``pymysql.OperationalError(1054) Unknown column``. On any meta lookup
    failure it degrades to ``["name"]`` so the call still succeeds.
    """
    if not fields:
        return ["name"]
    if isinstance(fields, str):
        fields = [f for f in fields.split(",") if f]
    try:
        valid = {df.fieldname for df in frappe.get_meta(doctype).fields}
        valid |= _META_COLUMNS
    except Exception:
        return ["name"]
    out: list[str] = []
    for f in fields:
        f = str(f).strip()
        if f and f in valid and f not in out:
            out.append(f)
    if "name" not in out:
        out.append("name")
    return out


def _audit_admin(
    description: str,
    *,
    reference_doctype: str,
    reference_name: str,
    company: str | None = None,
    employee: str | None = None,
    new_value=None,
) -> None:
    """Best-effort audit row for an admin mutation.

    Uses ``audit_type="Manual Override"`` (the only admin-safe value in the
    VN Audit Event Select). ``audit_api.log`` swallows all failures so a
    logging hiccup never aborts the business operation (plan §3.3).
    """
    audit_api.log(
        "Manual Override",
        company=company or _default_company(),
        employee=employee,
        reference_doctype=reference_doctype,
        reference_name=reference_name,
        description=description,
        new_value=new_value,
    )


# --------------------------------------------------------------------------- #
# G1: User creation
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def get_assignable_roles() -> list[dict]:
    """Return the portal roles HR may assign — for the role picker UI."""
    _require_hr_admin()
    return [{"value": r, "label": r} for r in PORTAL_ROLES]


@frappe.whitelist()
def create_user(
    email: str,
    full_name: str,
    first_name: str | None = None,
    last_name: str | None = None,
    send_welcome_email: int = 1,
    roles: list[str] | None = None,
) -> dict:
    """Create a Frappe User and assign portal roles atomically.

    Returns ``{"name", "email", "full_name", "roles"}``.

    Validation:
    * email must not already exist as a User;
    * only :data:`PORTAL_ROLES` are honoured (silently drops anything else);
    * the new User is created as ``Website User`` (not System User) so it
      cannot access the desk by default.
    """
    _require_hr_admin()
    email = (email or "").strip()
    full_name = (full_name or "").strip()
    if not email or not full_name:
        frappe.throw(_("Email và họ tên là bắt buộc."))
    if frappe.db.exists("User", email):
        frappe.throw(_("Email đã tồn tại: {0}").format(email))

    user = frappe.get_doc(
        {
            "doctype": "User",
            "email": email,
            "username": email.split("@")[0],
            "first_name": first_name or full_name.split()[0],
            "last_name": last_name,
            "full_name": full_name,
            "send_welcome_email": 1 if send_welcome_email else 0,
            "enabled": 1,
            "user_type": "Website User",
        }
    )
    user.insert()

    assigned = []
    for role in roles or []:
        if role in PORTAL_ROLES:
            user.add_roles(role)
            assigned.append(role)

    _audit_admin(
        _("Tạo tài khoản người dùng"),
        reference_doctype="User",
        reference_name=user.name,
        new_value={"email": email, "roles": assigned},
    )
    return {"name": user.name, "email": email, "full_name": full_name, "roles": assigned}


# --------------------------------------------------------------------------- #
# G2: Role assignment
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def assign_roles(user: str, roles: list[str]) -> dict:
    """Add portal roles to a user (idempotent). Only :data:`PORTAL_ROLES` allowed.

    Returns ``{"name", "added"}``.
    """
    _require_hr_admin()
    user = (user or "").strip()
    if not user or not frappe.db.exists("User", user):
        frappe.throw(_("Người dùng không tồn tại."))
    target = frappe.get_doc("User", user)
    current = {d.role for d in target.get("roles", [])}
    added = []
    for role in roles or []:
        if role in PORTAL_ROLES and role not in current:
            # HR Manager has write on User (granted via setup_permissions), so a
            # real save() works — no ignore_permissions bypass. _require_hr_admin()
            # above is the app-level gate on top.
            target.append("roles", {"role": role})
            current.add(role)
            added.append(role)
    if added:
        target.save()
    _audit_admin(
        _("Gán vai trò: {0}").format(", ".join(added) or "—"),
        reference_doctype="User",
        reference_name=user,
        new_value={"added": added},
    )
    return {"name": user, "added": added}


@frappe.whitelist()
def remove_roles(user: str, roles: list[str]) -> dict:
    """Remove portal roles from a user. NEVER strips ``System Manager``.

    Returns ``{"name", "removed"}``.
    """
    _require_hr_admin()
    user = (user or "").strip()
    if not user or not frappe.db.exists("User", user):
        frappe.throw(_("Người dùng không tồn tại."))
    target = frappe.get_doc("User", user)
    existing = {d.role: d for d in target.get("roles", [])}
    removed = []
    for role in roles or []:
        # Safety: System Manager is never removable via this endpoint — prevents
        # an HR Manager from stripping the only admin (plan risk §9).
        if role == "System Manager":
            continue
        row = existing.get(role)
        if row:
            # HR Manager has write on User (granted via setup_permissions), so a
            # real save() works — no ignore_permissions bypass.
            target.get("roles").remove(row)
            removed.append(role)
    if removed:
        target.save()
    _audit_admin(
        _("Gỡ vai trò: {0}").format(", ".join(removed) or "—"),
        reference_doctype="User",
        reference_name=user,
        new_value={"removed": removed},
    )
    return {"name": user, "removed": removed}


# --------------------------------------------------------------------------- #
# G4: Link User ↔ Employee
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def link_user_to_employee(employee: str, user: str) -> dict:
    """Set ``Employee.user_id`` so the user resolves to the employee on login.

    Returns ``{"name", "user_id"}``.
    """
    _require_hr_admin()
    employee = (employee or "").strip()
    user = (user or "").strip()
    if not employee or not frappe.db.exists("Employee", employee):
        frappe.throw(_("Nhân viên không tồn tại."))
    if not user or not frappe.db.exists("User", user):
        frappe.throw(_("Người dùng không tồn tại."))
    # Reject if the user is already linked to another employee.
    existing = frappe.db.get_value("Employee", {"user_id": user, "name": ["!=", employee]}, "name")
    if existing:
        frappe.throw(_("Người dùng đã được liên kết với nhân viên khác: {0}").format(existing))

    company = _company_for_employee(employee)
    emp = frappe.get_doc("Employee", employee)
    old_user = emp.user_id
    emp.db_set("user_id", user)
    _audit_admin(
        _("Liên kết tài khoản với nhân viên"),
        reference_doctype="Employee",
        reference_name=employee,
        company=company,
        employee=employee,
        new_value={"old_user_id": old_user, "new_user_id": user},
    )
    return {"name": employee, "user_id": user}


# --------------------------------------------------------------------------- #
# G3: Shift Assignment
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def list_shift_assignments(
    employee: str | None = None,
    shift_type: str | None = None,
    status: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """List Shift Assignments (submitted) for the admin table.

    Filters by employee / shift_type / status / date window.
    """
    _require_hr_admin()
    filters = {"docstatus": 1}
    if employee:
        filters["employee"] = employee
    if shift_type:
        filters["shift_type"] = shift_type
    if status:
        filters["status"] = status
    if from_date:
        filters["start_date"] = [">=", getdate(from_date)]
    if to_date:
        filters["end_date"] = ["<=", getdate(to_date)]
    try:
        return frappe.get_all(
            "Shift Assignment",
            filters=filters,
            fields=[
                "name",
                "employee",
                "employee_name",
                "shift_type",
                "start_date",
                "end_date",
                "status",
                "docstatus",
                "company",
            ],
            order_by="start_date desc",
            limit_page_length=int(limit or 100),
        )
    except Exception:
        return []


@frappe.whitelist()
def create_shift_assignment(
    employee: str,
    shift_type: str,
    start_date: str,
    end_date: str | None = None,
    status: str = "Active",
) -> dict:
    """Create + submit a Shift Assignment (Frappe HR submittable DocType).

    Submission triggers the scheduler to materialise ``VN Employee Shift
    Instance`` rows (plan §7.1 layer 1→2; see
    :func:`gege_hr.gege_hr.api.shift.my_schedule` which reads these rows).

    Conflict check: rejects overlapping Active assignments for the same
    employee (plan risk §9 — prevents double-booking).
    """
    _require_hr_admin()
    employee = (employee or "").strip()
    shift_type = (shift_type or "").strip()
    start_date = (start_date or "").strip()
    if not employee or not frappe.db.exists("Employee", employee):
        frappe.throw(_("Nhân viên không tồn tại."))
    if not shift_type or not frappe.db.exists("Shift Type", shift_type):
        frappe.throw(_("Ca làm việc không tồn tại."))
    if not start_date:
        frappe.throw(_("Ngày bắt đầu là bắt buộc."))

    company = _company_for_employee(employee)
    end = getdate(end_date) if end_date else None
    start = getdate(start_date)

    # Overlap detection against active submitted assignments.
    overlap_filters = [
        ["employee", "=", employee],
        ["status", "=", "Active"],
        ["docstatus", "=", 1],
        ["start_date", "<=", end or "2999-12-31"],
    ]
    existing = frappe.db.get_all(
        "Shift Assignment",
        filters=overlap_filters,
        or_filters=[["end_date", ">=", start], ["end_date", "is", "not set"]],
        fields=["name", "shift_type", "start_date", "end_date"],
    )
    if existing:
        frappe.throw(
            _("Nhân viên đã có ca làm việc trong khoảng thời gian này: {0}").format(
                ", ".join(r.name for r in existing)
            )
        )

    doc = frappe.get_doc(
        {
            "doctype": "Shift Assignment",
            "employee": employee,
            "shift_type": shift_type,
            "start_date": start,
            "end_date": end,
            "status": status,
            "company": company,
        }
    )
    doc.insert()
    doc.submit()

    _audit_admin(
        _("Gán ca làm việc {0} cho {1}").format(shift_type, employee),
        reference_doctype="Shift Assignment",
        reference_name=doc.name,
        company=company,
        employee=employee,
        new_value={
            "shift_type": shift_type,
            "start_date": str(start),
            "end_date": str(end) if end else None,
        },
    )
    return {"name": doc.name}


@frappe.whitelist()
def end_shift_assignment(name: str, end_date: str) -> dict:
    """End an active Shift Assignment: set ``end_date`` then cancel.

    Cancelling (rather than just setting status) is the Frappe convention for
    submittable docs and frees the employee for a new assignment.
    """
    _require_hr_admin()
    name = (name or "").strip()
    if not name or not frappe.db.exists("Shift Assignment", name):
        frappe.throw(_("Ca làm việc không tồn tại."))
    if not end_date:
        frappe.throw(_("Ngày kết thúc là bắt buộc."))

    doc = frappe.get_doc("Shift Assignment", name)
    company = doc.company or _company_for_employee(doc.employee)
    doc.end_date = getdate(end_date)
    doc.save()
    if doc.docstatus == 1:
        doc.cancel()
    _audit_admin(
        _("Kết thúc ca làm việc"),
        reference_doctype="Shift Assignment",
        reference_name=name,
        company=company,
        employee=doc.employee,
        new_value={"end_date": str(getdate(end_date))},
    )
    return {"name": name}
