from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.model.document import Document


class VNLeaveHandoverTask(Document):
    """Leave handover task — **Post-MVP** (plan v5 §11.5 / doctype-design §32).

    Created when a Leave Application that requires handover (see the
    ``require_handover`` flag on VN Leave Policy Extension) is approved. The
    employee going on leave hands over work to ``to_employee`` with a description
    and optional attachment; the receiver marks the task Completed. Lifecycle is
    status-driven (Pending → In Progress → Completed / Cancelled) and the DocType
    is submittable so completed handovers are auditable.
    """

    def validate(self):
        self._validate_distinct_employees()

    def _validate_distinct_employees(self):
        if self.from_employee and self.to_employee and self.from_employee == self.to_employee:
            frappe.throw(_("From Employee and To Employee must be different."))
