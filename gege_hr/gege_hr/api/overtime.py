"""
Overtime Request API — plan v5 §10.4 / doctype-design §13.

Milestone-2 read + submit endpoints fronting the VN Overtime Request DocType.
These map 1:1 to the frontend ``gege_hr.gege_hr.api.overtime.<fn>`` calls in
``hr-ui/src/api/index.js``:

  * ``my_overtime_requests`` — the caller's own OT requests (filtered by window)
  * ``submit_overtime_request`` — create a Draft OT request (validation + naming
    handled by the DocType); the workflow transitions move it toward Approved.

The lifecycle is workflow-driven (Draft → Pending Manager → Pending HR →
Approved → Confirmed/Rejected). Only Approved/Confirmed rows are picked up by
the calculation engine (``calc.get_approved_ot_requests``).
"""

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.utils import getdate

from gege_hr.gege_hr.api import audit as audit_api
from gege_hr.gege_hr.utils import employee as emp_utils, pagination
from gege_hr.gege_hr.utils.request_workflow import send_for_approval

DOCTYPE = "VN Overtime Request"

# SPA datetime payloads may arrive offset-qualified (EC-3 ``withLocalOffset``:
# ``"2026-10-06 17:00+07:00"``). MySQL rejects the raw string (1292) and this
# system stores naive PORTAL-WALL-CLOCK datetimes (formatTime PHASE-1 renders
# them as-is), so the correct normalisation is to strip the offset and keep
# the wall clock, padding seconds. Naive inputs pass through untouched.
_DT_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[ T](\d{1,2}):(\d{2})(?::\d{2})?(?:\.\d+)?\s*(?:[+-]\d{2}:?\d{2})?$"
)


def _normalize_input_dt(value):
    """Normalise an SPA datetime payload for storage (see _DT_RE note)."""
    if not value or not isinstance(value, str):
        return value
    m = _DT_RE.match(value.strip())
    if not m:
        return value
    return f"{m.group(1)} {int(m.group(2)):02d}:{m.group(3)}:00"


# Row shape returned to the SPA — kept stable/flat so the list renders without
# a second lookup (matches ``hr-ui/src/api/index.js`` OT request row docstring).
_LIST_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "work_date",
    "shift_instance",
    "work_session",
    "company",
    "overtime_type",
    "from_datetime",
    "to_datetime",
    "requested_hours",
    "actual_hours",
    "approved_hours",
    "workflow_state",
    "docstatus",
    "reason",
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _resolve(employee: str | None) -> str:
    """Resolve the employee for the caller (param > session user)."""
    if employee:
        return emp_utils.emp_name(employee)
    emp = emp_utils.get_employee_for_user()
    if not emp:
        frappe.throw(
            _("Tài khoản này chưa được liên kết với nhân viên."),
            frappe.PermissionError,
        )
    return emp


def _is_manager() -> bool:
    roles = set(emp_utils.get_user_roles() or [])
    return bool(roles & emp_utils.HR_MANAGER_ROLES)


def _assert_own(employee: str) -> None:
    """HR/Manager may read anyone; a plain Employee may only read their own row."""
    if _is_manager():
        return
    own = emp_utils.get_employee_for_user()
    if own != emp_utils.emp_name(employee):
        frappe.throw(
            _("Bạn không có quyền truy cập dữ liệu của nhân viên khác."),
            frappe.PermissionError,
        )


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
def _filter_rows(rows: list[dict], search: str | None, fields: tuple[str, ...]) -> list[dict]:
    """Server-side free-text filter across the given row fields (DNA §6.6 D).

    Applied after the rows are fetched so it never risks an ``or_filters``
    "column does not exist" error on a per-DocType list.
    """
    q = (search or "").strip().lower()
    if not q:
        return rows
    return [r for r in rows if any(q in str(r.get(k) or "").lower() for k in fields)]


def _to_float(value) -> float:
    """Best-effort numeric coercion (whitelist args + DB rows may be ``None``)."""
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _ot_summary(rows: list[dict], ws_approved: float | None = None) -> dict:
    """Aggregate the OT list over the *full* filtered set (DNA §6.6 A).

    Computed once on the filtered (pre-pagination) list so the SPA summary tiles
    stay correct on every page. Mirrors the employee OT tile semantics:

      * ``requested`` — Σ ``requested_hours``
      * ``approved``  — OT thực sự đã được tính (Work Session total when
        available; falls back to Σ ``approved_hours`` on the request rows).
        The per-row ``approved_hours`` is only stamped *after* a Work Session
        recalc, so it can lag — the Work Session figure is the source of truth
        (BUG-2 fix, plan T2).
      * ``pending``   — count of still-cancellable requests (``Draft`` /
        ``Pending Manager`` / ``Pending HR`` / ``Open`` — matches
        ``hrStatus.otIsCancellable``).
    """
    requested = 0.0
    approved_rowsum = 0.0
    pending = 0
    for r in rows or []:
        requested += _to_float(r.get("requested_hours"))
        approved_rowsum += _to_float(r.get("approved_hours"))
        state = str(r.get("workflow_state") or "").lower()
        if state in ("draft", "open") or "pending" in state:
            pending += 1
    approved = ws_approved if ws_approved is not None else approved_rowsum
    return {"requested": requested, "approved": approved, "pending": pending}


def _ws_approved_hours(employee: str, from_date, to_date) -> float:
    """Σ ``approved_overtime_hours`` across the employee's Work Sessions in the
    window — the authoritative "OT đã được tính" figure for the summary tile
    (plan T2 / BUG-2). Bench-safe: returns ``0`` when the doctype/table is absent
    or the read fails.
    """
    try:
        if not frappe.db.table_exists("VN Attendance Work Session"):  # type: ignore[attr-defined]
            return 0.0
    except Exception:
        return 0.0
    filters = {"employee": employee}
    if from_date or to_date:
        filters["work_date"] = ["between", [from_date or to_date, to_date or from_date]]
    try:
        rows = frappe.db.get_all(
            "VN Attendance Work Session",
            filters=filters,
            fields=["approved_overtime_hours"],
        )
    except Exception:
        return 0.0
    return _to_float(sum(_to_float(r.get("approved_overtime_hours")) for r in rows))


@frappe.whitelist()
def my_overtime_requests(
    employee: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    search: str | None = None,
    status: str | None = None,
    overtime_type: str | None = None,
    page: int = 1,
    page_size: int = 0,
) -> list[dict] | dict:
    """Plan §10.4 — the caller's OT requests, optionally narrowed by filters.

    Server-side filters (DNA §6.6 — gear popover Law #2):

      * ``from_date`` / ``to_date`` — ``work_date`` window
      * ``status``          — exact ``workflow_state`` (Draft / Pending Manager /
        Pending HR / Approved / Confirmed / Rejected)
      * ``overtime_type``   — exact ``overtime_type`` (Pre-shift / Post-shift /
        Holiday / Custom)

    ``search`` OR-matches a free-text query across the row's text **and numeric**
    fields (name / overtime_type / reason / work_date / employee /
    employee_name / requested_hours / approved_hours), applied server-side
    (DNA §6.6 A/D — broad search covers numeric columns too, HR-BL-08). Managers
    (HR Manager/System Manager) may pass any ``employee``; a plain Employee is
    scoped to own.

    Pagination is **opt-in** (DNA §6.6 A): pass ``page`` + a positive
    ``page_size`` to receive
    ``{"data": [...], "total": int, "summary": {requested, approved, pending}}``
    — ``total`` is the filtered list length and ``summary`` aggregates the full
    filtered set (not just the page); without ``page_size`` the legacy bare-list
    return is preserved.
    """
    emp = _resolve(employee)
    _assert_own(emp)

    filters = {"employee": emp}
    if from_date or to_date:
        filters["work_date"] = ["between", [from_date or to_date, to_date or from_date]]
    if status:
        filters["workflow_state"] = status
    if overtime_type:
        filters["overtime_type"] = overtime_type

    rows = frappe.db.get_all(
        DOCTYPE,
        filters=filters,
        fields=_LIST_FIELDS,
        order_by="work_date desc, creation desc",
        limit_page_length=pagination.MAX_PAGE_SIZE * 10,  # newest-first bound
    )
    filtered = _filter_rows(
        rows,
        search,
        (
            "name",
            "overtime_type",
            "reason",
            "work_date",
            "employee",
            "employee_name",
            "requested_hours",
            "approved_hours",
        ),
    )
    summary = _ot_summary(filtered, ws_approved=_ws_approved_hours(emp, from_date, to_date))
    return pagination.paginate_filtered(filtered, page=page, page_size=page_size, summary=summary)


@frappe.whitelist()
def submit_overtime_request(**kwargs) -> dict:
    """Plan §10.4 — create a Draft OT request.

    Accepts the flat payload the SPA sends: ``employee``, ``work_date``,
    ``overtime_type``, ``from_datetime``, ``to_datetime``, ``requested_hours``,
    ``reason`` (optional ``shift_instance``/``work_session``/``attachment``).

    Returns ``{ name, status, message }``. Validation (window, requested-hours
    cap, no-duplicate) runs in ``VNOvertimeRequest.validate``; naming in its
    ``before_insert`` hook (``OR-YYMMDD-XXXXXX``).
    """
    if not kwargs:
        frappe.throw(_("Thiếu dữ liệu yêu cầu tăng ca."))

    employee = (kwargs.get("employee") or "").strip()
    if employee:
        _assert_own(employee)
    emp = _resolve(employee)

    doc = frappe.new_doc(DOCTYPE)
    doc.update(
        {
            "employee": emp,
            "work_date": getdate(kwargs.get("work_date")) if kwargs.get("work_date") else None,
            "overtime_type": kwargs.get("overtime_type") or "Post-shift",
            "from_datetime": _normalize_input_dt(kwargs.get("from_datetime")),
            "to_datetime": _normalize_input_dt(kwargs.get("to_datetime")),
            "requested_hours": kwargs.get("requested_hours"),
            "reason": kwargs.get("reason") or "",
            "workflow_state": "Draft",
            "docstatus": 0,
        }
    )
    # Optional link fields.
    if kwargs.get("shift_instance"):
        doc.shift_instance = kwargs["shift_instance"]
    if kwargs.get("work_session"):
        doc.work_session = kwargs["work_session"]
    if kwargs.get("attachment"):
        att = str(kwargs["attachment"])
        # Only files uploaded through Frappe (/files/...) — an external URL
        # here would render off-site content inside the HR portal.
        if not att.startswith(("/files/", "/private/files/")):
            frappe.throw(_("Tệp đính kèm không hợp lệ."), frappe.ValidationError)
        doc.attachment = att

    doc.insert()
    # Move into the approval pipeline (Draft → Pending Manager). Best-effort:
    # stays Draft if the workflow isn't seeded yet.
    send_for_approval(doc)
    audit_api.log(
        "OT Submit",
        doc=doc.as_dict(),
        work_date=doc.work_date,
        description=f"Draft → {doc.workflow_state}",
    )
    _publish_overtime(doc)
    return {
        "name": doc.name,
        "status": doc.workflow_state,
        "message": _("Đã tạo yêu cầu tăng ca {0}.").format(doc.name),
    }


@frappe.whitelist()
def cancel_overtime_request(name: str | None = None) -> dict:
    """Cancel a Draft/Pending OT request (set Rejected, keep the audit trail).

    A dedicated endpoint is preferable to the SPA ``setValue`` fallback so the
    transition is consistent and permission-checked server-side.
    """
    if not name:
        frappe.throw(_("Thiếu mã yêu cầu tăng ca."))
    doc = frappe.get_doc(DOCTYPE, name)
    _assert_own(doc.employee)
    if doc.docstatus >= 2:
        frappe.throw(_("Yêu cầu này đã bị hủy."))
    if doc.workflow_state in ("Approved", "Confirmed"):
        frappe.throw(
            _("Yêu cầu đã duyệt không thể hủy từ phía nhân viên."),
            frappe.PermissionError,
        )
    from gege_hr.gege_hr.api import approval as approval_api

    prev_state = doc.workflow_state
    doc.workflow_state = "Rejected"
    if doc.docstatus == 1:
        doc.cancel()
    else:
        doc.save()
    # Keep the Work Session in sync if this request was previously engine-active
    # (Approved/Confirmed). For Draft/Pending cancels this is a harmless no-op
    # (BUG-1 fix, plan T1).
    approval_api._after_ot_state_change(doc, from_state=prev_state, to_state="Rejected")
    return {
        "name": name,
        "status": doc.workflow_state,
        "message": _("Đã hủy yêu cầu tăng ca {0}.").format(name),
    }


# --------------------------------------------------------------------------- #
# Desk-free COMPLETE (plans/overtime-deskfree-complete.md) — detail / edit /
# HR browse / realtime. Contracts: §3.1–§3.4. The three legacy endpoints above
# keep their exact signatures (zero breaking change).
# --------------------------------------------------------------------------- #
_EDITABLE_FIELDS = (
    "work_date",
    "overtime_type",
    "from_datetime",
    "to_datetime",
    "requested_hours",
    "reason",
    "shift_instance",
    "work_session",
)

_REALTIME_EVENT = "overtime_updated"

# Drawer action-matrix states (plan OT5) — the FE must NOT re-derive these.
_CANCELLABLE_STATES = ("Draft", "Pending Manager", "Pending HR")


def _publish_overtime(doc=None) -> None:
    """Realtime ping for open ``/hr/overtime`` tabs (plan OT6). Best-effort."""
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
    """Engine-truth OT figures of the linked Work Session for the drawer."""
    if not name:
        return None
    try:
        row = frappe.db.get_value(
            "VN Attendance Work Session",
            name,
            [
                "name",
                "total_actual_hours",
                "raw_overtime_hours",
                "approved_overtime_hours",
                "calculation_status",
            ],
            as_dict=True,
        )
        return dict(row) if row else None
    except Exception:
        return None


def _activity_rows(name: str, limit: int = 15) -> list[dict]:
    """VN Approval Log rows for the doc — approval decisions incl. the reject
    comment (plan OT3: the comment the manager typed in the inbox surfaces
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
    """Action matrix for the drawer (plan OT5 — the BE is the single source of
    truth; the FE only renders what this returns)."""
    state = getattr(doc, "workflow_state", None) or ""
    docstatus = getattr(doc, "docstatus", 0) or 0
    actor = is_hr or bool(caller_emp and getattr(doc, "employee", None) == caller_emp)
    editable = docstatus == 0 and state == "Draft" and actor
    return {
        "edit": editable,
        "cancel": docstatus == 0 and state in _CANCELLABLE_STATES and actor,
        "resend": state == "Rejected" and actor,
        "confirm": state == "Approved" and is_hr,
        "upload": editable,
        "remove_file": editable,
        "print": True,
    }


@frappe.whitelist()
def get_overtime_request(name: str | None = None) -> dict:
    """§3.1 — one OT request for the self-service drawer.

    Returns ``{doc, employee_name, links, attachments, activity, can}``.
    ``links`` carries best-effort Shift Instance (planned window) + Work Session
    (engine OT figures) metadata; ``can`` is the action matrix. HR may read
    anyone; a plain Employee only their own (``_assert_own``).
    """
    if not name:
        frappe.throw(_("Thiếu mã yêu cầu tăng ca."))
    doc = frappe.get_doc(DOCTYPE, name)
    _assert_own(doc.employee)

    detail = {f: getattr(doc, f, None) for f in _LIST_FIELDS}
    detail.update(
        {
            "owner": getattr(doc, "owner", None),
            "creation": str(getattr(doc, "creation", "") or ""),
            "modified": str(getattr(doc, "modified", "") or ""),
            "salary_component": getattr(doc, "salary_component", None),
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
        },
        "attachments": attachments,
        "activity": _activity_rows(name),
        "can": _detail_can(
            doc,
            is_hr=_is_manager(),
            caller_emp=emp_utils.get_employee_for_user(),
        ),
    }


@frappe.whitelist()
def update_overtime_request(name: str | None = None, **kwargs) -> dict:
    """§3.2 — edit a Draft OT request (owner or HR; scoped bypass per OT1).

    Accepts the same flat payload as ``submit_overtime_request`` (minus
    ``attachment`` — files go through the standard Frappe File 2-step upload).
    Validation (window / no-duplicate) re-runs via ``doc.save()``; the
    requested-hours cap only applies at creation (doctype rule). A Draft that
    survived because the workflow was absent at submit time is pushed into the
    approval pipeline again after the edit (best-effort, mirrors submit).
    """
    if not name:
        frappe.throw(_("Thiếu mã yêu cầu tăng ca."))
    doc = frappe.get_doc(DOCTYPE, name)
    _assert_own(doc.employee)

    if getattr(doc, "docstatus", 0) != 0:
        frappe.throw(_("Chỉ sửa được đơn chưa duyệt (Nháp)."), frappe.ValidationError)
    current_state = getattr(doc, "workflow_state", None) or ""
    if current_state != "Draft":
        frappe.throw(
            _("Đơn đang chờ/đã duyệt — không thể sửa. Hủy và tạo lại nếu cần."),
            frappe.ValidationError,
        )

    if "reason" in kwargs and not str(kwargs.get("reason") or "").strip():
        frappe.throw(_("Lý do tăng ca không được để trống."), frappe.ValidationError)

    wanted: dict = {}
    for field in _EDITABLE_FIELDS:
        if field in kwargs and kwargs.get(field) is not None:
            value = kwargs.get(field)
            if field == "work_date":
                value = getdate(value)
            elif field in ("from_datetime", "to_datetime"):
                value = _normalize_input_dt(value)
            setattr(doc, field, value)
            wanted[field] = value

    # Race-guard (clone of leave.update_draft): reload() re-reads DB truth and
    # wipes the in-memory edits — preserve them, and catch the case where an
    # approver moved the request on while the owner was editing.
    doc.reload()
    if getattr(doc, "docstatus", 0) != 0 or (getattr(doc, "workflow_state", None) or "") != "Draft":
        frappe.throw(_("Đơn vừa được người khác cập nhật."), frappe.ValidationError)
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
        "OT Update Draft",
        doc=doc.as_dict(),
        work_date=doc.work_date,
        description=f"sửa đơn nháp: {doc.name}",
    )
    _publish_overtime(doc)
    return {
        "name": doc.name,
        "status": doc.workflow_state,
        "message": _("Đã cập nhật yêu cầu tăng ca {0}.").format(doc.name),
    }


def _enrich_all_rows(rows: list[dict]) -> list[dict]:
    """Add ``department`` (Employee) + ``shift_type`` (Shift Instance) to each
    row via two batch queries (plan §3.3). Degrades to ``None`` on failure."""
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
    si_ids = {r.get("shift_instance") for r in out if r.get("shift_instance")}
    if si_ids:
        try:
            si_map = {
                r.get("name"): r.get("shift_type")
                for r in frappe.db.get_all(
                    "VN Employee Shift Instance",
                    filters={"name": ["in", sorted(si_ids)]},
                    fields=["name", "shift_type"],
                )
            }
        except Exception:
            si_map = {}
        for r in out:
            r.setdefault("shift_type", si_map.get(r.get("shift_instance")))
    return out


def _is_pending_state(state) -> bool:
    lowered = str(state or "").lower()
    return lowered in ("draft", "open") or "pending" in lowered


@frappe.whitelist()
def all_overtime_requests(
    from_date: str | None = None,
    to_date: str | None = None,
    status: str | None = None,
    overtime_type: str | None = None,
    employee: str | None = None,
    department: str | None = None,
    company: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 0,
) -> list[dict] | dict:
    """§3.3 — HR browse across EVERY employee's OT requests (Desk list parity).

    HR Manager / System Manager only. Filters: date window / exact status /
    exact overtime_type / exact employee / exact company / department (via the
    Employee table). Rows are enriched with ``employee_name`` (already on the
    row), ``department`` and ``shift_type``; the broad ``search`` OR-matches
    those too (post-query, DNA §6.6 D). Pagination is opt-in like
    ``my_overtime_requests`` with a ``summary`` over the full filtered set.
    """
    if not _is_manager():
        frappe.throw(_("Chức năng chỉ dành cho HR."), frappe.PermissionError)

    empty_summary = {
        "total": 0,
        "pending": 0,
        "approved_hours": 0.0,
        "requested_hours": 0.0,
    }
    filters: dict = {}
    if from_date or to_date:
        filters["work_date"] = ["between", [from_date or to_date, to_date or from_date]]
    if status:
        filters["workflow_state"] = status
    if overtime_type:
        filters["overtime_type"] = overtime_type
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
        fields=_LIST_FIELDS,
        order_by="work_date desc, creation desc",
        limit_page_length=pagination.MAX_PAGE_SIZE * 10,
    )
    enriched = _enrich_all_rows(rows)
    filtered = _filter_rows(
        enriched,
        search,
        (
            "name",
            "overtime_type",
            "reason",
            "work_date",
            "employee",
            "employee_name",
            "requested_hours",
            "approved_hours",
            "department",
            "shift_type",
        ),
    )
    summary = {
        "total": len(filtered),
        "pending": sum(1 for r in filtered if _is_pending_state(r.get("workflow_state"))),
        "approved_hours": round(sum(_to_float(r.get("approved_hours")) for r in filtered), 4),
        "requested_hours": round(sum(_to_float(r.get("requested_hours")) for r in filtered), 4),
    }
    return pagination.paginate_filtered(filtered, page=page, page_size=page_size, summary=summary)
