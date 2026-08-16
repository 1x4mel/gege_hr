from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.model.document import Document

from gege_hr.gege_hr.utils.naming import set_yymmdd_name


class VNAttendanceCalculationRun(Document):
    """A batch / recalculation / backfill run over many Work Sessions.

    Each Work Session points back to its originating run via ``calculation_run``
    (plan §6.2). The run records progress counters + an error log so a partial
    batch can be resumed rather than restarted.
    """

    def autoname(self):
        # Frappe calls this from set_new_name (naming.py step 4) — the
        # before_insert variant never ran because ``doc.name = None`` +
        # the JSON ``format:`` option always overwrote it first.
        set_yymmdd_name(self, "autoname")

    def validate(self):
        if self.from_date and self.to_date and self.from_date > self.to_date:
            frappe.throw(_("From Date không được sau To Date."))
