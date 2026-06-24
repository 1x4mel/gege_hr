from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.model.document import Document


class VNLeaveBlackoutPeriod(Document):
    """Leave blackout window (plan v5 §11.8 / doctype-design §31).

    Defines a date range during which leave is restricted (e.g. month-end
    close, Tet peak). Scoped by company / branch / department and optionally a
    single Leave Type (blank = all). When a Leave Application overlaps an active
    blackout, the ``action`` decides whether to merely warn, hard-block, or force
    the request through HR approval. ``reason`` is mandatory so the restriction
    is auditable.
    """

    def validate(self):
        self._validate_date_range()

    def _validate_date_range(self):
        if self.from_date and self.to_date and self.from_date > self.to_date:
            frappe.throw(_("From Date cannot be after To Date."))
