"""Standard Frappe report engine proxy — plan reports-desk-free (§3 BE-1).

Bridges the SPA ``/hr/reports`` "Thư viện báo cáo" tab to the standard Report
engine (``frappe.desk.query_report``) so HR can run the HRMS/VN Script Reports
without ever opening the desk. The SPA never calls the engine directly —
every call goes through this module so there is ONE place that:

* enforces the report allowlist (name → expected ``ref_doctype``),
* checks the caller holds an HR role (``_require_hr``, parity ``api/audit``),
* checks the report's own ``Report.roles`` child table,
* translates the engine's permission errors into Vietnamese messages.

Engine facts this module relies on (frappe/desk/query_report.py):

* ``run()`` is ``@frappe.whitelist()`` + ``@frappe.read_only()`` and gates on
  ``frappe.has_permission(report.ref_doctype, "report")`` — those ptype grants
  live in ``api/setup_permissions.py`` PERMISSION_MATRIX (BE-0).
* ``validate_filters_permissions`` requires read/select on Link filter values
  (Company is granted read to HR roles in the same matrix).
* ``generate_report_result`` normalises rows to list-of-dicts (columns come
  back as dicts for Script Reports), so the envelope passes through untouched.

Bench-free unit tests (``tests/test_reports_api.py``) stub ``frappe`` via
``sys.modules`` before importing this module — same harness as
``tests/test_audit_api.py``. Keep the frappe surface used here minimal.
"""

from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _

# --------------------------------------------------------------------------- #
# Allowlist — the ONLY reports the SPA may run (plan §3 BE-1).
# name → required ref_doctype (a mismatch → refuse: guards against a same-named
# report installed later with a different ref_doctype). Names verified against
# the live site (B0 survey 2026-08-29, module HR reports on erp-hr.local).
#
# NOTE: "Employee Information" (Report Builder) is deliberately absent —
# ``get_report_result`` only handles Query / Script / Custom Report types.
# --------------------------------------------------------------------------- #
REPORT_ALLOWLIST: dict[str, str] = {
    "Monthly Attendance Sheet": "Attendance",
    "Shift Attendance": "Attendance",
    "Employees working on a holiday": "Attendance",
    "Employee Analytics": "Employee",
    "Employee Birthday": "Employee",
    "Employee Leave Balance": "Employee",
    "Employee Leave Balance Summary": "Employee",
    "Leave Ledger": "Leave Ledger Entry",
    "Employee Advance Summary": "Employee Advance",
    "Unpaid Expense Claim": "Expense Claim",
    "Appraisal Overview": "Appraisal",
    "Recruitment Analytics": "Staffing Plan",
    "Employee Exits": "Exit Interview",
    # gege_hr Script Report (BE-2) — ref_doctype gates on the VN period.
    # ASCII name (engine scrubs it into the module path); the SPA maps the
    # Vietnamese label client-side (utils/reportView.js REPORT_LABEL_VN).
    "VN Attendance by Employee": "VN Monthly Attendance Period",
    # Desk-free COMPLETE B4 — per-employee checkout-miss aggregates; seeded as
    # a Query Report by setup_checkout_miss_deskfree.seed().
    "VN Checkout Miss by Employee": "VN Checkout Miss",
}

_REPORT_FIELDS = (
    "name",
    "report_name",
    "report_type",
    "ref_doctype",
    "module",
    "prepared_report",
    "add_total_row",
)


def _is_hr() -> bool:
    roles = set(frappe.get_roles(frappe.session.user))
    return bool(roles & {"HR Manager", "HR User", "System Manager"})


def _require_hr() -> None:
    if not _is_hr():
        frappe.throw(_("Báo cáo chỉ dành cho HR."), frappe.PermissionError)


def _report_roles(doc) -> set[str]:
    """Roles declared on the Report doc (child table). Empty → all roles
    (parity desk: a Report without roles is gated only by the ref_doctype
    report permission)."""
    return {r.role for r in (doc.roles or [])}


def _get_report(report_name: str):
    """Load an allowlisted, enabled Report whose ref_doctype matches."""
    expected = REPORT_ALLOWLIST.get(report_name)
    if not expected:
        frappe.throw(_("Báo cáo không khả dụng."))
    if not frappe.db.exists("Report", report_name):
        frappe.throw(_("Báo cáo không tồn tại trên hệ thống."))
    doc = frappe.get_doc("Report", report_name)
    if getattr(doc, "disabled", 0):
        frappe.throw(_("Báo cáo đã bị vô hiệu hóa."))
    if doc.ref_doctype != expected:
        frappe.throw(_("Báo cáo không khả dụng."))
    return doc


def _assert_role(doc) -> None:
    allowed = _report_roles(doc)
    if allowed and not (allowed & set(frappe.get_roles(frappe.session.user))):
        frappe.throw(_("Bạn không có quyền chạy báo cáo này."), frappe.PermissionError)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def list_reports() -> list[dict]:
    """Reports the current user may run (allowlist ∩ Report.roles ∩ enabled).

    Returns ``[{ name, report_name, report_type, ref_doctype, module,
    prepared_report, add_total_row }]`` sorted by name. Reports missing on
    this site are skipped silently (an app update may rename them).
    """
    _require_hr()
    user_roles = set(frappe.get_roles(frappe.session.user))
    out: list[dict] = []
    for name in REPORT_ALLOWLIST:
        try:
            if not frappe.db.exists("Report", name):
                continue
            doc = frappe.get_doc("Report", name)
        except Exception:  # noqa: BLE001 - unreadable/renamed report → skip
            continue
        if getattr(doc, "disabled", 0) or doc.ref_doctype != REPORT_ALLOWLIST[name]:
            continue
        allowed = _report_roles(doc)
        if allowed and not (allowed & user_roles):
            continue
        out.append({f: doc.get(f) for f in _REPORT_FIELDS})
    out.sort(key=lambda r: r["name"])
    return out


@frappe.whitelist()
def report_meta(report_name: str) -> dict:
    """Filter declarations for the SPA dynamic filter form.

    Mirrors the Report ``filters`` child table — the exact rows
    ``validate_filters_permissions`` walks, so the FE and the engine always
    agree on fieldnames/fieldtypes.
    """
    _require_hr()
    doc = _get_report(report_name)
    _assert_role(doc)
    meta: dict[str, Any] = {f: doc.get(f) for f in _REPORT_FIELDS}
    meta["filters"] = [
        {
            "fieldname": f.get("fieldname"),
            "label": f.get("label"),
            "fieldtype": f.get("fieldtype"),
            "options": f.get("options"),
            "default": f.get("default"),
            "reqd": f.get("reqd"),
            "depends_on": f.get("depends_on"),
        }
        for f in (doc.get("filters") or [])
    ]
    return meta


@frappe.whitelist()
def run_report(report_name: str, filters: str | dict | None = None) -> dict:
    """Run an allowlisted report via the standard engine. Envelope passes
    through untouched: ``{ result: [dict...], columns: [dict...], message,
    chart, report_summary, add_total_row, ... }``.
    """
    _require_hr()
    doc = _get_report(report_name)
    _assert_role(doc)

    from frappe.desk.query_report import run as run_query_report

    if isinstance(filters, dict):
        filters = json.dumps(filters)

    try:
        return run_query_report(report_name=report_name, filters=filters)
    except Exception as e:  # noqa: BLE001 - translate perms, re-raise the rest
        msg = str(e) or ""
        is_perm = isinstance(e, getattr(frappe, "PermissionError", tuple())) or (
            "report permission" in msg or "You do not have permission" in msg
        )
        if is_perm:
            frappe.throw(_("Bạn không có quyền chạy báo cáo này."), frappe.PermissionError)
        raise


@frappe.whitelist()
def export_report(report_name: str, file_type: str = "xlsx") -> None:
    """P1 stub — proxies ``frappe.desk.query_report.export_query`` later.

    Kept as a declared-but-throwing endpoint so the FE wrapper can call it
    the day it is implemented without another API contract change.
    """
    _require_hr()
    frappe.throw(_("Xuất báo cáo chưa được hỗ trợ."))
