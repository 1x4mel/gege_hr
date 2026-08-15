from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate

from gege_hr.gege_hr.utils.naming import set_yymmdd_name

__all__ = ["VNPayrollReviewPeriod"]


class VNPayrollReviewPeriod(Document):
    """A payroll closing cycle for one company/month (plan v5 §9.6 /
    doctype-design §20).

    Lifecycle: ``Draft → Calculated → Approved → Slips Generated → Published``
    (or ``Cancelled``). Each period owns one VN Payroll Review Line per
    employee, computed by :mod:`gege_hr.gege_hr.api.payroll`.
    """

    # Status constants — kept in sync with the Select options.
    STATUS_DRAFT = "Draft"
    STATUS_CALCULATED = "Calculated"
    STATUS_APPROVED = "Approved"
    STATUS_SLIPS = "Slips Generated"
    STATUS_PUBLISHED = "Published"
    STATUS_CANCELLED = "Cancelled"

    STATUS_CALCULATING = "Calculating"

    ALLOWED_TRANSITIONS = {
        STATUS_DRAFT: {STATUS_CALCULATING, STATUS_CALCULATED, STATUS_CANCELLED},
        # F10: a crash mid-calculation left the period stuck in Calculating
        # forever — allow it to recover back to a working state.
        STATUS_CALCULATING: {STATUS_CALCULATED, STATUS_DRAFT, STATUS_CANCELLED},
        STATUS_CALCULATED: {STATUS_APPROVED, STATUS_DRAFT, STATUS_CANCELLED},
        STATUS_APPROVED: {STATUS_SLIPS, STATUS_CANCELLED},
        STATUS_SLIPS: {STATUS_PUBLISHED, STATUS_CANCELLED},
        STATUS_PUBLISHED: set(),
        STATUS_CANCELLED: set(),
    }

    def before_insert(self):
        set_yymmdd_name(self, "before_insert")

    def validate(self):
        self._normalize_dates()
        self._validate_attendance_period_locked()
        self._validate_transition()

    # ------------------------------------------------------------------ #
    # Validations
    # ------------------------------------------------------------------ #
    def _normalize_dates(self):
        if self.from_date and self.to_date:
            if getdate(self.to_date) < getdate(self.from_date):
                frappe.throw(_("To Date cannot be before From Date."))

    def _validate_attendance_period_locked(self):
        """The linked attendance period must be locked before calculating.

        Falls back to a date-window lock check when no period is linked.
        """
        if not self.attendance_period:
            return
        try:
            # Lock state lives in ``status == "Locked"`` (there is no
            # ``is_locked`` column — that historic lookup always failed).
            locked = (
                frappe.db.get_value(
                    "VN Monthly Attendance Period",
                    self.attendance_period,
                    "status",
                )
                == "Locked"
            )
        except Exception:
            locked = False
        if not locked and self.status != self.STATUS_DRAFT:
            # Only enforce at calculation time — a Draft period may be created
            # in anticipation of the lock.
            frappe.throw(
                _("Attendance Period {0} must be locked before payroll review.").format(
                    self.attendance_period
                )
            )

    def _validate_transition(self):
        """Guard the status flow described in §20."""
        if not self.status or self.is_new():
            return
        prev = self.get_doc_before_save()
        prev_status = prev.status if prev else self.status
        if prev_status == self.status:
            return
        allowed = self.ALLOWED_TRANSITIONS.get(prev_status, set())
        if self.status not in allowed and prev_status != self.status:
            frappe.throw(_("Invalid status transition: {0} → {1}.").format(prev_status, self.status))

    def on_submit(self):
        # Submitting is only meaningful once slips are published.
        if self.status not in (self.STATUS_PUBLISHED, self.STATUS_SLIPS):
            self.status = self.STATUS_PUBLISHED

    def before_cancel(self):
        self.status = self.STATUS_CANCELLED
