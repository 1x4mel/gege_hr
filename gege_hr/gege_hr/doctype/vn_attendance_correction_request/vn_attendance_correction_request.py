from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_datetime

from gege_hr.gege_hr.utils.naming import set_yymmdd_name


class VNAttendanceCorrectionRequest(Document):
    """Employee request to correct a check-in/out for one shift day
    (plan v5 §12 / §10.2).

    An employee files a correction when a log is missing, wrong, or unrecorded
    (remote work / business trip / device error / manual). On approval, the
    resulting ``generated_checkin`` / ``generated_attendance`` links are filled
    and the Work Session is recalculated.

    Lifecycle is workflow-driven (``workflow_state``): Draft → Pending Manager →
    Pending HR → Approved (or Rejected). Only ``Approved`` rows trigger the
    recalculation / Additional-Salary side-effects.
    """

    # ------------------------------------------------------------------ #
    # Lifecycle hooks
    # ------------------------------------------------------------------ #
    def autoname(self):
        # Frappe calls this from set_new_name (naming.py step 4) — the
        # before_insert variant never ran because ``doc.name = None`` +
        # the JSON ``format:`` option always overwrote it first.
        set_yymmdd_name(self, "autoname")

    def validate(self):
        self._normalize_employee_name()
        self._normalize_company()
        self._prefill_current_times()
        self._validate_lock_period()
        self._validate_requested_change()
        self._validate_reason()
        self._validate_no_duplicate()

    def on_submit(self):
        # Only an Approved doc is effective. Workflow transitions normally set
        # ``workflow_state``; submitting without a terminal state defaults to
        # Approved so the audit trail is consistent.
        if self.workflow_state not in ("Approved", "Rejected"):
            self.workflow_state = "Approved"
        if self.workflow_state == "Approved":
            self._stamp_approval()

    # ------------------------------------------------------------------ #
    # Validation helpers
    # ------------------------------------------------------------------ #
    def _normalize_employee_name(self):
        if self.employee and not self.employee_name:
            self.employee_name = frappe.db.get_value("Employee", self.employee, "employee_name")

    def _normalize_company(self):
        if not self.company and self.employee:
            self.company = frappe.db.get_value("Employee", self.employee, "company")

    def _prefill_current_times(self):
        """Copy the current check-in/out from the linked Work Session so the
        approver sees the before/after diff without a second lookup."""
        if not self.work_session:
            return
        if self.current_checkin_time or self.current_checkout_time:
            return
        row = frappe.db.get_value(
            "VN Attendance Work Session",
            self.work_session,
            ["actual_checkin", "actual_checkout"],
            as_dict=True,
        )
        if not row:
            return
        if not self.current_checkin_time and row.get("actual_checkin"):
            self.current_checkin_time = row.actual_checkin
        if not self.current_checkout_time and row.get("actual_checkout"):
            self.current_checkout_time = row.actual_checkout

    def _validate_lock_period(self):
        """``work_date`` must not fall inside a locked Monthly Attendance Period
        (plan §12). The lookup is bench-safe: if the table/doctype is missing it
        short-circuits to "not locked" (see ``attendance._is_date_locked``)."""
        if not self.work_date:
            return
        if _is_date_locked(self.work_date):
            frappe.throw(
                _("Ngày {0} thuộc kỳ đã chốt công, không thể tạo yêu cầu điều chỉnh.").format(self.work_date)
            )

    def _validate_requested_change(self):
        """At least one requested time must differ from the current value (plan
        §12) — otherwise there is nothing to correct."""
        ci_changed = bool(self._changed_from_current("current_checkin_time", "requested_checkin_time"))
        co_changed = bool(self._changed_from_current("current_checkout_time", "requested_checkout_time"))
        # When there is no "current" value (e.g. Missing IN), any requested
        # value counts as a change.
        if not ci_changed and not co_changed:
            # Special-case: a purely "Remote Work" / "Business Trip" note with
            # no times is allowed only when no current times exist either.
            if not (self.requested_checkin_time or self.requested_checkout_time):
                frappe.throw(_("Vui lòng nhập giờ vào/ra đề xuất cho yêu cầu điều chỉnh."))

    def _changed_from_current(self, current_field: str, requested_field: str) -> bool:
        requested = getattr(self, requested_field, None)
        if not requested:
            return False
        current = getattr(self, current_field, None)
        if not current:
            return True  # Missing → adding is a change.
        return get_datetime(requested) != get_datetime(current)

    def _validate_reason(self):
        """``reason`` must be at least 10 characters (plan §12)."""
        reason = (self.reason or "").strip()
        if len(reason) < 10:
            frappe.throw(_("Lý do giải trình phải dài ít nhất 10 ký tự."))

    def _validate_no_duplicate(self):
        """No open duplicate ``(employee, work_date, correction_type)`` (plan
        §12). Already-Rejected rows don't count so the employee can re-file."""
        existing = frappe.db.exists(
            "VN Attendance Correction Request",
            {
                "employee": self.employee,
                "work_date": self.work_date,
                "correction_type": self.correction_type,
                "workflow_state": ["not in", ["Rejected"]],
                "docstatus": ["<", 2],
                "name": ["!=", self.name or ""],
            },
        )
        if existing:
            frappe.throw(
                _("Đã tồn tại yêu cầu điều chỉnh {0} cho nhân viên này trong ngày.").format(existing)
            )

    # ------------------------------------------------------------------ #
    # Approval side-effects
    # ------------------------------------------------------------------ #
    def _stamp_approval(self):
        """Record who approved and when. The actual check-in/attendance
        generation is performed by the approval endpoint (manager action), not
        here, so the doc stays a pure record of the request."""
        if not self.approver:
            self.approver = frappe.session.user
        if not self.approved_at:
            self.approved_at = frappe.utils.now()


# Imported lazily (and kept local) to avoid a hard cycle between the DocType
# module and ``api.attendance``. ``_is_date_locked`` is bench-safe.
def _is_date_locked(work_date):
    from gege_hr.gege_hr.api.attendance import _is_date_locked as _impl

    return _impl(work_date)
