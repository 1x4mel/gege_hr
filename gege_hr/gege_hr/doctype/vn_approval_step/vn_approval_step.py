from __future__ import unicode_literals

from frappe.model.document import Document


class VNApprovalStep(Document):
    """Child row of :doc:`VN Approval Matrix` — one approval stage.

    ``approver_type`` decides who may act at this stage:

    * ``Line Manager``     — the requester's ``Employee.reports_to`` user.
    * ``Department Head``  — the requester's department head.
    * ``HR User`` / ``HR Manager`` — any user holding that Frappe role.
    * ``Specific User``/``Specific Role`` — ``approver_user`` / ``approver_role``.

    ``step_no`` gives the order; the inbox advances one step at a time until
    the matrix is exhausted (→ Approved) or an approver rejects (→ Rejected).
    """

    pass
