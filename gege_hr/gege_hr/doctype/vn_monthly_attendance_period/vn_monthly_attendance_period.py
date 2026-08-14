from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate

from gege_hr.gege_hr.utils.naming import set_yymmdd_name

__all__ = ["VNMonthlyAttendancePeriod"]


class VNMonthlyAttendancePeriod(Document):
    """A monthly attendance closing cycle for one company (plan v5 §9.6 /
    doctype-design §16).

    Lifecycle: ``Draft → Generated → Locked`` (``Unlocked`` is a transient
    post-Lock state). Each period owns one VN Monthly Attendance Line per
    employee, aggregated by :mod:`gege_hr.gege_hr.api.attendance_period`.
    """

    STATUS_DRAFT = "Draft"
    STATUS_GENERATED = "Generated"
    STATUS_LOCKED = "Locked"
    STATUS_UNLOCKED = "Unlocked"

    ALLOWED_TRANSITIONS = {
        STATUS_DRAFT: {STATUS_GENERATED},
        STATUS_GENERATED: {STATUS_LOCKED, STATUS_DRAFT},
        STATUS_LOCKED: {STATUS_UNLOCKED},
        # Unlocked may re-lock directly, or go back to Generated to refresh
        # its aggregated lines (``generate_lines`` after an unlock).
        STATUS_UNLOCKED: {STATUS_LOCKED, STATUS_GENERATED},
    }

    def before_insert(self):
        set_yymmdd_name(self, "before_insert")

    def validate(self):
        self._normalize_dates()
        self._validate_month_year()
        self._validate_transition()

    # ------------------------------------------------------------------ #
    # Validations
    # ------------------------------------------------------------------ #
    def _normalize_dates(self):
        if self.from_date and self.to_date:
            if getdate(self.to_date) < getdate(self.from_date):
                frappe.throw(_("To Date cannot be before From Date."))

    def _validate_month_year(self):
        """``payroll_month`` / ``payroll_year`` must match the date range."""
        if not (self.from_date and self.to_date and self.payroll_year):
            return
        start = getdate(self.from_date)
        end = getdate(self.to_date)
        expected_month = f"{start.month:02d}"
        if self.payroll_month and self.payroll_month != expected_month:
            frappe.throw(
                _("Payroll Month {0} does not match From Date {1}.").format(
                    self.payroll_month, start.strftime("%Y-%m-%d")
                )
            )
        if int(self.payroll_year) not in (start.year, end.year):
            frappe.throw(_("Payroll Year {0} does not match the date range.").format(self.payroll_year))

    def _validate_transition(self):
        """Guard status changes against :data:`ALLOWED_TRANSITIONS`.

        Skip on insert (before the DB has the previous status) and ignore the
        very first save where the doc is still ``Draft``.
        """
        previous = self.get_doc_before_save()
        old = getattr(previous, "status", None) if previous else None
        if not old or old == self.status:
            return
        if self.status not in self.ALLOWED_TRANSITIONS.get(old, set()):
            frappe.throw(_("Invalid status transition: {0} → {1}.").format(old, self.status))
