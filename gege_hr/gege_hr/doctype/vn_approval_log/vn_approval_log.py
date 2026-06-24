from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.model.document import Document

from gege_hr.gege_hr.utils import employee as emp_utils


class VNApprovalLog(Document):
    """Immutable audit trail for approval actions (plan v5 §10.6 / §15).

    Created by the approval inbox (``gege_hr.gege_hr.api.approval``) on every
    workflow transition (Submit / Approve / Reject / Cancel / Delegate). Rows are
    append-only — the DocType has no submit lifecycle; it only records *what
    happened, by whom, when, and the from/to states*. This keeps a complete,
    queryable history for any request regardless of its own DocType.
    """

    def before_insert(self):
        # Stamp the actor's employee for easier per-employee reporting.
        if not self.actor:
            self.actor = emp_utils.get_current_user()
        if not self.actor_employee:
            self.actor_employee = emp_utils.get_employee_for_user(self.actor)
        if not self.reference_doctype or not self.reference_name:
            frappe.throw(_("Approval Log cần Reference DocType + Name."))
