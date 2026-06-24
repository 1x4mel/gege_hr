from __future__ import unicode_literals

from datetime import timedelta

import frappe
from frappe import _
from frappe.model.document import Document

from gege_hr.gege_hr.utils import tz as tz_utils
from gege_hr.gege_hr.utils.naming import set_yymmdd_name


class VNEmployeeShiftInstance(Document):
    """A concrete, dated occurrence of a shift for one employee.

    Generated daily from ``Shift Assignment`` (see ``api.shift.generate_daily_shift_instances``).
    ``work_date`` is the portal-TZ calendar date the shift STARTS on — not the
    UTC storage date (plan §2.7 / §3.2). All planned datetimes are stored as UTC
    but derived from portal-local ``planned_window``.
    """

    def before_insert(self):
        set_yymmdd_name(self, "before_insert")

    def validate(self):
        self._normalize_employee_name()
        self._compute_derived_times()

        if self.planned_end and self.planned_start and self.planned_end <= self.planned_start:
            frappe.throw(_("Planned End phải sau Planned Start."))

        if self.max_checkout_time and self.planned_end and self.max_checkout_time < self.planned_end:
            frappe.throw(_("Max Checkout Time không được trước Planned End."))

        self._check_overlap()

    def _normalize_employee_name(self):
        if self.employee and not self.employee_name:
            self.employee_name = frappe.db.get_value("Employee", self.employee, "employee_name")
        if self.shift_type and not self.shift_name:
            self.shift_name = self.shift_type

    def _compute_derived_times(self):
        """Fill half-day boundaries + check-in/out windows from the Shift Type.

        All derived values come from the Shift Type VN custom fields (plan §3.5)
        with sensible defaults; computed once on validate so downstream logic and
        reports read consistent values.
        """
        if not (self.work_date and self.shift_type):
            return
        st = frappe.get_cached_doc("Shift Type", self.shift_type)
        start_time = getattr(st, "start_time", None)
        end_time = getattr(st, "end_time", None)
        if not (start_time and end_time):
            return

        planned_start, planned_end = tz_utils.planned_window(self.work_date, start_time, end_time)
        utc = tz_utils.ZoneInfo("UTC")
        self.planned_start = planned_start.astimezone(utc).strftime("%Y-%m-%d %H:%M:%S")
        self.planned_end = planned_end.astimezone(utc).strftime("%Y-%m-%d %H:%M:%S")
        self.is_overnight = 1 if tz_utils.is_overnight(start_time, end_time) else 0

        # Half-day boundary at the midpoint of the shift.
        mid = planned_start + (planned_end - planned_start) / 2
        self.first_half_start = _fmt(planned_start)
        self.first_half_end = _fmt(mid)
        self.second_half_start = _fmt(mid)
        self.second_half_end = _fmt(planned_end)

        # Check-in / out windows from VN Shift Type custom-minute fields.
        earliest_in = int(getattr(st, "vn_earliest_checkin_minutes", 60) or 60)
        latest_in = int(getattr(st, "vn_latest_checkin_minutes", 30) or 30)
        earliest_out = int(getattr(st, "vn_earliest_checkout_minutes", 30) or 30)
        latest_out = int(getattr(st, "vn_latest_checkout_minutes", 60) or 60)
        max_after = int(getattr(st, "vn_max_checkout_after_end_minutes", 360) or 360)

        self.checkin_window_start = _fmt(planned_start - timedelta(minutes=earliest_in))
        self.checkin_window_end = _fmt(planned_start + timedelta(minutes=latest_in))
        self.checkout_window_start = _fmt(planned_end - timedelta(minutes=earliest_out))
        self.checkout_window_end = _fmt(planned_end + timedelta(minutes=latest_out))
        self.max_checkout_time = _fmt(planned_end + timedelta(minutes=max_after))

    def _check_overlap(self):
        """Reject overlapping instances for the same employee (plan §6.2 validation)."""
        if not (self.employee and self.planned_start and self.planned_end):
            return
        overlap = frappe.db.exists(
            "VN Employee Shift Instance",
            {
                "employee": self.employee,
                "name": ["!=", self.name or "___"],
                "docstatus": ["!=", 2],
                "planned_start": ["<", self.planned_end],
                "planned_end": [">", self.planned_start],
            },
        )
        if overlap:
            frappe.throw(
                _("Ca này trùng giờ với ca đã tồn tại: {0}.").format(overlap),
                frappe.ValidationError,
            )


def _fmt(dt):
    return dt.astimezone(tz_utils.ZoneInfo("UTC")).strftime("%Y-%m-%d %H:%M:%S")
