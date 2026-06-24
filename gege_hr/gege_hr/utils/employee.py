"""
Employee / user resolution helpers.

Maps the logged-in Frappe ``User`` to their ``Employee`` record, resolves the
portal role (Employee / HR User / HR Manager / System Manager), and fetches the
applicable VN Attendance Policy.
"""

from __future__ import annotations

try:
    import frappe  # type: ignore
except Exception:  # pragma: no cover
    frappe = None

HR_MANAGER_ROLES = {"HR Manager", "System Manager"}
HR_USER_ROLES = {"HR User", "HR Manager", "System Manager"}


def get_current_user() -> str | None:
    if frappe is None:
        return None
    user = frappe.session.user
    return None if user in ("Guest", None) else user


def get_employee_for_user(user: str | None = None) -> str | None:
    """Return the Employee name linked to the user (via ``user_id``), or None."""
    if frappe is None:
        return None
    user = user or get_current_user()
    if not user:
        return None
    return frappe.db.get_value("Employee", {"user_id": user, "status": "Active"})


def emp_name(employee) -> str:
    """Coerce an Employee arg (name string | dict | Document) to its *name*.

    The SPA sometimes echoes the whole Employee object (from ``me()``) as the
    ``employee`` param; using it verbatim as a ``filters`` value injects the
    dict repr into SQL. Always reduce to the name string before filtering.
    """
    if isinstance(employee, str):
        return employee
    if isinstance(employee, dict):
        return employee.get("name") or employee.get("employee") or ""
    return getattr(employee, "name", "") or ""


def get_user_roles(user: str | None = None) -> list[str]:
    if frappe is None:
        return []
    user = user or get_current_user()
    if not user:
        return []
    return sorted({r for r in frappe.get_roles(user)})


def portal_role(roles: list[str]) -> str:
    """Derive a single coarse portal role for the frontend layout.

    Priority: Manager > HR User > Employee > Guest.
    """
    role_set = set(roles or [])
    if role_set & HR_MANAGER_ROLES:
        return "Manager"
    if role_set & HR_USER_ROLES:
        return "HRUser"
    return "Employee"


def get_attendance_policy_name(employee: str | None = None) -> str | None:
    """Resolve the applicable policy: employee override → default → None.

    employee.vested policy may be set directly; otherwise fall back to the
    company default stored in VN HR Portal Setting.
    """
    if frappe is None:
        return None
    if employee:
        emp_policy = frappe.db.get_value("Employee", employee, "vn_attendance_policy")
        if emp_policy:
            return emp_policy
    return frappe.db.get_single_value("VN HR Portal Setting", "default_attendance_policy") or None
