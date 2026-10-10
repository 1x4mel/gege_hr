# Copyright (c) 2026 GegeTeam
# License: MIT (see license.txt)

"""VN Checkout Miss by Employee — Script Report (desk-free COMPLETE B4).

Per-employee checkout-miss aggregates over a date window: ticket volume per
status, penalty totals (Penalised non-waived only — parity
``utils.payroll.load_checkout_miss_penalty``), repeat-offender depth
(``max_occurrence``), appeals and the latest miss date. Registered in the SPA
report-library allowlist (``api/reports.py REPORT_ALLOWLIST``) so
``/hr/reports`` renders it without any desk step; the Auto Email Report seeded
by ``setup_checkout_miss_deskfree`` reuses the same definition weekly.

ASCII name rule: the engine resolves the module path via ``scrub(report_name)``.
ref_doctype is ``VN Checkout Miss`` → the engine gates on
``frappe.has_permission(..., "report")`` granted in
``api/setup_permissions.py`` PERMISSION_MATRIX.
"""

from __future__ import annotations

from datetime import date, timedelta

import frappe
from frappe import _

MISS_DOCTYPE = "VN Checkout Miss"

_COLUMNS = [
    {"label": _("Mã NV"), "fieldname": "employee", "fieldtype": "Link", "options": "Employee", "width": 140},
    {"label": _("Nhân viên"), "fieldname": "employee_name", "fieldtype": "Data", "width": 180},
    {"label": _("Công ty"), "fieldname": "company", "fieldtype": "Link", "options": "Company", "width": 140},
    {"label": _("Tổng ticket"), "fieldname": "tickets", "fieldtype": "Int", "width": 90},
    {"label": _("Chờ giải trình"), "fieldname": "pending", "fieldtype": "Int", "width": 110},
    {"label": _("Chờ HR duyệt"), "fieldname": "explained", "fieldtype": "Int", "width": 110},
    {"label": _("Đã miễn phạt"), "fieldname": "waived", "fieldtype": "Int", "width": 100},
    {"label": _("Đã phạt"), "fieldname": "penalised", "fieldtype": "Int", "width": 90},
    {"label": _("Đã đóng"), "fieldname": "closed", "fieldtype": "Int", "width": 90},
    {"label": _("Tổng phạt"), "fieldname": "penalty_total", "fieldtype": "Currency", "width": 120},
    {"label": _("Lần tái phạm cao nhất"), "fieldname": "max_occurrence", "fieldtype": "Int", "width": 150},
    {"label": _("Khiếu nại"), "fieldname": "appeals", "fieldtype": "Int", "width": 90},
    {"label": _("Quên gần nhất"), "fieldname": "last_miss_date", "fieldtype": "Date", "width": 110},
]


def execute(filters=None):
    filters = filters or {}
    company = (filters.get("company") or "").strip()
    search = (filters.get("search") or "").strip().lower()
    to_date = str(filters.get("to_date") or date.today())
    try:
        from_date = str(filters.get("from_date") or (date.today() - timedelta(days=30)))
    except Exception:
        from_date = str(date.today() - timedelta(days=30))

    conditions = [["docstatus", "<", 2], ["work_date", ">=", from_date], ["work_date", "<=", to_date]]
    if company:
        conditions.append(["company", "=", company])
    rows = frappe.get_all(
        MISS_DOCTYPE,
        filters=conditions,
        fields=[
            "employee",
            "employee_name",
            "company",
            "status",
            "penalty_amount",
            "penalty_waived",
            "occurrence_no",
            "appeal_count",
            "work_date",
        ],
        order_by="work_date desc",
    )

    by_emp: dict = {}
    for r in rows or []:
        key = r.get("employee") or "?"
        agg = by_emp.setdefault(
            key,
            {
                "employee": r.get("employee"),
                "employee_name": r.get("employee_name") or key,
                "company": r.get("company"),
                "tickets": 0,
                "pending": 0,
                "explained": 0,
                "waived": 0,
                "penalised": 0,
                "closed": 0,
                "penalty_total": 0.0,
                "max_occurrence": 0,
                "appeals": 0,
                "last_miss_date": None,
            },
        )
        agg["tickets"] += 1
        status = (r.get("status") or "").strip().lower()
        if status in ("pending", "explained", "waived", "penalised", "closed"):
            agg[status] += 1
        if status == "penalised" and not r.get("penalty_waived"):
            agg["penalty_total"] += float(r.get("penalty_amount") or 0)
        try:
            agg["max_occurrence"] = max(agg["max_occurrence"], int(r.get("occurrence_no") or 0))
        except (TypeError, ValueError):
            pass
        try:
            agg["appeals"] += int(r.get("appeal_count") or 0)
        except (TypeError, ValueError):
            pass
        wd = str(r.get("work_date") or "")[:10]
        if wd and (not agg["last_miss_date"] or wd > str(agg["last_miss_date"])):
            agg["last_miss_date"] = wd

    out = list(by_emp.values())
    if search:
        out = [
            a
            for a in out
            if search in str(a.get("employee") or "").lower()
            or search in str(a.get("employee_name") or "").lower()
            or search in str(a.get("company") or "").lower()
        ]
    out.sort(key=lambda a: (-a["tickets"], str(a.get("employee_name") or "")))
    return _COLUMNS, out
