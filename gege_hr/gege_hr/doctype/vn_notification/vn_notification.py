from __future__ import unicode_literals

import frappe
from frappe.model.document import Document

__all__ = ["VNNotification"]


class VNNotification(Document):
    """A user-facing notification row (doctype-design §25).

    Created by :mod:`gege_hr.gege_hr.api.notification` (and other domain APIs
    that emit realtime alerts). Not submittable — an append-only inbox row.
    Read state flips via the ``notification.mark_read`` /
    ``notification.mark_all_read`` endpoints.
    """

    def before_insert(self):
        # Auto-fill denormalised fields so callers only need (employee, type,
        # title, message).
        if self.employee and not self.employee_name:
            name = frappe.db.get_value("Employee", self.employee, "employee_name") if frappe else None
            if name:
                self.employee_name = name
        if self.employee and not self.user:
            uid = frappe.db.get_value("Employee", self.employee, "user_id") if frappe else None
            if uid:
                self.user = uid
        if not self.user and frappe:
            self.user = frappe.session.user
