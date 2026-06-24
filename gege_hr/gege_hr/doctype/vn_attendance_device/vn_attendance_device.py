from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.model.document import Document


class VNAttendanceDevice(Document):
    """Physical / virtual attendance device (biometric, face, card, mobile).

    ``device_code`` is the stable identifier used by the device-sync flow and
    ``VN Device Employee Mapping``. ``last_sync_at`` is updated by the sync API
    (plan §10.10).
    """

    def validate(self):
        if self.port and not (1 <= int(self.port) <= 65535):
            frappe.throw(_("Port phải nằm trong khoảng 1–65535."))
