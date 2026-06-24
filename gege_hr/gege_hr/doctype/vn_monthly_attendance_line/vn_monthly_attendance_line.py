from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.model.document import Document

__all__ = ["VNMonthlyAttendanceLine"]


class VNMonthlyAttendanceLine(Document):
    """One row of a VN Monthly Attendance Period — an employee's day & hour
    summary for the period (plan v5 §9.6 / doctype-design §17).

    Aggregated by :mod:`gege_hr.gege_hr.api.attendance_period` using the pure
    helpers in :mod:`gege_hr.gege_hr.utils.attendance_period`. The line status
    flows ``Draft → Confirmed → Adjusted → Locked``; a period cannot be locked
    while any of its lines is still ``Draft``.
    """

    STATUS_DRAFT = "Draft"
    STATUS_CONFIRMED = "Confirmed"
    STATUS_ADJUSTED = "Adjusted"
    STATUS_LOCKED = "Locked"

    def validate(self):
        self._normalize_employee()
        self._validate_unique_employee()

    # ------------------------------------------------------------------ #
    # Validations
    # ------------------------------------------------------------------ #
    def _normalize_employee(self):
        if not self.employee:
            return
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
        """One line per (period, employee) — §17 unique index, in-app guard."""
        if not (self.attendance_period and self.employee):
            return
        exists = frappe.db.exists(
            "VN Monthly Attendance Line",
            {
                "attendance_period": self.attendance_period,
                "employee": self.employee,
                "name": ["!=", self.name or ""],
                "docstatus": ["<", 2],
            },
        )
        if exists:
            frappe.throw(
                _("An attendance line for employee {0} already exists in this period.").format(self.employee)
            )
