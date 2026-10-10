from __future__ import unicode_literals

import frappe
from frappe.model.document import Document

from gege_hr.gege_hr.utils.naming import set_yymmdd_name


class VNAuditEvent(Document):
    """Append-only audit trail for sensitive portal actions (plan v5 §13.5 /
    doctype-design §26).

    One row is minted whenever a state-changing operation happens on the
    sensitive HR flows (leave/OT/correction/advance submit/approve, work-session
    recalculation, monthly lock/unlock, payroll calculate/approve/publish and any
    manual override). Rows are insert-only: the standard roles only have read
    permission, so the trail cannot be rewritten from the desk UI. ``before_insert``
    stamps the ``AE-YYMMDD-XXXXXX`` name (prefix registered in ``naming.PREFIXES``).
    """

    def autoname(self):
        # Frappe calls this from set_new_name (naming.py step 4) — the
        # before_insert variant never ran because ``doc.name = None`` +
        # the JSON ``format:`` option always overwrote it first.
        set_yymmdd_name(self, "autoname")
        self._stamp_actor()

    def _stamp_actor(self):
        if not self.actor and frappe:
            try:
                self.actor = frappe.session.user
            except Exception:  # pragma: no cover — defensive
                pass
