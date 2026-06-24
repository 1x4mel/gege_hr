from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.model.document import Document


class VNPayrollComponentMapping(Document):
    """Maps an attendance segment type to an ERPNext Salary Component (plan §19).

    Each record is one rule (segment type × day type → component + multiplier +
    formula). The calculation engine (:func:`calc._load_holiday_multipliers`)
    resolves all active rules for the policy's company into a
    ``{segment_type: multiplier}`` override map that :func:`calc.get_holiday_multiplier`
    consults before falling back to its built-in defaults.

    Later phases read ``salary_component`` + ``formula_type`` to generate
    ``Additional Salary`` rows; this DocType only stores the rule book.
    """

    def validate(self):
        self._validate_custom_formula()
        self._validate_unique_rule()

    def _validate_custom_formula(self):
        if self.formula_type == "Custom" and not self.formula:
            frappe.throw(_("Vui lòng nhập Formula khi Formula Type = Custom."))

    def _validate_unique_rule(self):
        """One active rule per ``(company, segment_type, day_type)`` so the
        engine's resolution is deterministic."""
        existing = frappe.db.exists(
            "VN Payroll Component Mapping",
            {
                "company": self.company,
                "segment_type": self.segment_type,
                "day_type": self.day_type or "All",
                "is_active": 1,
                "name": ["!=", self.name or ""],
            },
        )
        if existing:
            frappe.throw(
                _("Đã có rule active cho Company {0} / Segment {1} / Day {2}: {3}.").format(
                    self.company, self.segment_type, self.day_type or "All", existing
                )
            )
