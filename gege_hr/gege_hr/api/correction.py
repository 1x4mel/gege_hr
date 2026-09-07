"""
Correction Request desk-free API — plans/correction-deskfree-complete.md.

Detail / edit / HR-browse / realtime endpoints fronting the VN Attendance
Correction Request DocType. The three legacy endpoints
(``my_correction_requests`` / ``submit_correction_request`` /
``cancel_correction_request``) stay in :mod:`gege_hr.gege_hr.api.attendance`
untouched (plan CD1 — zero breaking change); this module adds:

  * ``get_correction_request``    — one request + links + files + activity + can-matrix (§3.1)
  * ``update_correction_request`` — edit a Draft (owner or HR; scoped bypass, §3.2)
  * ``all_correction_requests``   — HR browse across every employee (§3.3)
  * ``_publish_correction``       — realtime ping for open ``/hr/correction`` tabs (§3.4)

The drawer is CR-specific where it matters: the *before → after* time diff
(``current_*`` vs ``requested_*``), the linked Checkout-Miss ticket and the
``generated_checkin`` / ``generated_attendance`` results stamped on approval.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import getdate

from gege_hr.gege_hr.api import audit as audit_api
from gege_hr.gege_hr.api.overtime import _normalize_input_dt
from gege_hr.gege_hr.utils import employee as emp_utils, pagination
from gege_hr.gege_hr.utils.request_workflow import send_for_approval

DOCTYPE = "VN Attendance Correction Request"

# Mirror of attendance._CR_LIST_FIELDS (kept in sync manually — the legacy
# endpoints own the canonical copy; this one feeds the detail drawer).
_DETAIL_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "work_date",
    "shift_instance",
    "work_session",
    "company",
    "correction_type",
    "current_checkin_time",
    "current_checkout_time",
    "requested_checkin_time",
    "requested_checkout_time",
    "reason",
    "attachment",
    "workflow_state",
    "approver",
    "approved_at",
    "generated_checkin",
    "generated_attendance",
    "docstatus",
]

# Whitelist for update_correction_request (plan §3.2). ``current_*`` times are
# re-prefilled by the doctype from the Work Session anyway, but the SPA sends
# them on create — accept them for edit parity with submit_correction_request.
_EDITABLE_FIELDS = (
    "work_date",
    "correction_type",
    "current_checkin_time",
    "current_checkout_time",
    "requested_checkin_time",
    "requested_checkout_time",
    "reason",
    "shift_instance",
    "work_session",
)

_REALTIME_EVENT = "correction_updated"

# Drawer action-matrix states (plan CD5) — the FE must NOT re-derive these.
_CANCELLABLE_STATES = ("Draft", "Pending Manager", "Pending HR")


# --------------------------------------------------------------------------- #
# Helpers (local copies — no module-level cross-import with attendance.py,
# which lazy-imports this module for the publish wire; see CD20)
# --------------------------------------------------------------------------- #
def _is_hr_manager() -> bool:
    roles = set(emp_utils.get_user_roles() or [])
    return bool(roles & emp_utils.HR_MANAGER_ROLES)


def _assert_own(employee: str) -> None:
    """HR/Manager may read anyone; a plain Employee only their own row."""
    if _is_hr_manager():
        return
    own = emp_utils.get_employee_for_user()
    if own != emp_utils.emp_name(employee):
        frappe.throw(
            _("Bạn không có quyền truy cập dữ liệu của nhân viên khác."),
            frappe.PermissionError,
        )


def _filter_rows(rows: list[dict], search: str | None, fields: tuple[str, ...]) -> list[dict]:
    """Server-side free-text filter across the given row fields (DNA §6.6 D)."""
    q = (search or "").strip().lower()
    if not q:
        return rows
    return [r for r in rows if any(q in str(r.get(k) or "").lower() for k in fields)]


def _publish_correction(doc=None) -> None:
    """Realtime ping for open ``/hr/correction`` tabs (plan CD6). Best-effort."""
    try:
        frappe.publish_realtime(
            _REALTIME_EVENT,
            {
                "doctype": DOCTYPE,
                "name": getattr(doc, "name", None) if doc is not None else None,
            },
        )
    except Exception:
        pass


def _shift_instance_meta(name: str | None) -> dict | None:
    """Planned-window meta of the linked Shift Instance for the drawer (best-effort)."""
    if not name:
        return None
    try:
        row = frappe.db.get_value(
            "VN Employee Shift Instance",
            name,
            ["name", "shift_type", "planned_start", "planned_end"],
            as_dict=True,
        )
        return dict(row) if row else None
    except Exception:
        return None


def _work_session_meta(name: str | None) -> dict | None:
    """Actual punch times + hours of the linked Work Session — the "before"
    side of the CR before/after diff (best-effort)."""
    if not name:
        return None
    try:
        row = frappe.db.get_value(
            "VN Attendance Work Session",
            name,
            [
                "name",
                "actual_checkin",
                "actual_checkout",
                "total_actual_hours",
                "calculation_status",
            ],
            as_dict=True,
        )
        return dict(row) if row else None
    except Exception:
        return None


def _checkout_miss_meta(name: str | None) -> dict | None:
    """The linked Checkout-Miss ticket (when the CR was opened from an
    explanation) — approving the CR auto-waives it (BUG-5). Best-effort."""
    if not name:
        return None
    try:
        row = frappe.db.get_value(
            "VN Checkout Miss",
            name,
            ["name", "status", "penalty_amount", "grace_deadline", "work_date"],
            as_dict=True,
        )
        return dict(row) if row else None
    except Exception:
        return None


def _checkin_meta(name: str | None) -> dict | None:
    """The Employee Checkin row generated on approval (best-effort)."""
    if not name:
        return None
    try:
        row = frappe.db.get_value("Employee Checkin", name, ["name", "time", "log_type"], as_dict=True)
        return dict(row) if row else None
    except Exception:
        return None


def _attendance_meta(name: str | None) -> dict | None:
    """The Attendance row generated on approval (best-effort)."""
    if not name:
        return None
    try:
        row = frappe.db.get_value("Attendance", name, ["name", "status", "attendance_date"], as_dict=True)
        return dict(row) if row else None
    except Exception:
        return None


def _activity_rows(name: str, limit: int = 15) -> list[dict]:
    """VN Approval Log rows for the doc — approval decisions incl. the reject
    comment (plan gap 4: the comment the manager typed in the inbox surfaces
    here for the employee). Best-effort: ``[]`` when the doctype is absent."""
    try:
        rows = frappe.db.get_all(
            "VN Approval Log",
            filters={"reference_doctype": DOCTYPE, "reference_name": name},
            fields=["name", "action", "from_state", "to_state", "actor", "comment", "creation"],
            order_by="creation desc",
            limit_page_length=limit,
        )
        return [dict(r) for r in rows]
    except Exception:
        return []


def _detail_can(doc, *, is_hr: bool, caller_emp: str | None) -> dict:
    """Action matrix for the drawer (plan CD5 — the BE is the single source of
    truth; the FE only renders what this returns)."""
    state = getattr(doc, "workflow_state", None) or ""
    docstatus = getattr(doc, "docstatus", 0) or 0
    actor = is_hr or bool(caller_emp and getattr(doc, "employee", None) == caller_emp)
    editable = docstatus == 0 and state == "Draft" and actor
    return {
        "edit": editable,
        "cancel": docstatus == 0 and state in _CANCELLABLE_STATES and actor,
        "resend": state == "Rejected" and actor,
        "upload": editable,
        "remove_file": editable,
        "print": True,
    }


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def get_correction_request(name: str | None = None) -> dict:
    """§3.1 — one correction request for the self-service drawer.

    Returns ``{doc, employee_name, links, attachments, activity, can}``.
    ``links`` carries best-effort Shift Instance (planned window), Work Session
    (actual punches — the "before" side), the linked Checkout-Miss ticket and
    the generated Checkin/Attendance rows (the approval "results"); ``can`` is
    the action matrix. HR may read anyone; a plain Employee only their own.
    """
    if not name:
        frappe.throw(_("Thiếu mã yêu cầu điều chỉnh."))
    doc = frappe.get_doc(DOCTYPE, name)
    _assert_own(doc.employee)

    detail = {f: getattr(doc, f, None) for f in _DETAIL_FIELDS}
    detail.update(
        {
            "owner": getattr(doc, "owner", None),
            "creation": str(getattr(doc, "creation", "") or ""),
            "modified": str(getattr(doc, "modified", "") or ""),
            "vn_checkout_miss": getattr(doc, "vn_checkout_miss", None),
        }
    )

    try:
        employee_name = frappe.db.get_value("Employee", doc.employee, "employee_name")
    except Exception:
        employee_name = None

    attachments: list[dict] = []
    try:
        attachments = [
            dict(r)
            for r in frappe.db.get_all(
                "File",
                filters={"attached_to_doctype": DOCTYPE, "attached_to_name": name},
                fields=["name", "file_name", "file_url", "is_private", "file_size"],
            )
        ]
    except Exception:
        attachments = []

    return {
        "doc": detail,
        "employee_name": employee_name,
        "links": {
            "shift_instance": _shift_instance_meta(getattr(doc, "shift_instance", None)),
            "work_session": _work_session_meta(getattr(doc, "work_session", None)),
            "vn_checkout_miss": _checkout_miss_meta(getattr(doc, "vn_checkout_miss", None)),
            "generated_checkin": _checkin_meta(getattr(doc, "generated_checkin", None)),
            "generated_attendance": _attendance_meta(getattr(doc, "generated_attendance", None)),
        },
        "attachments": attachments,
        "activity": _activity_rows(name),
        "can": _detail_can(
            doc,
            is_hr=_is_hr_manager(),
            caller_emp=emp_utils.get_employee_for_user(),
        ),
    }


@frappe.whitelist()
def update_correction_request(name: str | None = None, **kwargs) -> dict:
    """§3.2 — edit a Draft correction request (owner or HR; scoped bypass per CD2).

    Accepts the same flat payload as ``submit_correction_request`` (minus
    ``attachment`` — files go through the standard Frappe File 2-step upload).
    Validation (lock period / requested-change-present / reason length /
    no-duplicate) re-runs via ``doc.save()``; ``_validate_no_duplicate`` already
    excludes the row itself so a Draft edit never self-collides. A Draft that
    survived because the workflow was absent at submit time is pushed into the
    approval pipeline again after the edit (best-effort, mirrors submit).
    """
    if not name:
        frappe.throw(_("Thiếu mã yêu cầu điều chỉnh."))
    doc = frappe.get_doc(DOCTYPE, name)
    _assert_own(doc.employee)

    if getattr(doc, "docstatus", 0) != 0:
        frappe.throw(_("Chỉ sửa được yêu cầu chưa duyệt (Nháp)."), frappe.ValidationError)
    current_state = getattr(doc, "workflow_state", None) or ""
    if current_state != "Draft":
        frappe.throw(
            _("Yêu cầu đang chờ/đã duyệt — không thể sửa. Hủy và tạo lại nếu cần."),
            frappe.ValidationError,
        )

    if "reason" in kwargs and not str(kwargs.get("reason") or "").strip():
        frappe.throw(_("Lý do điều chỉnh không được để trống."), frappe.ValidationError)

    wanted: dict = {}
    for field in _EDITABLE_FIELDS:
        if field in kwargs and kwargs.get(field) is not None:
            value = kwargs.get(field)
            if field == "work_date":
                value = getdate(value)
            elif field in (
                "current_checkin_time",
                "current_checkout_time",
                "requested_checkin_time",
                "requested_checkout_time",
            ):
                value = _normalize_input_dt(value)
            setattr(doc, field, value)
            wanted[field] = value

    # Race-guard (clone of overtime.update_overtime_request): reload() re-reads
    # DB truth and wipes the in-memory edits — preserve them, and catch the case
    # where an approver moved the request on while the owner was editing.
    doc.reload()
    if getattr(doc, "docstatus", 0) != 0 or (getattr(doc, "workflow_state", None) or "") != "Draft":
        frappe.throw(_("Yêu cầu vừa được người khác cập nhật."), frappe.ValidationError)
    for field, value in wanted.items():
        setattr(doc, field, value)

    _flags = getattr(doc, "flags", None)
    if _flags is not None:
        _flags.ignore_permissions = True
    try:
        doc.save()
    finally:
        if _flags is not None:
            _flags.ignore_permissions = False

    send_for_approval(doc)
    audit_api.log(
        "Correction Update Draft",
        doc=doc.as_dict(),
        work_date=doc.work_date,
        description=f"sửa đơn nháp: {doc.name}",
    )
    _publish_correction(doc)
    return {
        "name": doc.name,
        "status": doc.workflow_state,
        "message": _("Đã cập nhật yêu cầu điều chỉnh {0}.").format(doc.name),
    }


def _enrich_all_rows(rows: list[dict]) -> list[dict]:
    """Add ``department`` (Employee) to each row via one batch query (§3.3).
    Degrades to ``None`` on failure."""
    out = [dict(r) for r in rows or []]
    emp_ids = {r.get("employee") for r in out if r.get("employee")}
    if emp_ids:
        try:
            dept_map = {
                r.get("name"): r.get("department")
                for r in frappe.db.get_all(
                    "Employee",
                    filters={"name": ["in", sorted(emp_ids)]},
                    fields=["name", "department"],
                )
            }
        except Exception:
            dept_map = {}
        for r in out:
            r.setdefault("department", dept_map.get(r.get("employee")))
    return out


def _is_pending_state(state) -> bool:
    lowered = str(state or "").lower()
    return lowered in ("draft", "open") or "pending" in lowered


@frappe.whitelist()
def all_correction_requests(
    from_date: str | None = None,
    to_date: str | None = None,
    status: str | None = None,
    correction_type: str | None = None,
    employee: str | None = None,
    department: str | None = None,
    company: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 0,
) -> list[dict] | dict:
    """§3.3 — HR browse across EVERY employee's correction requests (Desk list parity).

    HR Manager / System Manager only. Filters: date window / exact status /
    exact correction_type / exact employee / exact company / department (via the
    Employee table). Rows are enriched with ``employee_name`` (already on the
    row) and ``department``; the broad ``search`` OR-matches those too
    (post-query, DNA §6.6 D). Pagination is opt-in with a ``summary`` over the
    full filtered set (matching the CorrectionView tab counts).
    """
    if not _is_hr_manager():
        frappe.throw(_("Chức năng chỉ dành cho HR."), frappe.PermissionError)

    empty_summary = {"total": 0, "pending": 0, "approved": 0, "rejected": 0}
    filters: dict = {}
    if from_date or to_date:
        filters["work_date"] = ["between", [from_date or to_date, to_date or from_date]]
    if status:
        filters["workflow_state"] = status
    if correction_type:
        filters["correction_type"] = correction_type
    if company:
        filters["company"] = company
    if employee:
        filters["employee"] = emp_utils.emp_name(employee)
    elif department:
        try:
            emps = frappe.db.get_all("Employee", filters={"department": department}, pluck="name")
        except Exception:
            emps = []
        if not emps:
            return pagination.paginate_filtered([], page=page, page_size=page_size, summary=empty_summary)
        filters["employee"] = ["in", emps]

    rows = frappe.db.get_all(
        DOCTYPE,
        filters=filters,
        fields=_DETAIL_FIELDS,
        order_by="work_date desc, creation desc",
        limit_page_length=pagination.MAX_PAGE_SIZE * 10,
    )
    enriched = _enrich_all_rows(rows)
    filtered = _filter_rows(
        enriched,
        search,
        (
            "name",
            "correction_type",
            "reason",
            "work_date",
            "employee",
            "employee_name",
            "department",
        ),
    )
    summary = {
        "total": len(filtered),
        "pending": sum(1 for r in filtered if _is_pending_state(r.get("workflow_state"))),
        "approved": sum(1 for r in filtered if str(r.get("workflow_state") or "") == "Approved"),
        "rejected": sum(1 for r in filtered if str(r.get("workflow_state") or "") == "Rejected"),
    }
    return pagination.paginate_filtered(filtered, page=page, page_size=page_size, summary=summary)
