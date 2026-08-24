"""
Auth API — plan v5 §10.1.

``get_csrf_token`` / ``login`` / ``logout`` / ``me`` power the hr-ui shell
(useHrAuth). ``me()`` returns the identity + coarse portal role + employee
mapping + portal timezone so the frontend can render the correct layout and
format every time in VN time (plan §2.7).
"""

from __future__ import annotations

import frappe
from frappe import _

from gege_hr.gege_hr.utils import employee as emp_utils, tz as tz_utils


@frappe.whitelist(allow_guest=True)
def get_csrf_token() -> dict:
    """Return a fresh CSRF token (and set the cookie) for the SPA bootstrap."""
    token = frappe.sessions.get_csrf_token()
    return {"csrf_token": token}


@frappe.whitelist(allow_guest=True)
def login(usr: str = None, pwd: str = None) -> dict:
    """Authenticate a Frappe user and return the identity payload (mirrors ``me``).

    Kept as a thin wrapper around ``frappe.auth.LoginManager`` so the SPA can
    POST credentials directly to ``gege_hr.gege_hr.api.auth.login``.
    """
    if not usr or not pwd:
        frappe.throw(_("Vui lòng nhập tài khoản và mật khẩu."), frappe.AuthenticationError)

    login_manager = frappe.auth.LoginManager()
    login_manager.authenticate(usr, pwd)
    login_manager.post_login()

    return me()


@frappe.whitelist()
def logout() -> dict:
    """End the current session."""
    frappe.local.login_manager.logout()
    return {"logged_out": True}


@frappe.whitelist()
def me() -> dict:
    """Identity payload for the logged-in user (plan §10.1 ``api.auth.me``)."""
    user = frappe.session.user
    if user in ("Guest", None):
        frappe.throw(_("Not logged in."), frappe.AuthenticationError)

    user_doc = frappe.get_cached_doc("User", user)
    roles = emp_utils.get_user_roles(user)
    portal_role = emp_utils.portal_role(roles)
    employee_name = emp_utils.get_employee_for_user(user)

    employee = None
    if employee_name:
        emp = frappe.get_cached_doc("Employee", employee_name)
        employee = {
            "name": emp.name,
            "employee_name": emp.employee_name,
            "employee_number": getattr(emp, "employee_number", None),
            "company": emp.company,
            "branch": getattr(emp, "branch", None),
            "department": getattr(emp, "department", None),
            "designation": getattr(emp, "designation", None),
            "employment_type": getattr(emp, "employment_type", None),
            "gender": getattr(emp, "gender", None),
            "date_of_birth": getattr(emp, "date_of_birth", None),
            "date_of_joining": getattr(emp, "date_of_joining", None),
            "cell_number": getattr(emp, "cell_number", None),
            "reports_to": getattr(emp, "reports_to", None),
            "user_id": getattr(emp, "user_id", None),
        }

    return {
        "user": user,
        "full_name": user_doc.full_name,
        "email": user_doc.email,
        "username": user_doc.username,
        "user_image": user_doc.user_image,
        "roles": roles,
        "role": portal_role,
        "employee": employee,
        "portal_timezone": tz_utils.get_portal_timezone(),
        "home_page": _home_page_for_role(portal_role),
    }


def _home_page_for_role(role: str) -> str:
    """Landing route per coarse portal role (matches hr-ui router redirects)."""
    if role == "Manager":
        return "/approvals"
    if role == "HRUser":
        return "/hr/employees"
    return "/dashboard"


def get_boot_data(bootinfo: dict) -> None:
    """``boot_session`` hook: inject portal timezone + enable flags into boot."""
    try:
        setting = frappe.get_cached_doc("VN HR Portal Setting", "VN HR Portal Setting")
        bootinfo.portal_timezone = setting.timezone or tz_utils.DEFAULT_PORTAL_TZ
        bootinfo.gege_hr = {
            "enable_mobile_checkin": bool(setting.enable_mobile_checkin),
            "require_geolocation": bool(setting.require_geolocation),
            "require_selfie": bool(setting.require_selfie),
            "enable_device_sync": bool(setting.enable_device_sync),
            "enable_employee_self_service": bool(setting.enable_employee_self_service),
            "enable_manager_dashboard": bool(setting.enable_manager_dashboard),
        }
    except Exception:
        bootinfo.portal_timezone = tz_utils.DEFAULT_PORTAL_TZ
