from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.model.document import Document

# HR roles that the gege_hr approval matrix authorises to act on ANY cancellation
# request (read + write + submit), regardless of ownership. Without these hooks
# doc-level permission resolves False for them (User Permission / link
# restrictions override the bare role grant), which blocked the matrix-driven
# approval inbox. The employee (owner) keeps Frappe's default owner permission.
_HR_APPROVER_ROLES = {"HR Manager", "HR User"}


def has_permission(doc, ptype="read", user=None):
    if not user:
        user = frappe.session.user
    if user == "Administrator":
        return True
    try:
        roles = set(frappe.get_roles(user))
    except Exception:
        return None
    if roles & _HR_APPROVER_ROLES:
        return True
    return None  # defer to Frappe's default (owner / role) for everyone else


def get_permission_query_conditions(user):
    if not user:
        user = frappe.session.user
    try:
        roles = set(frappe.get_roles(user))
    except Exception:
        roles = set()
    if roles & (_HR_APPROVER_ROLES | {"Administrator"}):
        return ""  # no extra restriction — sees every request
    return ""  # others fall back to the standard owner-scoped role permission


class VNLeaveCancellationRequest(Document):
    """Request to cancel an already-approved Leave Application
    (plan v5 §11.5 / doctype-design §28).

    Once a leave is approved, the employee cannot just delete it — they must
    file a cancellation request which routes through the same matrix-driven
    approval inbox as the original leave (``workflow_state`` Draft → Pending
    Manager → Pending HR → Approved / Rejected). On ``Approved`` the linked
    Leave Application is cancelled and the affected attendance / work session
    is flagged for recalculation. The ``vn_cancellation_request`` custom field
    on Leave Application (Phần A.6) is back-linked to the approved request.
    """

    def before_insert(self):
        self._stamp_requester()

    def validate(self):
        self._normalize_employee_name()
        self._normalize_from_leave_application()
        self._validate_reason()

    # ------------------------------------------------------------------ #
    # Validation helpers
    # ------------------------------------------------------------------ #
    def _stamp_requester(self):
        if not self.requested_by and frappe:
            try:
                self.requested_by = frappe.session.user
            except Exception:  # pragma: no cover — defensive
                pass

    def _normalize_employee_name(self):
        if self.employee_name:
            return
        if not self.employee:
            return
        try:
            self.employee_name = frappe.db.get_value("Employee", self.employee, "employee_name") or ""
        except Exception:  # pragma: no cover — bench-only
            self.employee_name = ""

    def _normalize_from_leave_application(self):
        """Derive employee / work_date / shift_instance from the linked leave."""
        if not self.leave_application:
            return
        try:
            doc = frappe.db.get_value(
                "Leave Application",
                self.leave_application,
                ["employee", "from_date"],
                as_dict=True,
            )
        except Exception:  # pragma: no cover — bench-only
            doc = None
        if not doc:
            return
        if not self.employee:
            self.employee = doc.employee
        if not self.work_date:
            self.work_date = getattr(doc, "from_date", None)

    def _validate_reason(self):
        if not (self.reason or "").strip():
            frappe.throw(_("Reason is required."))
