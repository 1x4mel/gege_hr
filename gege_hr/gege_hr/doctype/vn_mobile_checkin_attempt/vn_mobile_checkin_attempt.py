from __future__ import unicode_literals

from frappe.model.document import Document

from gege_hr.gege_hr.utils.naming import set_yymmdd_name


class VNMobileCheckinAttempt(Document):
    """Audit record for every mobile check-in/out tap (plan §8.6 / §Security).

    The ``client_request_id`` (unique) is the idempotency key: a retried tap
    with the same id is a no-op. Created mostly by the API, so ``in_create``
    is enabled and writes bypass the UI workflow.
    """

    def autoname(self):
        # Frappe naming hook (set_new_name step 4) — before_insert never won
        # against the JSON ``format:`` autoname (doc.name is reset first).
        set_yymmdd_name(self, "autoname")
