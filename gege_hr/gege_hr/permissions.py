"""Shared permission hooks for the gege_hr matrix-driven approval inbox.

The approval matrix (``VN Approval Matrix``) authorises **HR Manager / HR User**
roles to act on ANY pending request of the matrix doctypes (Salary Advance /
Overtime / Correction / Leave Cancellation / Expense / Grievance / Travel /
Leave Encashment / Comp-Off), regardless of which employee filed it. Frappe's
default per-employee ``User Permission`` / ``permission_query_conditions``
restricts a user to their own (or their reports') rows, which BLOCKS the inbox
(``approval.approve_request``/``reject_request`` load via ``frappe.get_doc`` and
persist via ``doc.save`` — both permission-checked).

These hooks grant the matrix-authorised roles access-to-all so the inbox works
through the **proper Frappe flow** (``validate`` + ``on_update``/``on_submit`` →
logs / ledger / side-effects), WITHOUT any ``ignore_permissions`` bypass.

Register per-doctype in ``hooks.py``::

    from gege_hr.gege_hr.permissions import MATRIX_DOCTYPES
    has_permission = {dt: "gege_hr.gege_hr.permissions.has_permission" for dt in MATRIX_DOCTYPES}
    permission_query_conditions = {dt: "gege_hr.gege_hr.permissions.permission_query_conditions" for dt in MATRIX_DOCTYPES}
"""
from __future__ import annotations

import frappe

# Roles the approval matrix treats as approvers. ``Administrator`` is always
# allowed (checked in :func:`_is_hr_approver`).
_HR_APPROVER_ROLES = {"HR Manager", "HR User"}

# Doctypes routed through the gege_hr approval inbox (matrix-driven). Each gets
# the has_permission / permission_query_conditions hooks so HR Manager / HR User
# can read + advance ANY pending request of these types via the unified inbox.
MATRIX_DOCTYPES = [
    "VN Salary Advance Request",
    "VN Overtime Request",
    "VN Attendance Correction Request",
    "VN Leave Cancellation Request",
    # HRMS-backed request types the inbox also serves (approve/reject from /hr/approvals):
    "Expense Claim",
    "Employee Grievance",
    "Travel Request",
    "Leave Encashment",
    "Compensatory Leave Request",
]


def _is_hr_approver(user: str | None) -> bool:
    if not user:
        user = frappe.session.user
    if user == "Administrator":
        return True
    try:
        return bool(set(frappe.get_roles(user)) & _HR_APPROVER_ROLES)
    except Exception:
        return False


def has_permission(doc, ptype="read", user=None):
    """Grant HR Manager / HR User access to ANY row of the matrix doctypes.

    Returns ``True`` for those roles, ``None`` for everyone else (defer to
    Frappe's default owner/role permission — employees still see only their own).
    """
    if _is_hr_approver(user):
        return True
    return None


def permission_query_conditions(user):
    """HR Manager / HR User see every row (no extra SQL restriction).

    Others return "" too — Frappe then applies the standard role/owner permission
    (employees are scoped to their own via the doctype's existing perm rules).
    """
    return ""
