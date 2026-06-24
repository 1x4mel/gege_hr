from __future__ import unicode_literals

from frappe.model.document import Document


class VNLeavePolicyExtension(Document):
    """Per-leave-type rules layered on top of Frappe ``Leave Type``
    (plan v5 §11.4 / doctype-design §27).

    Scoped by company / branch / department / employee grade. Captures the
    Vietnamese-specific leave behaviour the engine consults when an employee
    applies for leave: minimum notice, max consecutive shifts, half-shift /
    custom-hours toggles, attachment & handover requirements, approval routing
    flags, whether an already-approved leave may be cancelled (and whether that
    cancellation needs approval), whether to block while the period is locked,
    and the salary-impact type (Paid / Unpaid / Half Paid) consumed by the
    payroll/closing aggregation.
    """

    def validate(self):
        self._normalize_scope_fields()

    def _normalize_scope_fields(self):
        """Clear scope fields that don't apply to the chosen ``apply_to``."""
        active = (self.apply_to or "All").strip()
        scope_map = {
            "All": [],
            "Branch": ["department", "employee_grade"],
            "Department": ["branch", "employee_grade"],
            "Employee Grade": ["branch", "department"],
        }
        for field in scope_map.get(active, []):
            if getattr(self, field, None):
                setattr(self, field, None)
