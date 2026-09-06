# Copyright (c) 2026 GegeTeam
# License: MIT (see license.txt)

"""VN Attendance by Employee — Script Report (plan reports-desk-free BE-2).

NOTE the report *name* must stay ASCII: the engine resolves the module path via
``scrub(report_name)`` — a Vietnamese name would look for a diacritic folder
("vn_chấm_công_theo_nhân_viên"). The SPA maps this name to the Vietnamese
label (utils/reportView.js REPORT_LABEL_VN).

Standard Frappe Script Report wrapping the SAME pure core the
``/hr/reports`` "Theo nhân viên" tab uses (``api/dashboard.get_attendance_report``
→ ``utils/report.py``), so the SPA tab, the report library tab AND the desk all
read identical numbers. Run through ``frappe.desk.query_report.run`` the engine
adds filters/columns/totals handling for free (``add_total_row: 1`` in the
report JSON — the engine sums the numeric columns itself).

ref_doctype is ``VN Monthly Attendance Period`` → the engine gates on
``frappe.has_permission(..., "report")`` granted in
``api/setup_permissions.py`` PERMISSION_MATRIX (BE-0).
"""

from __future__ import annotations

import frappe
from frappe import _

from gege_hr.gege_hr.utils import report as report_utils

# Searchable text/numeric columns for the optional free-text filter (mirrors
# api/dashboard._REPORT_SEARCH_KEYS so both surfaces match the same rows).
_SEARCH_KEYS = (
    "employee",
    "employee_name",
    "department",
    "branch",
    "company",
    "present_days",
    "absent_days",
    "leave_days",
    "overtime_hours",
    "late_minutes",
    "payable_days",
)

_COLUMNS = [
    {"label": _("Mã NV"), "fieldname": "employee", "fieldtype": "Link", "options": "Employee", "width": 140},
    {"label": _("Nhân viên"), "fieldname": "employee_name", "fieldtype": "Data", "width": 180},
    {"label": _("Phòng ban"), "fieldname": "department", "fieldtype": "Data", "width": 140},
    {"label": _("Chi nhánh"), "fieldname": "branch", "fieldtype": "Data", "width": 120},
    {"label": _("Ngày có mặt"), "fieldname": "present_days", "fieldtype": "Float", "width": 100},
    {"label": _("Ngày vắng"), "fieldname": "absent_days", "fieldtype": "Float", "width": 90},
    {"label": _("Phép có lương"), "fieldname": "paid_leave_days", "fieldtype": "Float", "width": 110},
    {"label": _("Phép không lương"), "fieldname": "unpaid_leave_days", "fieldtype": "Float", "width": 120},
    {"label": _("Ngày lễ"), "fieldname": "holiday_days", "fieldtype": "Float", "width": 90},
    {"label": _("Giờ làm thường"), "fieldname": "regular_hours", "fieldtype": "Float", "width": 110},
    {"label": _("Giờ OT"), "fieldname": "overtime_hours", "fieldtype": "Float", "width": 90},
    {"label": _("OT đêm"), "fieldname": "overtime_night_hours", "fieldtype": "Float", "width": 90},
    {"label": _("Số lần trễ"), "fieldname": "late_count", "fieldtype": "Int", "width": 90},
    {"label": _("Phút trễ"), "fieldname": "late_minutes", "fieldtype": "Int", "width": 90},
    {"label": _("Về sớm (phút)"), "fieldname": "early_leave_minutes", "fieldtype": "Int", "width": 110},
    {"label": _("Công trả lương (ngày)"), "fieldname": "payable_days", "fieldtype": "Float", "width": 130},
    {"label": _("Cần xem xét"), "fieldname": "need_review_count", "fieldtype": "Int", "width": 100},
]


def _search_rows(rows: list[dict], search: str | None) -> list[dict]:
    q = (search or "").strip().lower()
    if not q:
        return rows
    out = []
    for row in rows:
        for key in _SEARCH_KEYS:
            val = row.get(key)
            if val is not None and q in str(val).lower():
                out.append(row)
                break
    return out


def execute(filters: dict | None = None) -> tuple:
    filters = frappe._dict(filters or {})

    company = (filters.get("company") or "").strip()
    month = str(filters.get("period_month") or "").strip()
    year = str(filters.get("period_year") or "").strip()

    if not company:
        frappe.throw(_("Vui lòng chọn công ty."))
    if not (month and year):
        frappe.throw(_("Vui lòng chọn tháng và năm."))

    window = report_utils.month_window(month, year)
    if not window:
        frappe.throw(_("Tháng / năm không hợp lệ."))
    from_date, to_date = window

    ws_rows = report_utils.load_work_sessions(company, from_date.isoformat(), to_date.isoformat())
    leave_rows = report_utils.load_leave_applications(company, from_date.isoformat(), to_date.isoformat())
    rows = report_utils.build_employee_report(ws_rows, leave_rows)
    rows = _search_rows(rows, filters.get("search"))

    return _COLUMNS, rows, None, None
