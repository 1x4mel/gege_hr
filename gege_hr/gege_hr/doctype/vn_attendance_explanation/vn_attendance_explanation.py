# For license information, see LICENSE.txt / NOTICE.txt in the gege_hr app.

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class VNAttendanceExplanation(Document):
    """Phiếu giải trình chấm công ngoài cửa sổ (mandatory explanation ticket).

    Sinh tự động bởi ``api.attendance.mobile_checkin`` khi lượt chấm vượt mốc
    cấu hình của ca (đi muộn / về sớm / đến sớm / check-out trễ) và nhân viên
    đã nhập lý do. HR duyệt/từ chối qua ``api.explanation.decide_explanation``.
    """

    def validate(self):
        if not (self.reason or "").strip():
            frappe.throw(_("Vui lòng nhập lý do giải trình."))
        if self.status != "Open" and not self.decided_by:
            frappe.throw(_("Thiếu người duyệt cho phiếu đã xử lý."))
