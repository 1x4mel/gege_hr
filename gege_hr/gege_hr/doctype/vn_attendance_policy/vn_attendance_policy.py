from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.model.document import Document


class VNAttendancePolicy(Document):
    """VN Attendance Policy — plan v5 §B.1 / doctype-design §B.2.

    Server-side validations (doctype-design §2):
      * effective_from <= effective_to (when effective_to set)
      * is_locked blocks edits (HR Manager may unlock)
      * penalty_rules sorted ascending by from_minutes, no overlap
      * min_working_hours_half_day < min_working_hours_full_day
      * editing an active policy bumps ``version`` automatically
    """

    def validate(self):
        self._validate_effective_window()
        self._validate_locked()
        self._validate_penalty_rules()
        self._validate_working_hours()
        self._bump_version_if_active()

    # -- validations --------------------------------------------------------
    def _validate_effective_window(self):
        if self.effective_from and self.effective_to and self.effective_from > self.effective_to:
            frappe.throw(_("Effective From phải trước hoặc bằng Effective To."))

    def _validate_locked(self):
        if self.is_locked and not self.flags.ignore_locked_policy:
            if self.is_new():
                return
            # Only HR Manager / System Manager may unlock; otherwise block
            # writes (F12: System Manager was wrongly rejected here).
            if not ({"HR Manager", "System Manager"} & set(frappe.get_roles())):
                frappe.throw(_("Policy đã bị khóa. Chỉ HR Manager mới được chỉnh sửa."))

    def _validate_penalty_rules(self):
        rules = sorted(
            (r for r in (self.penalty_rules or []) if r.from_minutes is not None),
            key=lambda r: r.from_minutes,
        )
        prev_to = None
        for r in rules:
            if prev_to is not None and r.from_minutes < prev_to:
                frappe.throw(
                    _("Penalty Rules phải sắp xếp tăng dần và không chồng lấn (xung đột tại %s phút).")
                    % r.from_minutes
                )
            if r.to_minutes is not None and r.to_minutes <= r.from_minutes:
                frappe.throw(_("To (minutes) phải lớn hơn From (minutes) ở khoảng %s phút.") % r.from_minutes)
            prev_to = r.to_minutes if r.to_minutes is not None else r.from_minutes + 1

    def _validate_working_hours(self):
        if (
            self.min_working_hours_half_day is not None
            and self.min_working_hours_full_day is not None
            and self.min_working_hours_half_day >= self.min_working_hours_full_day
        ):
            frappe.throw(_("Min Working Hours (Half Day) phải nhỏ hơn Min Working Hours (Full Day)."))

    def _bump_version_if_active(self):
        if self.is_active and not self.is_new():
            self.version = (self.version or 1) + 1
