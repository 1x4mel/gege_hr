from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.model.document import Document


class VNLeaveStaffingRule(Document):
    """Minimum-staffing rule enforcing coverage (plan v5 §11.7 /
    doctype-design §30).

    Scoped by company / branch / department and optionally a specific Shift Type.
    When an employee applies for leave, the engine counts already-approved leave
    for the same scope+day and, against ``min_required_employees`` /
    ``max_leave_allowed``, emits the configured ``rule_action`` (Warning /
    Block / Require HR Approval). Block is the only hard gate; Warning surfaces
    a preview note, Require HR Approval forces the request through the HR step
    regardless of the matrix.
    """

    def validate(self):
        self._validate_min_required()

    def _validate_min_required(self):
        try:
            minimum = int(self.min_required_employees or 0)
        except (TypeError, ValueError):
            minimum = 0
        if minimum < 0:
            frappe.throw(_("Min Required Employees cannot be negative."))
