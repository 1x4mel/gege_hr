import frappe
from frappe.model.document import Document


class VNPayrollAdjustment(Document):
    """Child table row — one manual adjustment (bonus / penalty / deduction)
    on a VN Payroll Review Line. ``signed_amount`` is derived from
    ``adjustment_type`` (+Bonus, − everything else) so the line's net can be
    recomputed as ``formula_net + Σ(signed_amount)``."""

    def validate(self) -> None:
        if (self.amount or 0) < 0:
            frappe.throw(frappe._("Số tiền điều chỉnh không được âm."))
        amt = float(self.amount or 0)
        self.signed_amount = amt if self.adjustment_type == "Bonus" else -amt
