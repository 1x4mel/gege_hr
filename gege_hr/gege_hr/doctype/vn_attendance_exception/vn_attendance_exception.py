from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.model.document import Document

from gege_hr.gege_hr.utils import tz as tz_utils
from gege_hr.gege_hr.utils.naming import set_yymmdd_name


class VNAttendanceException(Document):
    """A flag raised by the engine / HR needing manual review (plan §4.7).

    Lifecycle: ``Open → In Progress → Resolved/Ignored/Escalated``. The Work
    Session is not final while an Open exception references it. The engine
    auto-creates these (missing log, excess OT, excess work hours); HR can also
    raise a ``Manual Flag``.
    """

    def before_insert(self):
        set_yymmdd_name(self, "before_insert")

    def validate(self):
        self._normalize_employee_name()
        self._enforce_resolution_consistency()

    def before_save(self):
        # Stamp resolved_at / resolved_by when transitioning into a closed state.
        closed = {"Resolved", "Ignored"}
        if self.status in closed and not self.resolved_at:
            self.resolved_at = tz_utils.now_in_portal().replace(tzinfo=None)
        if self.status in closed and not self.resolved_by:
            self.resolved_by = frappe.session.user

    def _normalize_employee_name(self):
        if self.employee and not self.employee_name:
            self.employee_name = frappe.db.get_value("Employee", self.employee, "employee_name")

    def _enforce_resolution_consistency(self):
        """A closed exception must record how it was resolved."""
        if self.status in {"Resolved", "Ignored"} and not self.resolution_type:
            frappe.throw(_("Vui lòng chọn Resolution Type khi đóng exception."))
