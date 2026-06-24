from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.model.document import Document


class VNSalaryAdvancePolicy(Document):
    """Master rule book governing how much salary an employee may draw in
    advance (plan v5 §16 / doctype-design §22).

    A policy is scoped (company / branch / employee group) and caps the advance
    by the smaller of:

      * ``max_percentage`` % of base salary, or
      * ``max_fixed_amount`` (when set).

    It also gates eligibility with ``min_working_days`` (worked in the period),
    ``max_requests_per_month`` and ``cutoff_day`` (after which no new advance
    may be requested that month). The VN Salary Advance Request DocType
    resolves the best-matching policy and auto-calculates ``eligible_amount``.
    """

    def validate(self):
        self._validate_percentage()
        self._validate_cutoff_day()

    def _validate_percentage(self):
        try:
            pct = float(self.max_percentage or 0)
        except (TypeError, ValueError):
            pct = 0
        if pct < 0 or pct > 100:
            frappe.throw(_("Max Percentage phải nằm trong khoảng 0–100."))

    def _validate_cutoff_day(self):
        try:
            day = int(self.cutoff_day or 0)
        except (TypeError, ValueError):
            day = 0
        if day < 1 or day > 31:
            frappe.throw(_("Cutoff Day phải nằm trong khoảng 1–31."))
