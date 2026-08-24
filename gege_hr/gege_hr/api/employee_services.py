"""NEW-4 (hr-gap-audit 🟥) — Employee Grievance + Travel Request API.

Reuses Frappe HR's ``Employee Grievance`` and ``Travel Request`` DocTypes and
exposes a DNA-compliant surface: an employee files a grievance / travel request;
HR lists all + resolves/approves/rejects. Mirrors ``api/expense.py``.

Schema note (plan-test-complete-hr-extra P0bis): against the installed HRMS
version the grievance employee link is ``raised_by`` (not ``employee``), the
raised date is ``date`` and the resolution text field is ``resolution_detail``.
Travel Request carries NO status/from_date/to_date/total_travel_cost columns
(dates live in the ``itinerary`` child; ``purpose_of_travel`` is a Link), so the
portal contract maps onto the ``vn_status`` / ``vn_note`` / ``vn_from_date`` /
``vn_to_date`` / ``vn_purpose`` / ``vn_total_cost`` custom fields declared in
``custom_fields.py``; lists project them back to the legacy SPA keys.
"""

from __future__ import annotations

import frappe

from gege_hr.gege_hr.utils import notify, pagination

GRIEVANCE_DOCTYPE = "Employee Grievance"
TRAVEL_DOCTYPE = "Travel Request"

# Portal status vocabulary (vn_status custom field on Travel Request).
PORTAL_STATUSES = ("Draft", "Approved", "Rejected")
_DOCSTATUS_FALLBACK = {0: "Draft", 1: "Approved", 2: "Cancelled"}

_GRIEVANCE_FIELDS = [
    "name",
    "raised_by",
    "employee_name",
    "grievance_type",
    "subject",
    "status",
    "date",
]
_TRAVEL_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "vn_purpose",
    "vn_from_date",
    "vn_to_date",
    "vn_total_cost",
    "docstatus",
    "vn_status",
    "vn_note",
]


def portal_status(vn_status, docstatus=0) -> str:
    """Coalesce the travel status shown to the SPA (vn_status || docstatus)."""
    s = (str(vn_status or "")).strip()
    if s:
        return s
    try:
        ds = int(docstatus or 0)
    except (TypeError, ValueError):
        ds = 0
    return _DOCSTATUS_FALLBACK.get(ds, "Draft")


def date_order_ok(start, end) -> bool:
    """True when ``end`` is on/after ``start`` (ISO date strings — bench-free)."""
    a, b = str(start or "")[:10], str(end or "")[:10]
    if not a or not b:
        return False
    return b >= a


def project_grievance_row(row: dict) -> dict:
    """DB row → SPA contract: ``raised_by``/``date`` → ``employee``/``raised_on``."""
    out = dict(row or {})
    out["employee"] = out.pop("raised_by", None)
    out["raised_on"] = out.pop("date", None)
    return out


def project_travel_row(row: dict) -> dict:
    """DB row → SPA contract: vn_* → legacy keys (from_date/to_date/...)."""
    out = dict(row or {})
    out["status"] = portal_status(out.pop("vn_status", None), out.pop("docstatus", 0))
    out["purpose_of_travel"] = out.pop("vn_purpose", None)
    out["from_date"] = out.pop("vn_from_date", None)
    out["to_date"] = out.pop("vn_to_date", None)
    out["total_travel_cost"] = out.pop("vn_total_cost", None)
    return out


def _resolve(employee: str | None) -> str:
    if employee:
        return employee
    emp = frappe.db.get_value("Employee", {"user_id": frappe.session.user})
    if not emp:
        frappe.throw("Tài khoản chưa liên kết nhân viên.")
    return emp


def _is_manager() -> bool:
    roles = set(frappe.get_roles(frappe.session.user))
    return bool(roles & {"HR Manager", "HR User", "System Manager"})


def _assert_own(employee: str) -> None:
    if _is_manager():
        return
    own = frappe.db.get_value("Employee", {"user_id": frappe.session.user})
    if employee != own:
        frappe.throw("Bạn chỉ xem được yêu cầu của chính mình.")


def _today() -> str:
    try:
        return frappe.utils.nowdate()
    except Exception:
        from datetime import date as _d

        return _d.today().isoformat()


def _notify_outcome(employee: str, name: str, doctype: str, outcome: str, label: str, state: str) -> None:
    """Best-effort employee notification — never aborts the transition."""
    url = "/services" if doctype == GRIEVANCE_DOCTYPE else "/services"
    try:
        notify.push_notification(
            employee=employee,
            notification_type="Alert",
            title=f"Yêu cầu {label} đã {outcome}",
            message=f"Yêu cầu {label} của bạn đã được {outcome} ({state}).",
            reference_doctype=doctype,
            reference_name=name,
            action_url=url,
        )
    except Exception:
        pass


def _field_options(doctype, fieldname):
    """Read a Select field's option list straight from the DocType meta."""
    try:
        df = frappe.get_meta(doctype).get_field(fieldname)
        if df and df.fieldtype == "Select":
            return [o.strip() for o in (df.options or "").split("\n") if o.strip()]
    except Exception:
        pass
    return []


def _list(
    doctype,
    fields,
    filters,
    status,
    search,
    page,
    page_size,
    extra_filters=None,
    search_fields=None,
    status_field="status",
    projector=None,
    summary_field=None,
) -> dict:
    """DNA §6.6 — server-side list + filter + broad search + pagination.

    ``status`` / ``extra_filters`` are AND conditions (list form, keeps multiple
    conditions on the same field — DNA §6.6 B). ``search`` becomes ``or_filters``
    LIKE over ``search_fields`` (incl. numeric, DNA §6.6 A). Returns a
    server-aggregated ``summary`` (counts by status over the FULL filtered set —
    not page-scoped, DNA §3.5). ``projector`` maps raw DB rows to the SPA shape.
    """
    flt = list(filters or [])
    if status:
        flt.append([status_field, "=", status])
    if extra_filters:
        flt.extend(extra_filters)
    or_filters = None
    q = (search or "").strip()
    if q:
        like = f"%{pagination.escape_like(q)}%"
        or_filters = [[f, "like", like] for f in (search_fields or ["employee_name", "name", "subject"])]
    page = max(1, int(page or 1))
    page_size = max(1, int(page_size or 20))
    summary_field = summary_field or status_field
    try:
        rows = (
            frappe.get_all(
                doctype,
                filters=flt or None,
                or_filters=or_filters,
                fields=fields,
                order_by="creation desc",
                limit_start=(page - 1) * page_size,
                limit_page_length=page_size,
            )
            or []
        )
        all_rows = (
            frappe.get_all(
                doctype,
                filters=flt or None,
                or_filters=or_filters,
                fields=["name", summary_field],
                limit_page_length=0,
            )
            or []
        )
        total = len(all_rows)
        by_status = {}
        for r in all_rows:
            s = r.get(summary_field) or "—"
            by_status[s] = by_status.get(s, 0) + 1
        summary = {"total": total, "by_status": by_status}
    except Exception:
        frappe.log_error(title=f"employee_services.list {doctype} failed")
        return {"data": [], "total": 0, "summary": {"total": 0, "by_status": {}}}
    data = [projector(r) for r in rows] if projector else rows
    return {"data": data, "total": total, "summary": summary}


# --------------------------------------------------------------------------- #
# Grievance
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def my_grievances(
    employee=None,
    status=None,
    grievance_type=None,
    search=None,
    date_from=None,
    date_to=None,
    page=1,
    page_size=20,
):
    emp = _resolve(employee)
    _assert_own(emp)
    extra = []
    if grievance_type:
        extra.append(["grievance_type", "=", grievance_type])
    if date_from:
        extra.append(["date", ">=", date_from])
    if date_to:
        extra.append(["date", "<=", date_to])
    return _list(
        GRIEVANCE_DOCTYPE,
        _GRIEVANCE_FIELDS,
        [["raised_by", "=", emp]],  # HRMS field (there is no `employee` column)
        status,
        search,
        page,
        page_size,
        extra_filters=extra,
        search_fields=["employee_name", "name", "subject", "grievance_type"],
        projector=project_grievance_row,
    )


@frappe.whitelist()
def all_grievances(
    status=None, grievance_type=None, search=None, date_from=None, date_to=None, page=1, page_size=20
):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager xem tất cả.")
    extra = []
    if grievance_type:
        extra.append(["grievance_type", "=", grievance_type])
    if date_from:
        extra.append(["date", ">=", date_from])
    if date_to:
        extra.append(["date", "<=", date_to])
    return _list(
        GRIEVANCE_DOCTYPE,
        _GRIEVANCE_FIELDS,
        None,
        status,
        search,
        page,
        page_size,
        extra_filters=extra,
        search_fields=["employee_name", "name", "subject", "grievance_type"],
        projector=project_grievance_row,
    )


def _default_grievance_type() -> str | None:
    """First Grievance Type, seeding a generic "Chung" master when none exists.

    HRMS marks ``grievance_type`` mandatory — a fresh site has an empty master
    and every portal grievance would fail validation. The master is
    ``autoname: Prompt`` so the seed must pass ``__newname`` explicitly.
    Best-effort + idempotent (insert rolls back to a duplicate-name error).
    """
    try:
        existing = frappe.get_all("Grievance Type", pluck="name", order_by="name asc", limit_page_length=1)
        if existing:
            return existing[0]
        doc = frappe.new_doc("Grievance Type")
        doc.__newname = "Chung"
        doc.description = "Khiếu nại chung (gege_hr portal default)"
        doc.insert(ignore_permissions=True)
        return doc.name
    except Exception:
        frappe.log_error(title="employee_services.seed_grievance_type failed")
        return None


def _default_purpose_of_travel() -> str | None:
    """First Purpose of Travel, seeding a generic master when none exists.

    ``purpose_of_travel`` on Travel Request is a mandatory Link — the portal
    collects free text, so the doc links to a generic master while the free
    text is preserved in ``vn_purpose`` / ``description``. ``autoname`` is
    ``field:purpose_of_travel``.
    """
    try:
        existing = frappe.get_all("Purpose of Travel", pluck="name", order_by="name asc", limit_page_length=1)
        if existing:
            return existing[0]
        doc = frappe.new_doc("Purpose of Travel")
        doc.purpose_of_travel = "Công tác chung"
        doc.insert(ignore_permissions=True)
        return doc.name
    except Exception:
        frappe.log_error(title="employee_services.seed_purpose_of_travel failed")
        return None


@frappe.whitelist()
def submit_grievance(employee=None, grievance_type=None, subject=None, description=None):
    emp = _resolve(employee)
    _assert_own(emp)
    subject = (subject or "").strip()
    if not subject:
        frappe.throw("Cần chủ đề khiếu nại.")
    doc = frappe.new_doc(GRIEVANCE_DOCTYPE)
    doc.raised_by = emp  # ← HRMS employee link (not `employee`)
    # HRMS mandatory fields the portal form does not collect: a portal grievance
    # is by default about the submitter's own situation, so the "against" party
    # is the employee themselves; description falls back to the subject.
    doc.grievance_against_party = "Employee"
    doc.grievance_against = emp
    doc.grievance_type = grievance_type or _default_grievance_type()
    doc.subject = subject
    doc.description = (description or "").strip() or subject
    if not doc.get("date"):
        doc.date = _today()
    doc.insert(ignore_permissions=True)
    return {"name": doc.name, "subject": doc.subject}


@frappe.whitelist()
def resolve_grievance(name=None, resolution=None):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager xử lý khiếu nại.")
    doc = frappe.get_doc(GRIEVANCE_DOCTYPE, name)
    doc.status = "Resolved"
    doc.resolution_detail = resolution or ""  # ← HRMS field (`resolution_details` s)
    doc.resolved_by = frappe.session.user
    doc.resolution_date = _today()
    # Manager-gated above; ignore_permissions bypasses Frappe's per-employee
    # User-Permission link check on `raised_by` (an HR manager must resolve any
    # employee's grievance) — same pattern as api/handover.update_handover_status.
    doc.save(ignore_permissions=True)
    _notify_outcome(doc.get("raised_by"), name, GRIEVANCE_DOCTYPE, "xử lý", "khiếu nại", "Resolved")
    return {"name": doc.name, "status": doc.status}


@frappe.whitelist()
def grievance_options() -> dict:
    try:
        types = frappe.get_all("Grievance Type", pluck="name", order_by="name asc") or []
    except Exception:
        types = []
    return {
        "grievance_types": types,
        "grievance_statuses": _field_options(GRIEVANCE_DOCTYPE, "status"),
        # Travel Request has no native status Select — offer the portal vocab.
        "travel_statuses": list(PORTAL_STATUSES),
    }


# --------------------------------------------------------------------------- #
# Travel Request
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def my_travel_requests(
    employee=None, status=None, search=None, date_from=None, date_to=None, page=1, page_size=20
):
    emp = _resolve(employee)
    _assert_own(emp)
    extra = []
    if date_from:
        extra.append(["vn_from_date", ">=", date_from])
    if date_to:
        extra.append(["vn_to_date", "<=", date_to])
    return _list(
        TRAVEL_DOCTYPE,
        _TRAVEL_FIELDS,
        [["employee", "=", emp]],
        status,
        search,
        page,
        page_size,
        extra_filters=extra,
        search_fields=["employee_name", "name", "vn_purpose", "vn_total_cost"],
        status_field="vn_status",
        projector=project_travel_row,
    )


@frappe.whitelist()
def all_travel_requests(status=None, search=None, date_from=None, date_to=None, page=1, page_size=20):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager xem tất cả.")
    extra = []
    if date_from:
        extra.append(["vn_from_date", ">=", date_from])
    if date_to:
        extra.append(["vn_to_date", "<=", date_to])
    return _list(
        TRAVEL_DOCTYPE,
        _TRAVEL_FIELDS,
        None,
        status,
        search,
        page,
        page_size,
        extra_filters=extra,
        search_fields=["employee_name", "name", "vn_purpose", "vn_total_cost"],
        status_field="vn_status",
        projector=project_travel_row,
    )


@frappe.whitelist()
def submit_travel_request(
    employee=None, purpose_of_travel=None, from_date=None, to_date=None, estimated_cost=None
):
    emp = _resolve(employee)
    _assert_own(emp)
    if not (from_date and to_date and (purpose_of_travel or "").strip()):
        frappe.throw("Cần mục đích + ngày đi + ngày về.")
    # G2 — reject inverted travel ranges at the door.
    if not date_order_ok(from_date, to_date):
        frappe.throw("Ngày về không được trước ngày đi.")
    doc = frappe.new_doc(TRAVEL_DOCTYPE)
    doc.employee = emp
    # HRMS mandatory fields the portal form does not collect: a default
    # domestic trip against a generic Purpose of Travel master (seeded on
    # first use); the portal free-text is preserved in vn_purpose.
    doc.travel_type = "Domestic"
    doc.purpose_of_travel = _default_purpose_of_travel()
    # Travel Request's stock fields don't fit the portal contract — mirror it
    # onto the vn_* custom fields and keep the desk-visible description in sync.
    doc.vn_purpose = purpose_of_travel.strip()
    doc.description = purpose_of_travel.strip()
    doc.vn_from_date = from_date
    doc.vn_to_date = to_date
    if estimated_cost:
        try:
            doc.vn_total_cost = float(estimated_cost)
        except (TypeError, ValueError):
            pass
    doc.vn_status = "Draft"
    doc.insert(ignore_permissions=True)
    return {"name": doc.name, "from_date": from_date, "to_date": to_date}


@frappe.whitelist()
def approve_travel_request(name=None):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager duyệt công tác.")
    doc = frappe.get_doc(TRAVEL_DOCTYPE, name)
    doc.vn_status = "Approved"
    doc.save(ignore_permissions=True)  # manager-gated; bypass per-employee link check
    _notify_outcome(doc.get("employee"), name, TRAVEL_DOCTYPE, "duyệt", "công tác", "Approved")
    return {"name": doc.name, "status": "Approved"}


@frappe.whitelist()
def reject_travel_request(name=None, reason=None):
    """G5 — the missing manager action: reject a travel request with a reason."""
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager từ chối công tác.")
    doc = frappe.get_doc(TRAVEL_DOCTYPE, name)
    note = (getattr(doc, "vn_note", "") or "").strip()
    why = (str(reason or "")).strip()
    if why:
        tag = f"Từ chối: {why}"
        if tag not in note:
            note = f"{note} | {tag}".strip(" |")
    doc.vn_status = "Rejected"
    doc.vn_note = note
    doc.save(ignore_permissions=True)  # manager-gated; bypass per-employee link check
    _notify_outcome(doc.get("employee"), name, TRAVEL_DOCTYPE, "từ chối", "công tác", "Rejected")
    return {"name": doc.name, "status": "Rejected", "note": note}
