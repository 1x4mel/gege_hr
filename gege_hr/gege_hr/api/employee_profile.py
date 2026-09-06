"""Employee 360° profile API — /hr/employees parity (plans/plan-employee-frontend-parity.md §2.1).

Endpoints powering the SPA ``Employee360Drawer`` so an HR admin never has to
open the Frappe Desk to operate an Employee record:

* :func:`get_employee_form_meta` — scrubbed DocType meta (sections + editable
  whitelist + link filter options) driving the meta-based edit form. Never
  returns the raw meta (sensitive/hidden/unsupported fields are stripped).
* :func:`get_employee_detail` — one-call 360° payload: the scrubbed doc +
  related lists (attendance / leave / payslips / expenses / shift assignments /
  salary-structure assignments / VN bank accounts / onboarding) + merged
  activity timeline (standard ``Comment`` + ``Version`` + ``VN Audit Event``)
  + ``File`` attachments.
* :func:`add_employee_comment` — HR note stored as a standard ``Comment`` row
  referenced to the Employee (Desk-equivalent of the sidebar comment box).
* :func:`delete_employee_attachment` — guarded ``File`` delete + audit.

Self-contained on purpose (no import from :mod:`gege_hr.gege_hr.api.admin` at
module top): the shared whitelist / audit helpers are imported lazily inside
the endpoints so the bench-free stub-frappe tests in
``tests/test_employee_profile.py`` stay simple. Pure helpers
(:func:`scrub_meta_fields`, :func:`group_sections`, :func:`parse_depends_on`,
:func:`version_summary`, :func:`merge_activity`, :func:`summarize_related`)
carry the logic and are unit-testable without a bench.
"""

from __future__ import annotations

import json
import re

import frappe
from frappe import _

HR_ROLES = ["HR Manager", "HR User", "System Manager"]
EMPLOYEE_DOCTYPE = "Employee"

# Fieldnames that must NEVER leave the server, whatever the meta says.
SENSITIVE_FIELD_RE = re.compile(r"password|api_key|api_secret|secret", re.IGNORECASE)

# Layout markers the SPA renderer understands (start a group / a column).
LAYOUT_FIELDTYPES = {"Section Break", "Column Break"}

# Fieldtypes safe to render + edit from the SPA form. Everything else
# (Attach, Table, HTML, Button, Tab Break, …) is dropped by the scrub.
DISPLAYABLE_FIELDTYPES = LAYOUT_FIELDTYPES | {
    "Data",
    "Date",
    "Datetime",
    "Int",
    "Float",
    "Check",
    "Select",
    "Link",
    "Small Text",
    "Text",
    "Long Text",
    "Currency",
    "Percent",
    "Phone",
}

# Standard table columns present on every DocType — safe to project always.
_META_COLUMNS = frozenset(
    {
        "name",
        "creation",
        "modified",
        "modified_by",
        "owner",
        "docstatus",
        "idx",
        "parent",
        "parentfield",
        "parenttype",
    }
)


def _require_hr() -> None:
    """Gate: same read-level roles as ``admin.list_employees``."""
    frappe.only_for(HR_ROLES)


# --------------------------------------------------------------------------- #
# Pure helpers — no frappe I/O (bench-free unit tests, BE-EP-01/09/10)
# --------------------------------------------------------------------------- #
def _field_dict(df) -> dict:
    """Normalise a DocField (object or dict) into a plain dict."""
    if isinstance(df, dict):
        return df
    keys = (
        "fieldname",
        "label",
        "fieldtype",
        "options",
        "reqd",
        "read_only",
        "hidden",
        "depends_on",
        "default",
        "description",
    )
    return {k: getattr(df, k, None) for k in keys}


def scrub_meta_fields(meta_fields, editable_fields) -> list[dict]:
    """DocField rows → SPA-renderable field dicts (BE-EP-01).

    Drops: sensitive fieldnames (password/api_key/…), hidden fields and
    unsupported fieldtypes. Everything outside ``editable_fields`` (except
    layout markers) is returned with ``read_only=1`` so the form can still
    DISPLAY it — mirroring how the Desk shows no-permlevel fields greyed out.
    """
    editable = set(editable_fields or [])
    out: list[dict] = []
    for raw in meta_fields or []:
        df = _field_dict(raw)
        fieldname = str(df.get("fieldname") or "").strip()
        fieldtype = str(df.get("fieldtype") or "").strip()
        if not fieldname or not fieldtype:
            continue
        if SENSITIVE_FIELD_RE.search(fieldname):
            continue
        if fieldtype not in DISPLAYABLE_FIELDTYPES:
            continue
        hidden = 1 if int(df.get("hidden") or 0) == 1 else 0
        is_layout = fieldtype in LAYOUT_FIELDTYPES
        if hidden and not is_layout:
            continue
        read_only = 0
        if not is_layout:
            forced_read_only = int(df.get("read_only") or 0) == 1
            read_only = 1 if (forced_read_only or fieldname not in editable) else 0
        out.append(
            {
                "fieldname": fieldname,
                "label": df.get("label") or fieldname,
                "fieldtype": fieldtype,
                "options": df.get("options"),
                "mandatory": 1 if df.get("reqd") else 0,
                "read_only": read_only,
                "hidden": hidden,
                "depends_on": df.get("depends_on"),
                "default": df.get("default"),
                "description": df.get("description"),
            }
        )
    return out


def group_sections(scrubbed_fields) -> list[dict]:
    """Split a scrubbed field list into ``[{fieldname, label, fields}]`` groups.

    Fields before the first ``Section Break`` (if any) land in an unnamed
    leading section so nothing is silently dropped. ``Column Break`` entries
    stay inside the field list for the 2-column SPA layout.
    """
    sections: list[dict] = []
    current: dict = {"fieldname": None, "label": None, "fields": []}
    for f in scrubbed_fields or []:
        if f.get("fieldtype") == "Section Break":
            if current["fields"] or current["label"]:
                sections.append(current)
            current = {"fieldname": f["fieldname"], "label": f["label"], "fields": []}
        else:
            current["fields"].append(f)
    if current["fields"] or current["label"]:
        sections.append(current)
    return sections


def parse_depends_on(expr) -> dict | None:
    """``eval:doc.status=='Left'`` → ``{"field": "status", "op": "==", "value": "Left"}``.

    Only the simple ``doc.<field> ==/!= 'value'`` pattern is extracted — the
    one the SPA needs (show the offboarding group when status is Left).
    Anything more complex returns ``None`` (field always shown).
    """
    if not expr:
        return None
    m = re.search(
        r"doc\.([A-Za-z_][A-Za-z0-9_]*)\s*(==|!=)\s*['\"]([^'\"]+)['\"]",
        str(expr),
    )
    if not m:
        return None
    return {"field": m.group(1), "op": m.group(2), "value": m.group(3)}


def version_summary(data_json) -> str:
    """Version ``data`` JSON → human summary ``"status: Active → Left"``.

    Malformed/empty payloads degrade to a generic Vietnamese label, never raise.
    """
    try:
        data = json.loads(data_json) if data_json else {}
    except (TypeError, ValueError):
        data = {}
    parts: list[str] = []
    for row in data.get("changed") or []:
        if isinstance(row, (list, tuple)) and row:
            old = row[1] if len(row) > 1 else ""
            new = row[2] if len(row) > 2 else ""
            parts.append(f"{row[0]}: {old} → {new}")
    return "; ".join(parts) or "Cập nhật hồ sơ"


def merge_activity(comments, versions, audits, limit: int = 20) -> list[dict]:
    """Merge the three activity sources into one newest-first timeline.

    Each item: ``{"source": comment|version|audit, "actor", "at", "text",
    "type"}`` — ``type`` is only set for audits (audit_type). Missing
    timestamps sort last.
    """
    items: list[dict] = []
    for c in comments or []:
        items.append(
            {
                "source": "comment",
                "actor": c.get("owner") or c.get("comment_email"),
                "at": str(c.get("creation") or ""),
                "text": c.get("content") or "",
                "type": None,
            }
        )
    for v in versions or []:
        items.append(
            {
                "source": "version",
                "actor": v.get("owner"),
                "at": str(v.get("creation") or ""),
                "text": version_summary(v.get("data")),
                "type": None,
            }
        )
    for a in audits or []:
        items.append(
            {
                "source": "audit",
                "actor": a.get("actor"),
                "at": str(a.get("created_at") or a.get("creation") or ""),
                "text": a.get("description") or "",
                "type": a.get("audit_type"),
            }
        )
    items.sort(key=lambda i: i["at"], reverse=True)
    return items[: max(0, int(limit))]


def summarize_related(rows, recent_limit: int = 6) -> dict:
    """``[rows]`` → ``{"count": len, "recent": rows[:limit]}`` (BE-EP-09)."""
    rows = list(rows or [])
    return {"count": len(rows), "recent": rows[: max(0, int(recent_limit))]}


def scrub_document(doc_dict) -> dict:
    """Drop sensitive keys from a document ``as_dict()`` payload."""
    return {k: v for k, v in (doc_dict or {}).items() if not SENSITIVE_FIELD_RE.search(str(k))}


# --------------------------------------------------------------------------- #
# Frappe I/O helpers
# --------------------------------------------------------------------------- #
def _existing_fields(doctype: str, fields) -> list[str]:
    """Subset of ``fields`` that exist on ``doctype`` (mirrors ``admin._safe_fields``).

    Keeps the queries below from raising ``Unknown column`` on benches whose
    custom fields (vn_employee_code, …) have not been migrated yet.
    """
    if not fields:
        return ["name"]
    try:
        valid = {df.fieldname for df in frappe.get_meta(doctype).fields}
        valid |= _META_COLUMNS
    except Exception:
        return ["name"]
    out: list[str] = []
    for f in fields:
        f = str(f).strip()
        if f and f in valid and f not in out:
            out.append(f)
    if "name" not in out:
        out.append("name")
    return out


def _related_summary(
    doctype: str,
    filters: dict,
    fields: list[str],
    order_by: str,
    recent_limit: int,
) -> dict:
    """Count + newest slice of one related doctype (cap 100 rows, BE-EP-09)."""
    safe = _existing_fields(doctype, fields)
    try:
        rows = (
            frappe.get_all(
                doctype,
                filters=filters,
                fields=safe,
                order_by=order_by,
                limit_page_length=100,
            )
            or []
        )
    except Exception:
        frappe.log_error(title=f"employee_profile._related_summary {doctype} failed")
        rows = []
    return summarize_related(rows, recent_limit)


def _safe_rows(doctype: str, filters: dict, fields: list[str], order_by: str, limit: int) -> list[dict]:
    """Fail-tolerant get_all for the activity/attachment sources (plan §2.1.2).

    A missing column/doctype on a partial install logs and yields ``[]`` so the
    360° payload still renders — one broken timeline source never kills the call.
    """
    try:
        return (
            frappe.get_all(
                doctype,
                filters=filters,
                fields=_existing_fields(doctype, fields),
                order_by=order_by,
                limit_page_length=limit,
            )
            or []
        )
    except Exception:
        frappe.log_error(title=f"employee_profile._safe_rows {doctype} failed")
        return []


def _version_ref_filters(employee: str) -> dict:
    """Version link filters, column-name aware across Frappe versions.

    Newest benches store ``ref_doctype`` + ``docname`` (E2E profile-deskfree
    caught the live 1054 ``Unknown column 'ref_name'`` — F-PF5); v15 used
    ``ref_doctype`` + ``ref_name``; older used ``ref_type`` + ``ref_name``.
    """
    if "ref_doctype" in _existing_fields("Version", ["ref_doctype"]):
        name_field = "docname" if "docname" in _existing_fields("Version", ["docname"]) else "ref_name"
        return {"ref_doctype": EMPLOYEE_DOCTYPE, name_field: employee}
    return {"ref_type": EMPLOYEE_DOCTYPE, "ref_name": employee}


def _latest_onboarding(employee: str) -> dict | None:
    """Newest ``VN Employee Onboarding`` row (or None when absent/partial install)."""
    try:
        rows = (
            frappe.get_all(
                "VN Employee Onboarding",
                filters={"employee": employee},
                fields=_existing_fields(
                    "VN Employee Onboarding", ["name", "status", "progress", "boarding_date"]
                ),
                order_by="creation desc",
                limit_page_length=1,
            )
            or []
        )
        return rows[0] if rows else None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def get_employee_form_meta() -> dict:
    """Scrubbed Employee meta for the SPA 360° form (plan §2.1.1).

    Returns ``{"sections": [...], "editable_fields": [...], "link_options":
    {...}}``. The editable whitelist is the single source of truth in
    :mod:`gege_hr.gege_hr.api.admin` (``_EMPLOYEE_EDITABLE_FIELDS``) — imported
    lazily so this module stays stub-test friendly.
    """
    _require_hr()
    from gege_hr.gege_hr.api.admin import _EMPLOYEE_EDITABLE_FIELDS

    try:
        meta_fields = frappe.get_meta(EMPLOYEE_DOCTYPE).fields or []
    except Exception:
        meta_fields = []
    sections = group_sections(scrub_meta_fields(meta_fields, _EMPLOYEE_EDITABLE_FIELDS))

    link_options: dict[str, list] = {
        field: []
        for field in (
            "department",
            "branch",
            "designation",
            "employment_type",
            "company",
            "line_manager",
            "default_work_location",
        )
    }
    try:
        from gege_hr.gege_hr.api.admin import get_employee_filter_options

        link_options.update(get_employee_filter_options() or {})
    except Exception:
        frappe.log_error(title="employee_profile.get_employee_form_meta options failed")
    # Gender is a Link master not covered by the gear-popover options.
    try:
        genders = (
            frappe.db.get_all("Gender", fields=["name"], order_by="name asc", limit_page_length=20) or []
        )
        link_options["gender"] = [{"value": r["name"], "label": r["name"]} for r in genders]
    except Exception:
        link_options["gender"] = []

    return {
        "sections": sections,
        "editable_fields": list(_EMPLOYEE_EDITABLE_FIELDS),
        "link_options": link_options,
    }


@frappe.whitelist()
def get_employee_detail(employee: str, related_limit: int = 6) -> dict:
    """One-call 360° payload for the SPA drawer (plan §2.1.2, BE-EP-09)."""
    _require_hr()
    employee = (employee or "").strip()
    if not employee or not frappe.db.exists(EMPLOYEE_DOCTYPE, employee):
        frappe.throw(_("Nhân viên không tồn tại."))
    related_limit = max(1, min(int(related_limit or 6), 20))

    doc = frappe.get_doc(EMPLOYEE_DOCTYPE, employee).as_dict()
    related = {
        "attendance": _related_summary(
            "Attendance",
            {"employee": employee},
            ["attendance_date", "status", "in_time", "out_time"],
            "attendance_date desc",
            related_limit,
        ),
        "leave": _related_summary(
            "Leave Application",
            {"employee": employee},
            ["leave_type", "from_date", "to_date", "total_leave_days", "status"],
            "from_date desc",
            related_limit,
        ),
        "payslips": _related_summary(
            "Salary Slip",
            {"employee": employee},
            ["start_date", "end_date", "net_pay", "status"],
            "start_date desc",
            related_limit,
        ),
        "expenses": _related_summary(
            "Expense Claim",
            {"employee": employee},
            ["posting_date", "total_claimed_amount", "approval_status"],
            "posting_date desc",
            related_limit,
        ),
        "shift_assignments": _related_summary(
            "Shift Assignment",
            {"employee": employee},
            ["shift_type", "start_date", "end_date", "docstatus"],
            "creation desc",
            related_limit,
        ),
        "ssa": _related_summary(
            "Salary Structure Assignment",
            {"employee": employee},
            ["salary_structure", "from_date", "base", "docstatus"],
            "from_date desc",
            related_limit,
        ),
        "bank_accounts": _related_summary(
            "VN Employee Bank Account",
            {"employee": employee},
            ["bank_name", "bank_bin", "account_no", "account_name", "is_default"],
            "is_default desc, creation desc",
            10,
        ),
        "onboarding": _latest_onboarding(employee),
    }

    comments = _safe_rows(
        "Comment",
        {"reference_doctype": EMPLOYEE_DOCTYPE, "reference_name": employee},
        ["owner", "comment_email", "creation", "content"],
        "creation desc",
        20,
    )
    versions = _safe_rows(
        "Version",
        _version_ref_filters(employee),
        ["owner", "creation", "data"],
        "creation desc",
        20,
    )
    audits = _safe_rows(
        "VN Audit Event",
        {"employee": employee},
        ["audit_type", "actor", "created_at", "description"],
        "created_at desc",
        20,
    )
    attachments = _safe_rows(
        "File",
        {"attached_to_doctype": EMPLOYEE_DOCTYPE, "attached_to_name": employee},
        ["file_name", "file_url", "is_private", "file_size", "owner", "creation"],
        "creation desc",
        50,
    )

    return {
        "employee": scrub_document(doc),
        "related": related,
        "activity": {
            "comments": comments or [],
            "versions": versions or [],
            "audits": audits or [],
            "merged": merge_activity(comments, versions, audits, 20),
        },
        "attachments": attachments or [],
    }


@frappe.whitelist()
def add_employee_comment(employee: str, comment: str) -> dict:
    """HR note → standard ``Comment`` row on the Employee (plan §2.1.3, BE-EP-11)."""
    _require_hr()
    employee = (employee or "").strip()
    if not employee or not frappe.db.exists(EMPLOYEE_DOCTYPE, employee):
        frappe.throw(_("Nhân viên không tồn tại."))
    text = (comment or "").strip()
    if len(text) < 2:
        frappe.throw(_("Nội dung ghi chú quá ngắn."))
    doc = frappe.get_doc(
        {
            "doctype": "Comment",
            "comment_type": "Comment",
            "reference_doctype": EMPLOYEE_DOCTYPE,
            "reference_name": employee,
            "comment_email": frappe.session.user,
            "content": text,
        }
    )
    doc.insert(ignore_permissions=True)
    return {"name": doc.name, "creation": doc.creation, "owner": doc.owner}


@frappe.whitelist()
def delete_employee_attachment(file_name: str, employee: str | None = None) -> dict:
    """Guarded ``File`` delete from the drawer (plan §2.1.4, BE-EP-12).

    The file must be attached to an ``Employee`` doc — and to ``employee``
    when the argument is passed — otherwise a Vietnamese error is raised.
    Best-effort audit via ``admin._audit_admin``.
    """
    _require_hr()
    file_name = (file_name or "").strip()
    if not file_name or not frappe.db.exists("File", file_name):
        frappe.throw(_("Tệp đính kèm không tồn tại."))
    file = frappe.get_doc("File", file_name)
    if (getattr(file, "attached_to_doctype", None) or "") != EMPLOYEE_DOCTYPE:
        frappe.throw(_("Tệp này không đính kèm với nhân viên."))
    owner_employee = (employee or "").strip()
    if owner_employee and (getattr(file, "attached_to_name", None) or "") != owner_employee:
        frappe.throw(_("Tệp không thuộc về nhân viên này."))
    frappe.delete_doc("File", file_name, ignore_permissions=True)
    try:
        from gege_hr.gege_hr.api.admin import _audit_admin, _company_for_employee

        _audit_admin(
            _("Xoá tệp đính kèm"),
            reference_doctype="File",
            reference_name=file_name,
            company=_company_for_employee(owner_employee or getattr(file, "attached_to_name", None)),
            employee=owner_employee or getattr(file, "attached_to_name", None),
        )
    except Exception:
        frappe.log_error(title="employee_profile.delete_employee_attachment audit failed")
    return {"name": file_name, "deleted": True}


# =========================================================================== #
# Phần 2 — Vòng đời + sơ đồ tổ chức + bulk + cấp phép (plan §2.4–§2.6, P1–P3)
# =========================================================================== #
HR_MANAGER_ROLES = ["HR Manager", "System Manager"]

# Các field create_transfer biết áp lên Employee (qua Employee Property History).
_TRANSFER_FIELDS = {
    "department": "Phòng ban",
    "designation": "Chức vụ",
    "branch": "Chi nhánh",
    "reports_to": "Quản lý",
}


def _require_hr_manager() -> None:
    """Gate chặt hơn cho các thao tác vòng đời (chỉ HR Manager/System Manager)."""
    frappe.only_for(HR_MANAGER_ROLES)


def validate_offboard(doc: dict, relieving_date, reason_for_leaving, reasons_allowed=None) -> dict:
    """Pure (BE-EP-03) — raise ``ValueError`` với message tiếng Việt khi sai."""
    if (doc.get("status") or "") == "Left":
        raise ValueError("Nhân viên đã ở trạng thái Nghỉ việc.")
    relieving_date = str(relieving_date or "").strip()[:10]
    if not relieving_date:
        raise ValueError("Vui lòng nhập ngày nghỉ việc.")
    doj = str(doc.get("date_of_joining") or "")[:10]
    if doj and relieving_date < doj:
        raise ValueError("Ngày nghỉ việc không được trước ngày vào làm.")
    reason = str(reason_for_leaving or "").strip()
    if not reason:
        raise ValueError("Vui lòng nhập lý do nghỉ việc.")
    if reasons_allowed and reason not in reasons_allowed:
        raise ValueError("Lý do nghỉ việc không hợp lệ.")
    return {"relieving_date": relieving_date, "reason_for_leaving": reason}


def _reason_for_leaving_options() -> list[str]:
    """Select options của Employee.reason_for_leaving theo meta (fallback [])."""
    try:
        field = frappe.get_meta(EMPLOYEE_DOCTYPE).get_field("reason_for_leaving")
        return [s.strip() for s in str(getattr(field, "options", "") or "").split("\n") if s.strip()]
    except Exception:
        return []


@frappe.whitelist()
def offboard_employee(
    employee: str,
    relieving_date: str,
    reason_for_leaving: str,
    deactivate_user: int | bool = 0,
) -> dict:
    """Chuyển Left + relieving_date/lý do (BE-EP-04/05, plan §2.4.1).

    ``doc.save()`` tự chạy hook ``handle_employee_status_change`` sẵn có (kết
    thúc Shift Assignment + huỷ Shift Instance tương lai).
    """
    _require_hr_manager()
    employee = (employee or "").strip()
    if not employee or not frappe.db.exists(EMPLOYEE_DOCTYPE, employee):
        frappe.throw(_("Nhân viên không tồn tại."))
    doc = frappe.get_doc(EMPLOYEE_DOCTYPE, employee)
    try:
        values = validate_offboard(
            doc.as_dict(), relieving_date, reason_for_leaving, _reason_for_leaving_options()
        )
    except ValueError as exc:
        frappe.throw(_(str(exc)))
    doc.set("status", "Left")
    doc.set("relieving_date", values["relieving_date"])
    doc.set("reason_for_leaving", values["reason_for_leaving"])
    doc.save(ignore_permissions=True)
    user_disabled = False
    user_id = getattr(doc, "user_id", None)
    if deactivate_user and user_id:
        frappe.db.set_value("User", user_id, "enabled", 0)
        user_disabled = True
    try:
        from gege_hr.gege_hr.api.admin import _audit_admin, _company_for_employee

        _audit_admin(
            _("Nghỉ việc nhân viên"),
            reference_doctype=EMPLOYEE_DOCTYPE,
            reference_name=employee,
            company=_company_for_employee(employee),
            employee=employee,
            new_value=f"{values['relieving_date']} · {values['reason_for_leaving']}",
        )
    except Exception:
        frappe.log_error(title="employee_profile.offboard_employee audit failed")
    return {
        "name": employee,
        "status": "Left",
        "relieving_date": values["relieving_date"],
        "reason_for_leaving": values["reason_for_leaving"],
        "user_disabled": user_disabled,
    }


@frappe.whitelist()
def reactivate_employee(employee: str, clear_dates: int | bool = 1) -> dict:
    """Left/Inactive → Active (plan §2.4.2); xoá ngày/lý do nghỉ khi ``clear_dates``."""
    _require_hr_manager()
    employee = (employee or "").strip()
    if not employee or not frappe.db.exists(EMPLOYEE_DOCTYPE, employee):
        frappe.throw(_("Nhân viên không tồn tại."))
    doc = frappe.get_doc(EMPLOYEE_DOCTYPE, employee)
    if getattr(doc, "status", None) not in ("Left", "Inactive"):
        frappe.throw(_("Chỉ nhân viên đang Nghỉ việc / Ngừng làm việc mới có thể kích hoạt lại."))
    doc.set("status", "Active")
    if clear_dates:
        doc.set("relieving_date", None)
        doc.set("reason_for_leaving", None)
    doc.save(ignore_permissions=True)
    try:
        from gege_hr.gege_hr.api.admin import _audit_admin, _company_for_employee

        _audit_admin(
            _("Kích hoạt lại nhân viên"),
            reference_doctype=EMPLOYEE_DOCTYPE,
            reference_name=employee,
            company=_company_for_employee(employee),
            employee=employee,
        )
    except Exception:
        frappe.log_error(title="employee_profile.reactivate_employee audit failed")
    return {"name": employee, "status": "Active"}


@frappe.whitelist()
def create_transfer(
    employee: str,
    transfer_date: str,
    new_department: str | None = None,
    new_designation: str | None = None,
    new_branch: str | None = None,
    new_reports_to: str | None = None,
) -> dict:
    """Tạo + submit HRMS Employee Transfer (plan §2.4.3) — HRMS tự copy giá trị mới."""
    _require_hr_manager()
    if not frappe.db.exists("DocType", "Employee Transfer"):
        frappe.throw(_("DocType Employee Transfer không có trên site này."))
    employee = (employee or "").strip()
    if not employee or not frappe.db.exists(EMPLOYEE_DOCTYPE, employee):
        frappe.throw(_("Nhân viên không tồn tại."))
    transfer_date = str(transfer_date or "").strip()[:10]
    if not transfer_date:
        frappe.throw(_("Vui lòng nhập ngày điều chuyển."))
    emp = frappe.get_doc(EMPLOYEE_DOCTYPE, employee)
    changes = {
        field: str(value or "").strip()
        for field, value in {
            "department": new_department,
            "designation": new_designation,
            "branch": new_branch,
            "reports_to": new_reports_to,
        }.items()
        if str(value or "").strip()
    }
    if not changes:
        frappe.throw(_("Chọn ít nhất một giá trị mới để điều chuyển."))
    details = [
        {
            "doctype": "Employee Property History",
            "fieldname": field,
            "property": _TRANSFER_FIELDS[field],
            "current": str(getattr(emp, field, None) or ""),
            "new": value,
        }
        for field, value in changes.items()
    ]
    doc = frappe.get_doc(
        {
            "doctype": "Employee Transfer",
            "employee": employee,
            "employee_name": getattr(emp, "employee_name", None),
            "company": getattr(emp, "company", None),
            "transfer_date": transfer_date,
            "transfer_details": details,
        }
    )
    doc.insert(ignore_permissions=True)
    doc.submit()
    return {"name": doc.name, "docstatus": doc.docstatus, "changed": changes}


def merge_history(transfers, promotions, audits, limit: int = 30) -> list[dict]:
    """Gộp Transfer + Promotion + Audit thành timeline vòng đời sort desc (pure)."""
    items: list[dict] = []
    for t in transfers or []:
        new_company = t.get("new_company")
        text = "Điều chuyển" + (f" → {new_company}" if new_company else "")
        items.append(
            {
                "source": "transfer",
                "at": str(t.get("transfer_date") or t.get("creation") or ""),
                "text": text,
            }
        )
    for p in promotions or []:
        items.append(
            {
                "source": "promotion",
                "at": str(p.get("promotion_date") or p.get("creation") or ""),
                "text": "Thăng chức",
            }
        )
    for a in audits or []:
        items.append(
            {
                "source": "audit",
                "at": str(a.get("created_at") or a.get("creation") or ""),
                "text": a.get("description") or a.get("audit_type") or "",
            }
        )
    items.sort(key=lambda i: i["at"], reverse=True)
    return items[: max(0, int(limit))]


@frappe.whitelist()
def get_lifecycle_history(employee: str) -> dict:
    """Tab Lịch sử của drawer (plan §2.4.3)."""
    _require_hr()
    employee = (employee or "").strip()
    transfers = _safe_rows(
        "Employee Transfer",
        {"employee": employee},
        ["transfer_date", "new_company", "creation"],
        "transfer_date desc",
        20,
    )
    promotions = _safe_rows(
        "Employee Promotion",
        {"employee": employee},
        ["promotion_date", "creation"],
        "promotion_date desc",
        20,
    )
    audits = _safe_rows(
        "VN Audit Event",
        {"employee": employee},
        ["audit_type", "actor", "created_at", "description"],
        "created_at desc",
        20,
    )
    return {
        "transfers": transfers,
        "promotions": promotions,
        "merged": merge_history(transfers, promotions, audits),
    }


# --------------------------------------------------------------------------- #
# P2 — Org chart + guard vòng lặp reports_to (plan §2.5)
# --------------------------------------------------------------------------- #
def detect_cycle(reports_to_map: dict, start: str | None = None) -> list[str]:
    """Pure (BE-EP-06) — đi theo reports_to; trả path có vòng ([] nếu không).

    ``start=None`` quét mọi node. Bảo hiểm độ sâu 200 để dữ liệu bẩn không treo.
    """

    def _walk(node: str) -> list[str]:
        path = [node]
        seen = {node}
        cur = node
        while len(seen) <= 200:
            nxt = str((reports_to_map or {}).get(cur) or "").strip()
            if not nxt:
                return []
            if nxt in seen:
                return path + [nxt]
            seen.add(nxt)
            path.append(nxt)
            cur = nxt
        return []

    if start:
        return _walk(str(start))
    for node in reports_to_map or {}:
        cycle = _walk(node)
        if cycle:
            return cycle
    return []


def build_tree(rows: list[dict], root: str | None = None, depth: int = 5):
    """Pure (BE-EP-07) — flat rows → cây lồng nhau + warnings khi gặp vòng."""
    by_name = {r.get("name"): r for r in rows or []}
    children: dict[str, list] = {}
    names = set(by_name)
    warnings: list[str] = []
    for r in rows or []:
        parent = str(r.get("reports_to") or "").strip()
        if not parent or parent not in names:
            continue
        children.setdefault(parent, []).append(r.get("name"))
    depth = max(1, min(int(depth or 5), 10))

    def _node(name: str, level: int, stack: tuple):
        row = dict(by_name.get(name) or {"name": name})
        if name in stack:
            warnings.append(f"Vòng lặp báo cáo tại {name} — đã cắt nhánh.")
            row["children"] = []
            return row
        kids = []
        if level < depth:
            for child in children.get(name, []):
                kids.append(_node(child, level + 1, stack + (name,)))
        row["children"] = kids
        return row

    if root:
        if root not in by_name:
            return [], [f"Không tìm thấy nhân viên {root}."]
        return [_node(root, 0, ())], warnings
    roots = [
        r.get("name")
        for r in rows or []
        if not str(r.get("reports_to") or "").strip() or str(r.get("reports_to")).strip() not in names
    ]
    return [_node(name, 0, ()) for name in roots], warnings


@frappe.whitelist()
def get_org_chart(root: str | None = None, depth: int = 5) -> dict:
    """Cây sơ đồ tổ chức từ reports_to (plan §2.5)."""
    _require_hr()
    fields = _existing_fields(
        EMPLOYEE_DOCTYPE,
        ["name", "employee_name", "designation", "department", "image", "status", "reports_to"],
    )
    try:
        rows = (
            frappe.get_all(EMPLOYEE_DOCTYPE, fields=fields, limit_page_length=1000, order_by="name asc") or []
        )
    except Exception:
        frappe.log_error(title="employee_profile.get_org_chart failed")
        rows = []
    tree, warnings = build_tree(rows, root=(root or "").strip() or None, depth=depth)
    return {"root": root or None, "total": len(rows), "tree": tree, "warnings": warnings}


# --------------------------------------------------------------------------- #
# P3 — Bulk update + cấp phép ban đầu (plan §2.6)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def bulk_update_employees(employees, field: str, value) -> dict:
    """Sửa 1 field whitelisted cho ≤100 nhân viên (BE-EP-08) — chạy qua
    ``Document.save()`` đầy đủ validation + hook."""
    _require_hr_manager()
    from gege_hr.gege_hr.api.admin import _EMPLOYEE_EDITABLE_FIELDS

    field = str(field or "").strip()
    if field not in _EMPLOYEE_EDITABLE_FIELDS:
        frappe.throw(_("Trường này không cho phép sửa hàng loạt."))
    if isinstance(employees, str):
        try:
            import json as _json

            employees = _json.loads(employees)
        except (TypeError, ValueError):
            employees = [employees]
    names = [str(n or "").strip() for n in (employees or []) if str(n or "").strip()]
    if not names:
        frappe.throw(_("Chọn ít nhất một nhân viên."))
    if len(names) > 100:
        frappe.throw(_("Chỉ cho phép cập nhật tối đa 100 nhân viên mỗi lần."))
    updated: list[str] = []
    failed: list[dict] = []
    for name in names:
        try:
            doc = frappe.get_doc(EMPLOYEE_DOCTYPE, name)
            doc.set(field, value)
            doc.save(ignore_permissions=True)
            updated.append(name)
        except Exception as exc:  # noqa: BLE001 — per-row fail không kéo cả lô
            failed.append({"name": name, "error": str(exc)})
    try:
        from gege_hr.gege_hr.api.admin import _audit_admin, _default_company

        _audit_admin(
            _("Sửa hàng loạt nhân viên"),
            reference_doctype=EMPLOYEE_DOCTYPE,
            reference_name=field,
            company=_default_company(),
            new_value=str(value),
        )
    except Exception:
        frappe.log_error(title="employee_profile.bulk_update_employees audit failed")
    return {"updated": updated, "failed": failed, "total": len(names)}


@frappe.whitelist()
def create_leave_allocation(
    employee: str,
    leave_type: str,
    new_leaves_allocated: float,
    from_date: str,
    to_date: str,
) -> dict:
    """Cấp phép ban đầu khi thuê (plan §2.6) — submit Leave Allocation chuẩn."""
    _require_hr_manager()
    employee = (employee or "").strip()
    leave_type = (leave_type or "").strip()
    if not employee or not leave_type or not str(from_date or "").strip() or not str(to_date or "").strip():
        frappe.throw(_("Thiếu thông tin cấp phép (nhân viên / loại phép / khoảng thời gian)."))
    try:
        days = float(new_leaves_allocated or 0)
    except (TypeError, ValueError):
        days = 0.0
    if days <= 0:
        frappe.throw(_("Số ngày cấp phép phải lớn hơn 0."))
    doc = frappe.get_doc(
        {
            "doctype": "Leave Allocation",
            "employee": employee,
            "leave_type": leave_type,
            "new_leaves_allocated": days,
            "from_date": str(from_date)[:10],
            "to_date": str(to_date)[:10],
        }
    )
    doc.insert(ignore_permissions=True)
    doc.submit()
    return {"name": doc.name, "docstatus": doc.docstatus, "days": days}
