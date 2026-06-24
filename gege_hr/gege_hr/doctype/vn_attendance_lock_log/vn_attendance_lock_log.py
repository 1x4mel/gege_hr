from __future__ import unicode_literals

import frappe
from frappe.model.document import Document

__all__ = ["VNAttendanceLockLog"]


class VNAttendanceLockLog(Document):
    """Append-only audit trail for Lock/Unlock actions on a VN Monthly
    Attendance Period (doctype-design §18). Created by
    :mod:`gege_hr.gege_hr.api.attendance_period` on every status change in the
    Lock ↔ Unlock cycle.
    """

    def before_insert(self):
        if not self.actor:
            self.actor = frappe.session.user if frappe else None
