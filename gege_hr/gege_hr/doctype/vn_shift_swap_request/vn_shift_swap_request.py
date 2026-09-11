# For license information, see LICENSE.txt / NOTICE.txt in the gege_hr app.

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, now_datetime


class VNShiftSwapRequest(Document):
    """Employee-proposed shift swap ("đổi ca với đồng nghiệp").

    Created desk-free from the company board (``api.company_board.
    create_swap_request``); HR approves via ``decide_swap_request`` which
    atomically swaps the two shift days (``admin.swap_shift_days``).
    """

    def validate(self):
        if not self.employee or not self.target_employee:
            frappe.throw(_("Thiếu nhân viên / đồng nghiệp đổi ca."))
        if self.employee == self.target_employee:
            frappe.throw(_("Không thể đổi ca với chính mình."))
        if not self.from_date or not self.target_date:
            frappe.throw(_("Thiếu ngày hoán đổi."))
        if getdate(self.from_date) < getdate():
            frappe.throw(_("Không thể đổi ca của ngày đã qua."))
        if getdate(self.target_date) < getdate():
            frappe.throw(_("Không thể đổi ca của ngày đã qua."))
        if not (self.reason or "").strip():
            frappe.throw(_("Vui lòng nhập lý do đổi ca."))
        if self.status == "Open":
            dup = frappe.db.exists(
                "VN Shift Swap Request",
                {
                    "status": "Open",
                    "employee": self.employee,
                    "from_date": self.from_date,
                    "target_employee": self.target_employee,
                    "target_date": self.target_date,
                },
            )
            if dup and dup != self.name:
                frappe.throw(_("Đã có yêu cầu đổi ca tương tự đang chờ duyệt."))
