from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.model.document import Document


class VNDeviceEmployeeMapping(Document):
    """Maps a raw device code (badge id / fingerprint id) to a Frappe Employee.

    Unique per ``(device, raw_employee_code)`` so a punch from the device can be
    resolved deterministically to one employee. Used by the device-sync flow
    (plan §10.10) when ingesting ``VN Attendance Raw Log`` rows.
    """

    def validate(self):
        if self.valid_from and self.valid_to and self.valid_from > self.valid_to:
            frappe.throw(_("Valid From không được sau Valid To."))
        duplicate = frappe.db.exists(
            "VN Device Employee Mapping",
            {"device": self.device, "raw_employee_code": self.raw_employee_code, "name": ["!=", self.name]},
        )
        if duplicate:
            frappe.throw(
                _("Đã tồn tại mapping cho thiết bị {0} với mã {1}.").format(
                    self.device, self.raw_employee_code
                ),
                frappe.DuplicateEntryError,
            )
