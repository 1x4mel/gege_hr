from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime

from gege_hr.gege_hr.utils.naming import set_yymmdd_name


class VNOvertimeRequest(Document):
    """Employee request to work overtime for one shift day (plan §13 / v5 §9.4).

    The engine (:func:`calc.match_overtime_request`) overlaps the approved
    ``from_datetime``/``to_datetime`` window against the actual OT the employee
    worked, so only the hours actually served (capped by the request) count as
    ``approved_overtime_hours`` on the Work Session — gate-kept by the policy's
    ``require_overtime_approval`` flag.

    Lifecycle is workflow-driven (``workflow_state``): Draft → Pending Manager →
    Pending HR → Approved → Confirmed (or Rejected). Only ``Approved``/
    ``Confirmed`` rows are picked up by the calculation engine.
    """

    # ------------------------------------------------------------------ #
    # Lifecycle hooks
    # ------------------------------------------------------------------ #
    def before_insert(self):
        set_yymmdd_name(self, "before_insert")

    def validate(self):
        self._normalize_employee_name()
        self._normalize_company()
        self._validate_window()
        self._validate_requested_hours()
        self._validate_no_duplicate()

    def on_submit(self):
        # Doc is only effective when it reaches an approved state. The actual
        # workflow transitions set ``workflow_state``; submitting locks the doc.
        if self.workflow_state not in ("Approved", "Confirmed"):
            self.workflow_state = "Approved"

    # ------------------------------------------------------------------ #
    # Validation helpers
    # ------------------------------------------------------------------ #
    def _normalize_employee_name(self):
        if self.employee and not self.employee_name:
            self.employee_name = frappe.db.get_value("Employee", self.employee, "employee_name")

    def _normalize_company(self):
        if not self.company and self.employee:
            self.company = frappe.db.get_value("Employee", self.employee, "company")

    def _validate_window(self):
        """``to_datetime`` must be strictly after ``from_datetime``.

        Frappe stores datetimes as UTC strings; ``get_datetime`` normalises them
        so the comparison is timezone-correct regardless of how the value was
        bound (string vs. datetime).
        """
        if not self.from_datetime or not self.to_datetime:
            frappe.throw(_("Vui lòng nhập From và To hợp lệ."))
        if get_datetime(self.to_datetime) <= get_datetime(self.from_datetime):
            frappe.throw(_("'To' phải lớn hơn 'From'."))

    def _validate_requested_hours(self):
        try:
            requested = float(self.requested_hours or 0)
        except (TypeError, ValueError):
            requested = 0
        if requested <= 0:
            frappe.throw(_("Requested Hours phải lớn hơn 0."))
        # Cap against the policy max (plan §13 validation).
        max_hours = self._policy_max_ot_hours()
        if max_hours and requested > max_hours:
            frappe.throw(
                _("Requested Hours ({0}) vượt Max OT Hours Per Shift ({1}).").format(requested, max_hours)
            )

    def _policy_max_ot_hours(self) -> float:
        """Resolve the policy cap (``max_overtime_hours_per_shift``) for the OT
        day. Returns ``0`` when no policy is resolvable (skip the cap)."""
        try:
            policy_name = (
                frappe.db.get_value("VN Employee Shift Instance", self.shift_instance, "attendance_policy")
                if self.shift_instance
                else None
            )
            if policy_name:
                return float(
                    frappe.db.get_value("VN Attendance Policy", policy_name, "max_overtime_hours_per_shift")
                    or 0
                )
        except Exception:
            pass
        return 0

    def _validate_no_duplicate(self):
        """No open duplicate ``(employee, work_date, overtime_type)`` (plan §13).

        Rows already Rejected/Cancelled don't count, so an employee can re-file.
        """
        existing = frappe.db.exists(
            "VN Overtime Request",
            {
                "employee": self.employee,
                "work_date": self.work_date,
                "overtime_type": self.overtime_type,
                "workflow_state": ["not in", ["Rejected"]],
                "docstatus": ["<", 2],
                "name": ["!=", self.name or ""],
            },
        )
        if existing:
            frappe.throw(_("Đã tồn tại OT Request {0} cho nhân viên này trong ngày.").format(existing))
