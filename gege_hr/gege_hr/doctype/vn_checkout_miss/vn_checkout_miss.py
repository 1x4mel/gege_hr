import frappe
from frappe.model.document import Document


class VNCheckoutMiss(Document):
    """Ticket raised when an employee forgets to check out.

    Created by the auto-close engine (utils.checkout_miss). Each ticket tracks
    one shift that had an IN but no OUT, the auto-generated checkout at
    planned_end, the occurrence number (for penalty escalation), and the
    resolution flow (explain → HR review → waive/penalise/close).
    """

    def before_validate(self) -> None:
        if not self.employee_name:
            self.employee_name = frappe.db.get_value("Employee", self.employee, "employee_name")
        if not self.company:
            self.company = frappe.db.get_value("Employee", self.employee, "company")
