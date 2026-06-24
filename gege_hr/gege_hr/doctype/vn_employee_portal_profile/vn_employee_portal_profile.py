from __future__ import unicode_literals

import frappe
from frappe.model.document import Document


class VNEmployeePortalProfile(Document):
    """Per-employee portal preferences (plan v5 §10.9 / doctype-design §24).

    1:1 with ``Employee`` (autoname ``field:employee``). Stores the portal-facing
    preferences the frontend reads on boot: language, theme, notification toggle
    and the FCM push token used for real-time notifications. The profile is
    auto-created on first portal login; an employee may only edit their own row.
    """

    def validate(self):
        self._normalize_employee_name()

    def _normalize_employee_name(self):
        if self.employee_name:
            return
        if not self.employee:
            return
        try:
            self.employee_name = frappe.db.get_value("Employee", self.employee, "employee_name") or ""
        except Exception:  # pragma: no cover — defensive, bench-only
            self.employee_name = ""
