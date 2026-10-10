from frappe.model.document import Document


class VNEmployeeOnboarding(Document):
    """FIX-2 (I-2): a VN onboarding process. Progress is derived from its tasks
    in ``api/onboarding.recompute_progress`` (keep this controller thin)."""

    pass
