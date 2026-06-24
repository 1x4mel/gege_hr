from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.model.document import Document


class VNApprovalMatrix(Document):
    """Approval routing matrix (plan v5 §10.6 / doctype-design §14).

    One matrix record defines, for a (transaction_type, company, scope) tuple,
    the ordered list of approval steps (:doc:`VN Approval Step`). The approval
    inbox (``gege_hr.gege_hr.api.approval``) resolves the active matrix for a
    request and walks its steps as approvers act.

    Scoping (``apply_to`` = All / Branch / Department / Employee Grade) lets a
    company keep different routing for, say, branch vs. head office. The most
    specific active matrix wins; ``All`` is the fallback.
    """

    # ------------------------------------------------------------------ #
    # Lifecycle hooks
    # ------------------------------------------------------------------ #
    def validate(self):
        self._validate_scope_fields()
        self._validate_steps()

    # ------------------------------------------------------------------ #
    # Validation helpers
    # ------------------------------------------------------------------ #
    def _validate_scope_fields(self):
        """Only the field matching ``apply_to`` should be set (clean rest)."""
        scope_map = {
            "All": ("branch", "department", "employee_grade"),
            "Branch": ("department", "employee_grade"),
            "Department": ("branch", "employee_grade"),
            "Employee Grade": ("branch", "department"),
        }
        for f in scope_map.get(self.apply_to, ()):
            if self.get(f):
                self.set(f, None)

    def _validate_steps(self):
        """Steps must be ordered and each approver_type well-formed."""
        if not self.steps:
            frappe.throw(_("Cần ít nhất một bước duyệt."))

        seen = set()
        for i, step in enumerate(self.steps, start=1):
            # Auto-number if blank/out of order.
            if not step.step_no or step.step_no != i:
                step.step_no = i

            if step.approver_type in ("Specific User", "Specific Role"):
                target = step.approver_user if step.approver_type == "Specific User" else step.approver_role
                if not target:
                    frappe.throw(_("Bước {0}: cần chỉ định User/Role cho kiểu approver.").format(i))

            key = (step.approver_type, step.approver_user, step.approver_role)
            # Allow identical consecutive role-based steps but warn on exact dups.
            seen.add(key)
