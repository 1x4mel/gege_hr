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

# services-deskfree P0 (§0 F8) — endpoint mới chỉ phục vụ 2 doctype này.
_SERVICE_DOCTYPES = (GRIEVANCE_DOCTYPE, TRAVEL_DOCTYPE)
_REALTIME_EVENT = "employee_services_updated"

# §2.1 — drawer detail field sets (list fields + HRMS extras + audit stamps).
_GRIEVANCE_DETAIL_FIELDS = _GRIEVANCE_FIELDS + [
    "description",
    "grievance_against_party",
    "grievance_against",
    "cause_of_grievance",
    "resolution_detail",
    "resolved_by",
    "resolution_date",
    "employee_responsible",
    "associated_document_type",
    "associated_document",
    "vn_note",
    "owner",
    "creation",
    "modified",
]
_TRAVEL_DETAIL_FIELDS = _TRAVEL_FIELDS + [
    "description",
    "travel_type",
    "purpose_of_travel",
    "travel_proof",
    "owner",
    "creation",
    "modified",
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
    # G24 (services-deskfree): deep-link thẳng tới tab + đơn trong SPA.
    tab = "grievance" if doctype == GRIEVANCE_DOCTYPE else "travel"
    url = f"/services?tab={tab}&doc={name}"
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


# --------------------------------------------------------------------------- #
# services-deskfree P0 — shared helpers (plan §2.0; clones overtime.py).
# --------------------------------------------------------------------------- #
def _append_note(existing, text) -> str:
    """Ghép ghi chú portal ``existing | text`` (không lặp — tách từ reject_travel_request)."""
    note = (str(existing or "")).strip()
    tag = (str(text or "")).strip()
    if not tag:
        return note
    if tag not in note:
        note = f"{note} | {tag}".strip(" |")
    return note


def _publish_services(doctype, name, employee=None, status=None) -> None:
    """Realtime ping cho mọi tab ``/hr/services`` đang mở (§2.10). Best-effort."""
    try:
        frappe.publish_realtime(
            _REALTIME_EVENT,
            {"doctype": doctype, "name": name, "employee": employee, "status": status},
        )
    except Exception:
        pass


def _caller_employee():
    """Employee liên kết caller (None khi chưa link / stub — không throw)."""
    try:
        return frappe.db.get_value("Employee", {"user_id": frappe.session.user})
    except Exception:
        return None


def _attachments(doctype, name) -> list:
    """File đính kèm của doc (pattern get_leave_application ~L660). Best-effort."""
    try:
        return [
            dict(r)
            for r in frappe.get_all(
                "File",
                filters={"attached_to_doctype": doctype, "attached_to_name": name},
                fields=["name", "file_name", "file_url", "is_private", "file_size"],
            )
        ]
    except Exception:
        return []


def _activity_rows(doctype, name, limit=15) -> list:
    """Timeline merge Version (ai sửa gì) + Comment (trao đổi) — 2 doctype chưa
    có VN Approval Log (P1c mới đăng ký engine). Best-effort."""
    rows = []
    try:
        for v in frappe.get_all(
            "Version",
            filters={"ref_doctype": doctype, "docname": name},
            fields=["name", "owner", "creation"],
            order_by="creation desc",
            limit_page_length=limit,
        ):
            rows.append(
                {"type": "version", "name": v.get("name"), "actor": v.get("owner"), "creation": v.get("creation")}
            )
    except Exception:
        pass
    try:
        for c in frappe.get_all(
            "Comment",
            filters={"reference_doctype": doctype, "reference_name": name, "comment_type": "Comment"},
            fields=["name", "owner", "creation", "content"],
            order_by="creation desc",
            limit_page_length=limit,
        ):
            rows.append(
                {
                    "type": "comment",
                    "name": c.get("name"),
                    "actor": c.get("owner"),
                    "creation": c.get("creation"),
                    "detail": (c.get("content") or "")[:200],
                }
            )
    except Exception:
        pass
    rows.sort(key=lambda r: str(r.get("creation") or ""), reverse=True)
    return rows[:limit]


def _detail_can(doctype, doc, is_hr: bool, caller_emp) -> dict:
    """Action-matrix cho drawer (plan §3 — BE là nguồn sự thật duy nhất, FE chỉ render).

    Grievance (F1): native ``status`` Open/Investigated/Resolved/Invalid, docstatus 0.
    Travel (F7): ``vn_status`` Draft/Approved/Rejected overlay + docstatus (F3).
    ``owner`` = caller_emp trùng employee của đơn; HR = _is_manager().
    """
    if doctype == GRIEVANCE_DOCTYPE:
        status = (doc.get("status") or "").strip()
        owner = bool(caller_emp) and doc.get("raised_by") == caller_emp
        actor = owner or is_hr
        return {
            "edit": actor and status == "Open",
            "withdraw": owner and status == "Open",
            "delete": actor and status in ("Open", "Invalid"),
            "resolve": is_hr and status in ("Open", "Investigated"),
            "investigate": is_hr and status == "Open",
            "invalidate": is_hr and status in ("Open", "Investigated"),
            "reopen": actor and status == "Resolved",
            "comment": actor,
            "upload": actor and status == "Open",  # upload_service_attachment gate
        }
    docstatus = int(doc.get("docstatus") or 0)
    vn = (doc.get("vn_status") or "").strip()
    owner = bool(caller_emp) and doc.get("employee") == caller_emp
    actor = owner or is_hr
    editable = docstatus == 0 and vn in ("Draft", "Rejected")
    return {
        "edit": editable and actor,
        "withdraw": docstatus == 0 and vn == "Draft" and actor,
        "delete": editable and actor,
        "approve": is_hr and docstatus == 0 and vn == "Draft",
        "reject": is_hr and docstatus == 0 and vn == "Draft",
        "cancel": is_hr and docstatus == 1,
        "resend": actor and ((docstatus == 0 and vn == "Rejected") or docstatus == 2),
        "comment": actor,
        "upload": docstatus == 0 and actor,  # upload_service_attachment gate
    }


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
    employee=None,
    status=None,
    grievance_type=None,
    search=None,
    date_from=None,
    date_to=None,
    page=1,
    page_size=20,
):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager xem tất cả.")
    extra = []
    if employee:  # G13 — HR lọc đơn của một nhân viên cụ thể
        extra.append(["raised_by", "=", employee])
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
    _publish_services(GRIEVANCE_DOCTYPE, doc.name, emp, doc.get("status") or "Open")
    return {"name": doc.name, "subject": doc.subject}


@frappe.whitelist()
def resolve_grievance(name=None, resolution=None, cause=None):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager xử lý khiếu nại.")
    doc = frappe.get_doc(GRIEVANCE_DOCTYPE, name)
    doc.status = "Resolved"
    doc.resolution_detail = resolution or ""  # ← HRMS field (`resolution_details` s)
    # F2 (services-deskfree): `cause_of_grievance` là mandatory-depends HRMS khi
    # Resolved. Bản Frappe này KHÔNG chặn server-side (probe B0 trên erp-hr.local)
    # nhưng vẫn điền cho đúng dữ liệu điều tra chuẩn HRMS (cause > resolution > subject).
    doc.cause_of_grievance = (cause or "").strip() or (resolution or "").strip() or (doc.get("subject") or "")
    doc.resolved_by = frappe.session.user
    doc.resolution_date = _today()
    # Manager-gated above; ignore_permissions bypasses Frappe's per-employee
    # User-Permission link check on `raised_by` (an HR manager must resolve any
    # employee's grievance) — same pattern as api/handover.update_handover_status.
    doc.save(ignore_permissions=True)
    _notify_outcome(doc.get("raised_by"), name, GRIEVANCE_DOCTYPE, "xử lý", "khiếu nại", "Resolved")
    _publish_services(GRIEVANCE_DOCTYPE, doc.name, doc.get("raised_by"), "Resolved")
    return {"name": doc.name, "status": doc.status}


@frappe.whitelist()
def grievance_options() -> dict:
    try:
        types = frappe.get_all("Grievance Type", pluck="name", order_by="name asc") or []
    except Exception:
        types = []
    try:
        purposes = frappe.get_all("Purpose of Travel", pluck="name", order_by="name asc") or []
    except Exception:
        purposes = []
    grievance_all = _field_options(GRIEVANCE_DOCTYPE, "status") or [
        "Open",
        "Investigated",
        "Resolved",
        "Invalid",
    ]
    return {
        "grievance_types": types,
        "grievance_statuses": _field_options(GRIEVANCE_DOCTYPE, "status"),
        # Travel Request has no native status Select — offer the portal vocab.
        "travel_statuses": list(PORTAL_STATUSES),
        # services-deskfree §2.7 — additive keys only (FE cũ degrade an toàn).
        "grievance_all_statuses": grievance_all,  # F1 full vocabulary cho filter
        "purposes_of_travel": purposes,  # G12 — dropdown master thay free-text mù
        "travel_types": ["Domestic", "International"],
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
def all_travel_requests(
    employee=None, status=None, search=None, date_from=None, date_to=None, page=1, page_size=20
):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager xem tất cả.")
    extra = []
    if employee:  # G13 — HR lọc đơn của một nhân viên cụ thể
        extra.append(["employee", "=", employee])
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
    _publish_services(TRAVEL_DOCTYPE, doc.name, emp, "Draft")
    return {"name": doc.name, "from_date": from_date, "to_date": to_date}


@frappe.whitelist()
def approve_travel_request(name=None):
    """P1 (§2.6/F3) — Approve = ``doc.submit()`` (Travel Request là submittable).

    Probe B0 (erp-hr.local): submit KHÔNG bị mandatory itinerary chặn;
    ``set_user("Administrator")`` scoped quanh submit vì User-Permission chặn
    manager submit đơn của NV khác (parity approve leave-extra F3: notify với
    user THẬT trước, publish sau khi restore).
    """
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager duyệt công tác.")
    doc = frappe.get_doc(TRAVEL_DOCTYPE, name)
    if (getattr(doc, "docstatus", 0) or 0) != 0:
        frappe.throw("Yêu cầu đã được duyệt.")
    doc.vn_status = "Approved"
    prev_user = frappe.session.user
    try:
        frappe.set_user("Administrator")
        doc.flags.ignore_permissions = True
        doc.submit()
    finally:
        frappe.set_user(prev_user)
    _notify_outcome(doc.get("employee"), name, TRAVEL_DOCTYPE, "duyệt", "công tác", "Approved")
    _publish_services(TRAVEL_DOCTYPE, doc.name, doc.get("employee"), "Approved")
    return {"name": doc.name, "status": "Approved", "docstatus": 1}


@frappe.whitelist()
def reject_travel_request(name=None, reason=None):
    """G5 — the missing manager action: reject a travel request with a reason."""
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager từ chối công tác.")
    doc = frappe.get_doc(TRAVEL_DOCTYPE, name)
    why = (str(reason or "")).strip()
    note = _append_note(getattr(doc, "vn_note", "") or "", f"Từ chối: {why}" if why else "")
    doc.vn_status = "Rejected"
    doc.vn_note = note
    doc.save(ignore_permissions=True)  # manager-gated; bypass per-employee link check
    _notify_outcome(doc.get("employee"), name, TRAVEL_DOCTYPE, "từ chối", "công tác", "Rejected")
    _publish_services(TRAVEL_DOCTYPE, doc.name, doc.get("employee"), "Rejected")
    return {"name": doc.name, "status": "Rejected", "note": note}


# --------------------------------------------------------------------------- #
# services-deskfree P0 — detail / update / withdraw / delete (plan §2.1-§2.4)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def get_grievance(name=None) -> dict:
    """§2.1 — một khiếu nại cho drawer self-service.

    Returns ``{doc, status, against_name, attachments, activity, can}`` với
    ``can`` là action-matrix §3-F1 (BE là nguồn sự thật duy nhất — FE chỉ render).
    HR xem được tất cả; Employee chỉ của chính mình (``_assert_own``).
    """
    if not name:
        frappe.throw("Thiếu mã khiếu nại.")
    doc = frappe.get_doc(GRIEVANCE_DOCTYPE, name)
    emp = doc.get("raised_by")
    _assert_own(emp)
    row = project_grievance_row({f: doc.get(f) for f in _GRIEVANCE_DETAIL_FIELDS})
    against_name = None
    if row.get("grievance_against") and row.get("grievance_against_party"):
        try:
            field = "employee_name" if row["grievance_against_party"] == "Employee" else None
            against_name = (
                frappe.db.get_value(row["grievance_against_party"], row["grievance_against"], field)
                or row["grievance_against"]
            )
        except Exception:
            against_name = row["grievance_against"]
    return {
        "doc": row,
        "status": row.get("status"),
        "against_name": against_name,
        "attachments": _attachments(GRIEVANCE_DOCTYPE, name),
        "activity": _activity_rows(GRIEVANCE_DOCTYPE, name),
        "can": _detail_can(GRIEVANCE_DOCTYPE, doc, _is_manager(), _caller_employee()),
    }


@frappe.whitelist()
def get_travel_request(name=None) -> dict:
    """§2.1 — một yêu cầu công tác cho drawer (itinerary + travel_proof — F4)."""
    if not name:
        frappe.throw("Thiếu mã yêu cầu công tác.")
    doc = frappe.get_doc(TRAVEL_DOCTYPE, name)
    emp = doc.get("employee")
    _assert_own(emp)
    row = project_travel_row({f: doc.get(f) for f in _TRAVEL_DETAIL_FIELDS})
    row["docstatus"] = doc.get("docstatus", 0) or 0  # projector pops it — drawer cần
    itinerary = []
    try:
        for r in doc.get("itinerary") or []:
            itinerary.append(
                {
                    "from_location": r.get("from_location"),
                    "to_location": r.get("to_location"),
                    "from_date": str(r.get("from_date") or ""),
                    "to_date": str(r.get("to_date") or ""),
                    "mode_of_travel": r.get("mode_of_travel"),
                }
            )
    except Exception:
        itinerary = []
    return {
        "doc": row,
        "status": row.get("status"),
        "travel_proof": doc.get("travel_proof"),
        "itinerary": itinerary,
        "attachments": _attachments(TRAVEL_DOCTYPE, name),
        "activity": _activity_rows(TRAVEL_DOCTYPE, name),
        "can": _detail_can(TRAVEL_DOCTYPE, doc, _is_manager(), _caller_employee()),
    }


@frappe.whitelist()
def update_grievance(name=None, grievance_type=None, subject=None, description=None) -> dict:
    """§2.2 — sửa khiếu nại đang mở (owner hoặc HR hộ; race-guard clone update_draft)."""
    if not name:
        frappe.throw("Thiếu mã khiếu nại.")
    doc = frappe.get_doc(GRIEVANCE_DOCTYPE, name)
    _assert_own(doc.get("raised_by"))
    if (doc.get("status") or "Open") != "Open":
        frappe.throw("Chỉ sửa được khiếu nại đang mở.")
    if grievance_type:
        doc.grievance_type = grievance_type
    if subject is not None:
        subject = (subject or "").strip()
        if not subject:
            frappe.throw("Cần chủ đề khiếu nại.")
        doc.subject = subject
    if description is not None:
        doc.description = (description or "").strip() or doc.get("subject")
    # BUG #1 pattern (plan-leave-calendar): reload() re-reads DB truth and wipes
    # the in-memory edits — preserve them across the reload and catch the race
    # where HR resolved while the owner was editing.
    wanted = {k: doc.get(k) for k in ("grievance_type", "subject", "description")}
    doc.reload()
    if (doc.get("status") or "Open") != "Open":
        frappe.throw("Khiếu nại đã được xử lý bởi người khác — không thể sửa.")
    for k, v in wanted.items():
        if v is not None:
            setattr(doc, k, v)
    _flags = getattr(doc, "flags", None)
    if _flags is not None:
        _flags.ignore_permissions = True
    try:
        doc.save()
    finally:
        if _flags is not None:
            _flags.ignore_permissions = False
    _publish_services(GRIEVANCE_DOCTYPE, doc.name, doc.get("raised_by"), doc.get("status"))
    return {"name": doc.name, "status": doc.get("status")}


@frappe.whitelist()
def update_travel_request(
    name=None, purpose_of_travel=None, from_date=None, to_date=None, estimated_cost=None
) -> dict:
    """§2.2 — sửa yêu cầu công tác Draft/Rejected (Rejected → reset Draft, parity update_draft)."""
    if not name:
        frappe.throw("Thiếu mã yêu cầu công tác.")
    doc = frappe.get_doc(TRAVEL_DOCTYPE, name)
    _assert_own(doc.get("employee"))
    was_rejected = (doc.get("vn_status") or "").strip() == "Rejected"
    if (doc.get("docstatus") or 0) != 0 or (doc.get("vn_status") or "").strip() not in ("Draft", "Rejected"):
        frappe.throw("Chỉ sửa được yêu cầu chưa duyệt.")
    # G2 — re-run đủ validate của submit path (đảo ngày chặn tại cửa).
    if not date_order_ok(from_date or doc.get("vn_from_date"), to_date or doc.get("vn_to_date")):
        frappe.throw("Ngày về không được trước ngày đi.")
    if purpose_of_travel is not None and (purpose_of_travel or "").strip():
        doc.vn_purpose = purpose_of_travel.strip()
        doc.description = purpose_of_travel.strip()
    if from_date:
        doc.vn_from_date = from_date
    if to_date:
        doc.vn_to_date = to_date
    if estimated_cost is not None:
        try:
            doc.vn_total_cost = float(estimated_cost)
        except (TypeError, ValueError):
            pass
    if was_rejected:
        doc.vn_status = "Draft"  # đơn đã sửa đi lại vòng duyệt
    wanted = {
        k: doc.get(k)
        for k in ("vn_purpose", "description", "vn_from_date", "vn_to_date", "vn_total_cost", "vn_status")
    }
    doc.reload()
    if (doc.get("docstatus") or 0) != 0 or (doc.get("vn_status") or "").strip() not in ("Draft", "Rejected"):
        frappe.throw("Yêu cầu đã được duyệt bởi người khác — không thể sửa.")
    for k, v in wanted.items():
        if v is not None:
            setattr(doc, k, v)
    _flags = getattr(doc, "flags", None)
    if _flags is not None:
        _flags.ignore_permissions = True
    try:
        doc.save()
    finally:
        if _flags is not None:
            _flags.ignore_permissions = False
    status = portal_status(doc.get("vn_status"), doc.get("docstatus"))
    _publish_services(TRAVEL_DOCTYPE, doc.name, doc.get("employee"), status)
    return {"name": doc.name, "status": status}


@frappe.whitelist()
def withdraw_grievance(name=None, note=None) -> dict:
    """§2.3 — NV rút đơn: Open → Invalid + note (F1/F7 — không thêm vocabulary)."""
    if not name:
        frappe.throw("Thiếu mã khiếu nại.")
    doc = frappe.get_doc(GRIEVANCE_DOCTYPE, name)
    emp = doc.get("raised_by")
    _assert_own(emp)
    if (doc.get("status") or "") != "Open":
        frappe.throw("Chỉ rút được khiếu nại đang mở.")
    doc.status = "Invalid"
    doc.vn_note = _append_note(doc.get("vn_note"), note or "Nhân viên tự rút đơn")
    _flags = getattr(doc, "flags", None)
    if _flags is not None:
        _flags.ignore_permissions = True
    try:
        doc.save()
    finally:
        if _flags is not None:
            _flags.ignore_permissions = False
    # Notify HR đảo chiều để P1 (parity leave-extra withdraw).
    _publish_services(GRIEVANCE_DOCTYPE, doc.name, emp, "Invalid")
    return {"name": doc.name, "status": "Invalid", "note": doc.vn_note}


@frappe.whitelist()
def withdraw_travel_request(name=None, note=None) -> dict:
    """§2.3 — NV rút đơn: Draft → Rejected + note prefix (F7 — parity leave-extra)."""
    if not name:
        frappe.throw("Thiếu mã yêu cầu công tác.")
    doc = frappe.get_doc(TRAVEL_DOCTYPE, name)
    emp = doc.get("employee")
    _assert_own(emp)
    if (doc.get("docstatus") or 0) != 0 or (doc.get("vn_status") or "").strip() != "Draft":
        frappe.throw("Chỉ rút được yêu cầu chưa duyệt.")
    doc.vn_status = "Rejected"
    doc.vn_note = _append_note(doc.get("vn_note"), note or "Nhân viên tự rút đơn")
    _flags = getattr(doc, "flags", None)
    if _flags is not None:
        _flags.ignore_permissions = True
    try:
        doc.save()
    finally:
        if _flags is not None:
            _flags.ignore_permissions = False
    _publish_services(TRAVEL_DOCTYPE, doc.name, emp, "Rejected")
    return {"name": doc.name, "status": "Rejected", "note": doc.vn_note}


@frappe.whitelist()
def cancel_travel_request(name=None, reason=None) -> dict:
    """§2.6 (P1/G17) — HR hủy sau duyệt: docstatus 1 → ``doc.cancel()`` + Rejected + note.

    Probe B0: Travel on_cancel không sinh side-effect (không cần pre-cancel
    link như leave-extra #5). Reason BẮT BUỘC (parity reject-sau-duyệt).
    """
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager hủy công tác.")
    why = (str(reason or "")).strip()
    if not why:
        frappe.throw("Cần lý do hủy sau duyệt.")
    doc = frappe.get_doc(TRAVEL_DOCTYPE, name)
    if (getattr(doc, "docstatus", 0) or 0) != 1:
        frappe.throw("Chỉ hủy được yêu cầu đã duyệt.")
    note = _append_note(getattr(doc, "vn_note", "") or "", f"Hủy sau duyệt: {why}")
    doc.vn_status = "Rejected"
    doc.vn_note = note
    prev_user = frappe.session.user
    try:
        frappe.set_user("Administrator")
        doc.flags.ignore_permissions = True
        doc.cancel()
    finally:
        frappe.set_user(prev_user)
    _notify_outcome(doc.get("employee"), name, TRAVEL_DOCTYPE, "hủy", "công tác", "Rejected")
    _publish_services(TRAVEL_DOCTYPE, doc.name, doc.get("employee"), "Rejected")
    return {"name": doc.name, "status": "Rejected", "note": note, "docstatus": 2}


# --------------------------------------------------------------------------- #
# services-deskfree P1 — grievance lifecycle + collaboration + operations (§2.5/§2.9)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def investigate_grievance(name=None, cause=None) -> dict:
    """§2.5 (P1/G18) — HR đánh dấu đang điều tra + nguyên nhân (F1/F2 chuẩn HRMS)."""
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager điều tra khiếu nại.")
    cause = (str(cause or "")).strip()
    if not cause:
        frappe.throw("Cần nguyên nhân điều tra.")
    doc = frappe.get_doc(GRIEVANCE_DOCTYPE, name)
    if (doc.get("status") or "") != "Open":
        frappe.throw("Chỉ điều tra khiếu nại đang mở.")
    doc.status = "Investigated"
    doc.cause_of_grievance = cause
    doc.save(ignore_permissions=True)
    _publish_services(GRIEVANCE_DOCTYPE, doc.name, doc.get("raised_by"), "Investigated")
    return {"name": doc.name, "status": "Investigated"}


@frappe.whitelist()
def invalidate_grievance(name=None, reason=None) -> dict:
    """§2.5 (P1/G18) — HR đóng đơn không xử lý (vocabulary chuẩn HRMS "Invalid")."""
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager đóng khiếu nại.")
    doc = frappe.get_doc(GRIEVANCE_DOCTYPE, name)
    if (doc.get("status") or "") not in ("Open", "Investigated"):
        frappe.throw("Chỉ đóng được khiếu nại chưa xử lý.")
    why = (str(reason or "")).strip() or "không nêu lý do"
    doc.status = "Invalid"
    doc.vn_note = _append_note(doc.get("vn_note"), f"HR đóng đơn: {why}")
    doc.save(ignore_permissions=True)
    _notify_outcome(doc.get("raised_by"), name, GRIEVANCE_DOCTYPE, "đóng", "khiếu nại", "Invalid")
    _publish_services(GRIEVANCE_DOCTYPE, doc.name, doc.get("raised_by"), "Invalid")
    return {"name": doc.name, "status": "Invalid", "note": doc.vn_note}


@frappe.whitelist()
def reopen_grievance(name=None, note=None) -> dict:
    """§2.5 (P1/G10) — NV (hoặc HR) mở lại đơn đã Resolved."""
    if not name:
        frappe.throw("Thiếu mã khiếu nại.")
    doc = frappe.get_doc(GRIEVANCE_DOCTYPE, name)
    emp = doc.get("raised_by")
    _assert_own(emp)
    if (doc.get("status") or "") != "Resolved":
        frappe.throw("Chỉ mở lại được khiếu nại đã xử lý.")
    doc.status = "Open"
    doc.vn_note = _append_note(doc.get("vn_note"), note or "NV đề nghị mở lại")
    _flags = getattr(doc, "flags", None)
    if _flags is not None:
        _flags.ignore_permissions = True
    try:
        doc.save()
    finally:
        if _flags is not None:
            _flags.ignore_permissions = False
    # Notify HR đảo chiều — best-effort P2 (parity withdraw).
    _publish_services(GRIEVANCE_DOCTYPE, doc.name, emp, "Open")
    return {"name": doc.name, "status": "Open", "note": doc.vn_note}


@frappe.whitelist()
def add_service_comment(doctype=None, name=None, text=None) -> dict:
    """§2.9 (P1/G6) — comment thread NV↔HR (Comment chuẩn Frappe, scoped sau gate)."""
    doctype = (str(doctype or "")).strip()
    if doctype not in _SERVICE_DOCTYPES:
        frappe.throw("DocType không hợp lệ.")
    if not name:
        frappe.throw("Thiếu mã bản ghi.")
    doc = frappe.get_doc(doctype, name)
    emp = doc.get("raised_by") if doctype == GRIEVANCE_DOCTYPE else doc.get("employee")
    _assert_own(emp)
    content = (str(text or "")).strip()
    if not content:
        frappe.throw("Thiếu nội dung bình luận.")
    c = frappe.new_doc("Comment")
    c.comment_type = "Comment"
    c.reference_doctype = doctype
    c.reference_name = name
    c.content = content
    c.insert(ignore_permissions=True)
    _publish_services(doctype, name, emp, "Commented")
    return {"name": c.name, "content": content, "actor": frappe.session.user}


@frappe.whitelist()
def upload_service_attachment(doctype=None, name=None, is_private=0) -> dict:
    """§2.9 (P1/G7) — attachment; Travel mirror thêm vào ``travel_proof`` (F4).

    Deviations leave-extra #3/#4 áp dụng nguyên văn: frappe KHÔNG map multipart
    FILES vào kwargs (đọc ``frappe.request.files['file']``) và ``save_file`` bản
    này không nhận ``ignore_permissions`` (dùng scoped ``frappe.flags``).
    """
    doctype = (str(doctype or "")).strip()
    if doctype not in _SERVICE_DOCTYPES:
        frappe.throw("DocType không hợp lệ.")
    if not name:
        frappe.throw("Thiếu mã bản ghi.")
    doc = frappe.get_doc(doctype, name)
    emp = doc.get("raised_by") if doctype == GRIEVANCE_DOCTYPE else doc.get("employee")
    _assert_own(emp)
    # Gate edit-state: grievance Open; travel Draft (parity upload leave-extra).
    if doctype == GRIEVANCE_DOCTYPE and (doc.get("status") or "") != "Open":
        frappe.throw("Chỉ đính kèm được khiếu nại đang mở.")
    if doctype == TRAVEL_DOCTYPE and (getattr(doc, "docstatus", 0) or 0) != 0:
        frappe.throw("Chỉ đính kèm được yêu cầu chưa duyệt.")
    fs = None
    request = getattr(frappe, "request", None)
    files = getattr(request, "files", None) if request is not None else None
    if files:
        fs = files.get("file")
    if fs is None:
        frappe.throw("Thiếu tệp đính kèm.")
    from frappe.utils.file_manager import save_file

    filename = getattr(fs, "filename", None) or "attachment"
    content = fs.read()
    try:
        frappe.flags.ignore_permissions = True
        out = save_file(filename, content, doctype, name, is_private=bool(is_private))
    finally:
        frappe.flags.ignore_permissions = False
    file_url = None
    if isinstance(out, dict):
        file_url = out.get("file_url")
    else:
        file_url = getattr(out, "file_url", None)
    if doctype == TRAVEL_DOCTYPE and file_url:
        doc.travel_proof = file_url  # F4 — native Attach field mirror
        doc.save(ignore_permissions=True)
    _publish_services(doctype, name, emp, "Attached")
    return {"file_name": filename, "file_url": file_url}


@frappe.whitelist()
def bulk_travel_action(names=None, action=None, reason=None) -> dict:
    """§2.9 (P1/G14) — bulk approve/reject qua ĐÚNG endpoint đơn lẻ (Administrator
    set_user path giữ nguyên). Partial-safe: per-row try/except, không chết cả lô."""
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager thao tác gộp.")
    if action not in ("approve", "reject"):
        frappe.throw("Hành động không hợp lệ.")
    items = []
    if isinstance(names, str):
        import json as _json

        try:
            items = _json.loads(names)
        except Exception:
            items = [n.strip() for n in names.split(",") if n.strip()]
    else:
        items = list(names or [])
    updated, failed = 0, []
    for n in [str(x).strip() for x in items if str(x).strip()][:100]:
        try:
            if action == "approve":
                approve_travel_request(name=n)
            else:
                reject_travel_request(name=n, reason=reason)
            updated += 1
        except Exception as e:
            failed.append({"name": n, "reason": str(e)[:200]})
    # Bulk publish 1 lần cuối với count (tránh realtime spam — §7).
    _publish_services(TRAVEL_DOCTYPE, None, None, f"bulk:{action}:{updated}")
    return {"updated": updated, "failed": failed}


@frappe.whitelist()
def service_summary() -> dict:
    """§2.9 (P1/G22) — đếm theo trạng thái cho HR tiles/badge."""
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager xem tổng quan.")
    out = {"grievance": {}, "travel": {}}
    try:
        for r in frappe.get_all(GRIEVANCE_DOCTYPE, fields=["status"], limit_page_length=0) or []:
            s = (r.get("status") if isinstance(r, dict) else getattr(r, "status", None)) or "—"
            out["grievance"][s] = out["grievance"].get(s, 0) + 1
    except Exception:
        pass
    try:
        for r in frappe.get_all(TRAVEL_DOCTYPE, fields=["vn_status", "docstatus"], limit_page_length=0) or []:
            vn = r.get("vn_status") if isinstance(r, dict) else getattr(r, "vn_status", None)
            ds = r.get("docstatus") if isinstance(r, dict) else getattr(r, "docstatus", 0)
            s = portal_status(vn, ds or 0)
            out["travel"][s] = out["travel"].get(s, 0) + 1
    except Exception:
        pass
    return out


@frappe.whitelist()
def delete_service_draft(doctype=None, name=None) -> dict:
    """§2.4 — xoá hẳn bản nháp (whitelist 2 doctype; scoped bypass sau ownership gate F6)."""
    doctype = (str(doctype or "")).strip()
    if doctype not in _SERVICE_DOCTYPES:
        frappe.throw("DocType không hợp lệ.")
    if not name:
        frappe.throw("Thiếu mã bản ghi.")
    doc = frappe.get_doc(doctype, name)
    if doc is None:
        frappe.throw("Không tìm thấy bản ghi.")
    emp = doc.get("raised_by") if doctype == GRIEVANCE_DOCTYPE else doc.get("employee")
    _assert_own(emp)
    if doctype == GRIEVANCE_DOCTYPE:
        if (doc.get("status") or "") not in ("Open", "Invalid"):
            frappe.throw("Chỉ xoá được khiếu nại chưa xử lý.")
    else:
        if (doc.get("docstatus") or 0) != 0:
            frappe.throw("Chỉ xoá được bản nháp.")
    frappe.delete_doc(doctype, name, ignore_permissions=True)
    _publish_services(doctype, name, emp, "Deleted")
    return {"name": name, "deleted": True}
