from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.model.document import Document

__all__ = ["VNPayrollReviewLine"]


class VNPayrollReviewLine(Document):
    """One row of a VN Payroll Review Period — an employee's computed salary
    breakdown for the period (plan v5 §9.6 / doctype-design §21).

    Amounts are populated by :mod:`gege_hr.gege_hr.api.payroll` using the pure
    helpers in :mod:`gege_hr.gege_hr.utils.payroll`. The line status flows
    ``Draft → Confirmed → Adjusted → Approved``; a period cannot be approved
    while any of its lines is still ``Draft``.
    """

    STATUS_DRAFT = "Draft"
    STATUS_CONFIRMED = "Confirmed"
    STATUS_ADJUSTED = "Adjusted"
    STATUS_APPROVED = "Approved"

    def validate(self):
        self._normalize_employee()
        self._validate_unique_employee()

    # ------------------------------------------------------------------ #
    # Validations
    # ------------------------------------------------------------------ #
    def _normalize_employee(self):
        if self.employee:
            if not self.employee_name:
                self.employee_name = frappe.db.get_value("Employee", self.employee, "employee_name")
            emp = frappe.db.get_value(
                "Employee",
                self.employee,
                ["department", "branch", "company"],
                as_dict=True,
            )
            if emp:
                self.department = self.department or emp.department
                self.branch = self.branch or emp.branch
                self.company = self.company or emp.company

    def _validate_unique_employee(self):
        """One line per (period, employee) — §21 unique index, enforced in-app."""
        if not (self.payroll_review_period and self.employee):
            return
        exists = frappe.db.exists(
            "VN Payroll Review Line",
            {
                "payroll_review_period": self.payroll_review_period,
                "employee": self.employee,
                "name": ["!=", self.name or ""],
                "docstatus": ["<", 2],
            },
        )
        if exists:
            frappe.throw(
                _("A payroll review line for employee {0} already exists in this period.").format(
                    self.employee
                )
            )
