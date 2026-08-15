"""NEW-5 (hr-gap-audit 🟥) — Recruitment + Training API.

Reuses Frappe HR's ``Job Opening`` / ``Job Applicant`` / ``Training Event`` /
``Employee Training`` DocTypes and exposes a DNA-compliant surface: list job
openings, apply as an applicant; list training events + enroll. HR lists all
applicants + trainings. Mirrors ``api/expense.py``.
"""
from __future__ import annotations

import frappe

OPENING_DOCTYPE = "Job Opening"
APPLICANT_DOCTYPE = "Job Applicant"
TRAINING_EVENT_DOCTYPE = "Training Event"
EMP_TRAINING_DOCTYPE = "Employee Training"

_OPENING_FIELDS = ["name", "designation", "company", "status", "description", "posting_date"]
_APPLICANT_FIELDS = ["name", "applicant_name", "email_id", "phone_number", "job_title", "status"]
_EVENT_FIELDS = ["name", "event_name", "trainer_name", "start_time", "end_time", "status", "location"]
_EMP_TRAINING_FIELDS = ["name", "employee", "employee_name", "training_event", "status"]

# Broad-search (or_filters LIKE) fields per DocType — cover EVERY content column
# incl. display-name FKs (company / trainer_name) so broad search finds them too
# (DNA view-design-dna.md §6.6 A / Law #3).
_OPENING_SEARCH = ["name", "designation", "company", "description"]
_APPLICANT_SEARCH = ["name", "applicant_name", "email_id", "job_title"]
_EVENT_SEARCH = ["name", "event_name", "trainer_name", "location"]
_EMP_TRAINING_SEARCH = ["name", "employee_name", "training_event"]


def _is_manager() -> bool:
    roles = set(frappe.get_roles(frappe.session.user))
    return bool(roles & {"HR Manager", "HR User", "System Manager"})


def _list(doctype, fields, filters, status, search, page, page_size, search_fields=None) -> dict:
    flt = list(filters or [])
    if status:
        flt.append(["status", "=", status])
    or_filters = None
    q = (search or "").strip()
    if q:
        like = f"%{q}%"
        sf = search_fields or ["name"]
        or_filters = [[field, "like", like] for field in sf]
    page = max(1, int(page or 1))
    page_size = max(1, int(page_size or 20))
    try:
        rows = (
            frappe.get_all(
                doctype, filters=flt or None, or_filters=or_filters, fields=fields,
                order_by="creation desc", limit_start=(page - 1) * page_size, limit_page_length=page_size,
            )
            or []
        )
        total = len(frappe.get_all(doctype, filters=flt or None, or_filters=or_filters, fields=["name"], limit_page_length=0) or [])
    except Exception:
        frappe.log_error(title=f"recruitment.list {doctype} failed")
        rows, total = [], 0
    return {"data": rows, "total": total}


# ── Recruitment ──────────────────────────────────────────────────────────────
@frappe.whitelist()
def list_job_openings(status=None, search=None, page=1, page_size=20):
    # Status is now view-controlled (default "Open" is set on the FE filter),
    # so an explicit None means "all" — do NOT force "Open" server-side (DNA Law #2).
    return _list(OPENING_DOCTYPE, _OPENING_FIELDS, None, status, search, page, page_size, _OPENING_SEARCH)


@frappe.whitelist()
def submit_job_application(job_opening=None, applicant_name=None, email_id=None, phone_number=None, cover_letter=None):
    if not (applicant_name or "").strip() or not (email_id or "").strip():
        frappe.throw("Cần tên + email ứng viên.")
    opening_designation = None
    if job_opening:
        opening_designation = frappe.db.get_value(OPENING_DOCTYPE, job_opening, "designation")
    doc = frappe.new_doc(APPLICANT_DOCTYPE)
    doc.applicant_name = applicant_name.strip()
    doc.email_id = email_id.strip()
    doc.phone_number = phone_number or ""
    doc.job_title = opening_designation or ""
    doc.cover_letter = cover_letter or ""
    doc.status = "Open"
    doc.insert(ignore_permissions=True)
    return {"name": doc.name, "applicant_name": doc.applicant_name}


@frappe.whitelist()
def all_applicants(status=None, search=None, page=1, page_size=20):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager xem ứng viên.")
    return _list(APPLICANT_DOCTYPE, _APPLICANT_FIELDS, None, status, search, page, page_size, _APPLICANT_SEARCH)


# ── Training ─────────────────────────────────────────────────────────────────
@frappe.whitelist()
def list_training_events(status=None, search=None, page=1, page_size=20):
    return _list(TRAINING_EVENT_DOCTYPE, _EVENT_FIELDS, None, status, search, page, page_size, _EVENT_SEARCH)


@frappe.whitelist()
def enroll_training(employee=None, training_event=None):
    if not employee or not training_event:
        frappe.throw("Cần nhân viên + lớp đào tạo.")
    if not _is_manager():
        own = frappe.db.get_value("Employee", {"user_id": frappe.session.user})
        if employee != own:
            frappe.throw("Chỉ được đăng ký đào tạo cho chính mình.")
        if frappe.db.exists(EMP_TRAINING_DOCTYPE, {"employee": employee, "training_event": training_event}):
            frappe.throw("Nhân viên đã đăng ký lớp đào tạo này.")
    doc = frappe.new_doc(EMP_TRAINING_DOCTYPE)
    doc.employee = employee
    doc.training_event = training_event
    doc.status = "Mandatory"
    doc.insert(ignore_permissions=True)
    return {"name": doc.name, "training_event": training_event}


@frappe.whitelist()
def my_training(employee=None, status=None, page=1, page_size=20):
    if not employee:
        emp = frappe.db.get_value("Employee", {"user_id": frappe.session.user})
        if not emp:
            frappe.throw("Tài khoản chưa liên kết nhân viên.")
        employee = emp
    if not _is_manager():
        own = frappe.db.get_value("Employee", {"user_id": frappe.session.user})
        if employee != own:
            frappe.throw("Chỉ xem được đào tạo của chính mình.")
    return _list(EMP_TRAINING_DOCTYPE, _EMP_TRAINING_FIELDS, [["employee", "=", employee]], status, None, page, page_size)


@frappe.whitelist()
def all_training(status=None, search=None, page=1, page_size=20):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager xem tất cả đào tạo.")
    return _list(EMP_TRAINING_DOCTYPE, _EMP_TRAINING_FIELDS, None, status, search, page, page_size, _EMP_TRAINING_SEARCH)


@frappe.whitelist()
def recruitment_filter_options() -> dict:
    """Distinct status values per recruitment DocType — feeds the gear popover
    SearchableSelect (DNA §6.3) so no dropdown is ever empty.
    """
    def _distinct(doctype, field):
        try:
            rows = frappe.get_all(doctype, fields=[field])
        except Exception:
            frappe.log_error(title=f"recruitment.options {doctype} failed")
            return []
        return sorted({r.get(field) for r in rows if r.get(field)})

    return {
        "opening_statuses": _distinct(OPENING_DOCTYPE, "status"),
        "event_statuses": _distinct(TRAINING_EVENT_DOCTYPE, "status"),
        "training_statuses": _distinct(EMP_TRAINING_DOCTYPE, "status"),
    }
