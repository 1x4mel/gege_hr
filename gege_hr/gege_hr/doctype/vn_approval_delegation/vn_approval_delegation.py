# For license information, see LICENSE.txt / NOTICE.txt in the gege_hr app.

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate

# Maximum delegation window (plans/approvals-deskfree-complete §7 — abuse
# mitigation: delegation is a short-horizon vacation/coverage tool, not a
# permanent re-routing of the approval matrix).
_MAX_SPAN_DAYS = 31

_HR_ROLES = {"HR Manager", "HR User", "System Manager"}


class VNApprovalDelegation(Document):
    """Approver vacation coverage — "from_user ủy quyền duyệt cho to_user".

    Created desk-free from the approval inbox (``gege_hr.gege_hr.api.approval.
    delegate_request`` or ``gege_hr.gege_hr.api.delegation.save_delegation``).
    The approval inbox expands the current step's holder set with active
    delegations (``_delegated_users``), so the delegate sees + may act on the
    requests; every decision still logs the ACTING user in ``VN Approval Log``.
    """

    def validate(self):
        self.from_user = (self.from_user or "").strip()
        self.to_user = (self.to_user or "").strip()
        if not self.from_user or not self.to_user:
            frappe.throw(_("Thiếu người ủy quyền / người nhận ủy quyền."))
        if self.from_user == self.to_user:
            frappe.throw(_("Không thể ủy quyền cho chính mình."))
        if self.to_user in ("Guest", "Administrator"):
            frappe.throw(_("Người nhận ủy quyền không hợp lệ."))
        if not self.from_date or not self.to_date:
            frappe.throw(_("Thiếu thời hạn ủy quyền."))
        if getdate(self.to_date) < getdate(self.from_date):
            frappe.throw(_("Ngày kết thúc phải sau ngày bắt đầu."))
        span = (getdate(self.to_date) - getdate(self.from_date)).days
        if span > _MAX_SPAN_DAYS:
            frappe.throw(_("Chỉ ủy quyền tối đa {0} ngày.").format(_MAX_SPAN_DAYS))
        # Only the delegator themself (or HR) may mint a delegation — nobody
        # can volunteer to receive someone else's authority.
        session_user = frappe.session.user
        if session_user not in ("Administrator", self.from_user):
            roles = set(frappe.get_roles(session_user) or [])
            if not (roles & _HR_ROLES):
                frappe.throw(
                    _("Chỉ chính người ủy quyền hoặc HR mới tạo được ủy quyền này."),
                    frappe.PermissionError,
                )
