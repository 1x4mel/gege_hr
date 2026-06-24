from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.model.document import Document

from gege_hr.gege_hr.utils.naming import set_yymmdd_name


class VNAttendanceRawLog(Document):
    """A single raw punch before deduplication / resolution.

    Ingested from devices (``source_type=Device`` via the sync API), mobile
    (``Mobile``), manual entry, or bulk import. The sync flow resolves the
    ``raw_employee_code`` to a Frappe ``employee`` through
    ``VN Device Employee Mapping``, marks duplicates, then creates the matching
    ``Employee Checkin`` (plan §7.1 layers 1→3).
    """

    def before_insert(self):
        set_yymmdd_name(self, "before_insert")

    def validate(self):
        if not self.employee and not self.raw_employee_code:
            frappe.throw(_("Phải có Employee hoặc Raw Employee Code."))
        if self.employee and not self.employee_name:
            self.employee_name = frappe.db.get_value("Employee", self.employee, "employee_name")
        # Source-type specific requirement: a device log must carry its device.
        if self.source_type == "Device" and not self.device:
            frappe.throw(_("Log từ thiết bị phải chọn Device."))
