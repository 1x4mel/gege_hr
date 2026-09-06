"""Leave API — plan v5 §10.5 / doctype-design A.6.

Fronts Frappe HR's ``Leave Application`` + ``Leave Allocation`` so the SPA
(``hr-ui``) leave views work end-to-end. The frontend contract
(``hr-ui/src/api/index.js`` leave block) maps 1:1 to these endpoints:

  * ``leave_type_options``    → active leave types + paid-flag
  * ``my_leave_balance``      → allocated / taken / pending / remaining per type
  * ``my_applications``       → the caller's applications (server-side
                               q/status/leave_type + pagination — desk-free v2)
  * ``preview_leave``         → leave-day/hour math + balance impact + warnings
  * ``apply``                 → create (and submit) a Leave Application
  * ``cancel_draft_or_pending``  → cancel an Open/Draft application outright
  * ``request_cancellation``  → flag an Approved application for HR cancellation
  * ``update_draft``          → edit an Open/Rejected draft (resubmit rejected)
  * ``delete_draft``          → hard-delete an own docstatus-0 draft
  * ``get_leave_application`` → detail + attachments + can-matrix for the drawer

Pure leave-day math lives in ``utils/leave.py`` (bench-free). Bench loaders
here are guarded so a missing Frappe HR table degrades gracefully rather than
500-ing the whole leave view.
"""

from __future__ import annotations

from contextlib import contextmanager

import frappe
from frappe import _
from frappe.utils import cint, getdate, today as frappe_today

from gege_hr.gege_hr.api import audit as audit_api
from gege_hr.gege_hr.utils import employee as emp_utils, leave as leave_utils, notify
from gege_hr.gege_hr.utils.request_workflow import send_for_approval


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _as_employee_id(value) -> str:
    """Coerce a leave-payload ``employee`` field to the Employee ID string.

    The SPA employee store may pass the *whole* Employee doc (a dict) instead
    of the bare ID, e.g. ``{"name": "HR-EMP-001", ...}``. Extract the id and
    strip it so downstream ``.strip()`` calls never raise ``AttributeError``.
    """
    if value is None:
        return ""
    if isinstance(value, dict):
        value = value.get("name") or value.get("employee") or value.get("employee_id") or ""
    return str(value).strip()


def _resolve(employee: str | None) -> str:
    if employee:
        return emp_utils.emp_name(employee)
    emp = emp_utils.get_employee_for_user()
    if not emp:
        frappe.throw(_("Tài khoản này chưa được liên kết với nhân viên."), frappe.PermissionError)
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


def _leave_type_meta(name: str) -> dict:
    """Paid-flag + display name for a Leave Type (bench-safe)."""
    try:
        row = (
            frappe.db.get_value(
                "Leave Type",
                name,
                ["name", "is_lwp", "is_ppl", "is_compensatory"],
                as_dict=True,
            )
            or {}
        )
    except Exception:
        row = {}
    is_lwp = cint(row.get("is_lwp")) == 1
    is_ppl = cint(row.get("is_ppl")) == 1
    return {
        "leave_type": name,
        "leave_type_name": name,
        "is_paid": not is_lwp,
        "is_lwp": bool(is_lwp),
        "is_ppl": bool(is_ppl),
        "description": "",
    }


# --------------------------------------------------------------------------- #
# Read endpoints
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def leave_type_options(employee: str | None = None) -> list[dict]:
    """Plan §10.5 — active leave types for the selector.

    Returns ``[{ leave_type, leave_type_name, is_paid, description }]`` to
    match the ``useLeave``/``SearchableSelect`` contract.
    """
    # NOTE: Frappe HRMS `Leave Type` has no `disabled`/`is_active` column, so we
    # must not filter on one — doing so throws OperationalError (1054) which the
    # bare except below used to swallow, silently returning [] (empty dropdown).
    try:
        rows = frappe.db.get_all("Leave Type", fields=["name"], order_by="name")
    except Exception:  # bench-safe: keep the view alive if HR is absent
        frappe.log_error(frappe.get_traceback(), "leave_type_options")
        rows = []
    out = []
    for r in rows:
        meta = _leave_type_meta(r.name)
        out.append(
            {
                "leave_type": meta["leave_type"],
                "leave_type_name": meta["leave_type_name"],
                "is_paid": meta["is_paid"],
                "description": meta["description"],
            }
        )
    return out


@frappe.whitelist()
def my_leave_balance(employee: str | None = None) -> list[dict]:
    """Plan §10.5 — remaining/allocated/taken/pending per leave type.

    Returns ``[{ leave_type, leave_type_name, total_leaves, leaves_taken,
    leaves_pending_approval, balance_leaves }]`` (FE contract 1:1).
    LV15 (plan-leave-deskfree): gate ``_assert_own`` — parity ``my_applications``
    (a plain employee must not read a colleague's balances).
    """
    emp = _resolve(employee)
    _assert_own(emp)
    out = []
    as_of = getdate(frappe_today())
    try:
        # G0 (plan-leave-deskfree §3.5): Leave Type has NO stock ``disabled``
        # column — filtering on it throws OperationalError 1054 which the
        # except below swallowed into an EMPTY balance list. Fetch all types,
        # parity with ``leave_type_options``.
        leave_types = frappe.db.get_all("Leave Type", fields=["name"], order_by="name")
    except Exception:
        leave_types = []

    for lt in leave_types:
        name = lt.name
        allocated = _allocated_leaves(emp, name, as_of)
        taken = _leaves_taken(emp, name, as_of)
        pending = _leaves_pending(emp, name)
        balance = max(0.0, round(float(allocated) - float(taken), 4))
        out.append(
            {
                "leave_type": name,
                "leave_type_name": name,
                "total_leaves": round(float(allocated), 4),
                "leaves_taken": round(float(taken), 4),
                "leaves_pending_approval": round(float(pending), 4),
                "balance_leaves": balance,
            }
        )
    return out


def _allocated_leaves(emp: str, leave_type: str, as_of) -> float:
    """Sum of active Leave Allocations for the type (bench-guarded)."""
    try:
        rows = frappe.db.get_all(
            "Leave Allocation",
            filters={
                "employee": emp,
                "leave_type": leave_type,
                "docstatus": 1,
                "from_date": ["<=", as_of],
                "to_date": [">=", as_of],
            },
            fields=["total_leaves_allocated"],
        )
    except Exception:
        return 0.0
    return sum((float(r.total_leaves_allocated or 0) for r in rows), 0.0)


def _leaves_taken(emp: str, leave_type: str, as_of) -> float:
    """Sum of Approved Leave Application days up to ``as_of``."""
    try:
        rows = frappe.db.get_all(
            "Leave Application",
            filters={
                "employee": emp,
                "leave_type": leave_type,
                "status": "Approved",
                "to_date": ["<=", as_of],
            },
            fields=["total_leave_days"],
        )
    except Exception:
        return 0.0
    return sum((float(r.total_leave_days or 0) for r in rows), 0.0)


def _leaves_pending(emp: str, leave_type: str) -> float:
    """Sum of Open (pending-approval) Leave Application days."""
    try:
        rows = frappe.db.get_all(
            "Leave Application",
            filters={
                "employee": emp,
                "leave_type": leave_type,
                "status": "Open",
            },
            fields=["total_leave_days"],
        )
    except Exception:
        return 0.0
    return sum((float(r.total_leave_days or 0) for r in rows), 0.0)


@frappe.whitelist()
def my_applications(
    employee: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    q: str = "",
    status: str = "",
    leave_type: str = "",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """plan-leave-deskfree §3.4 — the employee's applications, server-side filter.

    Returns ``{ data: [...], total: int }``. ``status="Cancelled"`` maps to
    ``docstatus=2``; every other status filters on ``status`` + ``docstatus<2``.
    ``q`` is a broad like-search over leave_type / description / name.
    Without the new params the filters are identical to the legacy shape (only
    the return envelope changed — FE synced in the same PR, D4).
    """
    emp = _resolve(employee)
    _assert_own(emp)

    filters = {"employee": emp}
    if from_date or to_date:
        filters["from_date"] = ["between", [from_date or to_date, to_date or from_date]]
    status = (status or "").strip()
    if status == "Cancelled":
        filters["docstatus"] = 2
    elif status:
        filters["status"] = status
        filters["docstatus"] = ["<", 2]
    if leave_type:
        filters["leave_type"] = leave_type

    q = (q or "").strip()
    or_filters = None
    if q:
        like = f"%{q}%"
        or_filters = [
            ["Leave Application", "leave_type", "like", like],
            ["Leave Application", "description", "like", like],
            ["Leave Application", "name", "like", like],
        ]

    limit = min(max(cint(limit) or 50, 1), 200)
    offset = max(cint(offset), 0)

    try:
        data = frappe.db.get_all(
            "Leave Application",
            filters=filters,
            or_filters=or_filters,
            fields=_application_fields(),
            order_by="posting_date desc, creation desc",
            limit=limit,
            start=offset,
        )
        total_rows = frappe.db.get_all(
            "Leave Application",
            filters=filters,
            or_filters=or_filters,
            fields=["name"],
            limit_page_length=0,
        )
        return {"data": data, "total": len(total_rows)}
    except Exception:
        return {"data": [], "total": 0}


# --------------------------------------------------------------------------- #
# Preview
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def preview_leave(**kwargs) -> dict:
    """Plan §10.5 — preview a leave request before submit.

    Accepts the flat SPA payload: ``employee``, ``leave_type``, ``from_date``,
    ``to_date``, ``half_day``, ``half_day_date``, ``description``.

    Returns ``{ leave_hours, leave_days, balance_before, balance_after,
    balance_impact, warnings: [], is_blocker }``.
    """
    employee = _as_employee_id(kwargs.get("employee"))
    if employee:
        _assert_own(employee)
    emp = _resolve(employee)

    leave_type = kwargs.get("leave_type") or ""
    from_date = leave_utils.coerce_date(kwargs.get("from_date"))
    to_date = leave_utils.coerce_date(kwargs.get("to_date"))
    half_day = leave_utils.to_bool(kwargs.get("half_day"))
    half_day_date = leave_utils.coerce_date(kwargs.get("half_day_date")) or from_date

    if from_date is None or to_date is None:
        frappe.throw(_("Vui lòng chọn ngày bắt đầu và ngày kết thúc nghỉ."))
    if to_date < from_date:
        frappe.throw(_("Ngày kết thúc không được trước ngày bắt đầu."))

    meta = _leave_type_meta(leave_type) if leave_type else {"is_lwp": False}
    balance_before = _balance_for(emp, leave_type) if leave_type else 0.0
    holidays = _holiday_dates(emp, from_date, to_date)

    return leave_utils.build_preview(
        from_date,
        to_date,
        balance_before,
        half_day=half_day,
        half_day_date=half_day_date,
        holidays=holidays,
        include_holidays=True,
        hours_per_day=_hours_per_day(emp),
        is_lwp=meta.get("is_lwp", False),
    )


def _balance_for(emp: str, leave_type: str) -> float:
    """Current balance for one leave type (reuse the balance endpoint math)."""
    as_of = getdate(frappe_today())
    allocated = _allocated_leaves(emp, leave_type, as_of)
    taken = _leaves_taken(emp, leave_type, as_of)
    return max(0.0, round(float(allocated) - float(taken), 4))


def _holiday_dates(emp: str, from_date, to_date):
    """Holiday dates within the window from the employee's Holiday List."""
    try:
        hl = frappe.db.get_value("Employee", emp, "holiday_list")
        if not hl:
            return []
        rows = frappe.db.get_all(
            "Holiday",
            filters={
                "parent": hl,
                "holiday_date": ["between", [from_date, to_date]],
            },
            fields=["holiday_date"],
        )
        return [getdate(r.holiday_date) for r in rows if r.holiday_date]
    except Exception:
        return []


def _hours_per_day(emp: str) -> float:
    """Standard hours/day from the employee's shift (best-effort, default 8)."""
    try:
        hours = frappe.db.get_value(
            "Shift Type",
            {"name": frappe.db.get_value("Employee", emp, "default_shift")},
            "vn_standard_hours_per_day",
        )
        return float(hours) if hours else 8.0
    except Exception:
        return 8.0


# --------------------------------------------------------------------------- #
# Apply / cancel / request-cancellation
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def apply(**kwargs) -> dict:
    """Plan §10.5 — create a Leave Application as a **Draft** (pending approval).

    Accepts: ``employee``, ``leave_type``, ``from_date``, ``to_date``,
    ``half_day``, ``half_day_date``, ``description`` (optional ``follow_up``
    / ``attach``). Validation (date sanity, balance) runs in Frappe's Leave
    Application ``validate``.

    The application is created as a Draft (``docstatus=0``, ``status='Open'``)
    so it sits in the approval pipeline PENDING. Approval is then the proper
    HRMS action ``doc.submit()`` (see :func:`_approve_one`), which runs
    ``on_submit`` → creates the **Leave Ledger Entry** (balance deducted) +
    sets ``status='Approved'`` + fires the audit hooks. (Previously this
    submitted on apply, leaving submitted+Open docs that Frappe cannot
    re-save/submit — making proper approval impossible.)

    Returns ``{ name, status, message }``.
    """
    if not kwargs:
        frappe.throw(_("Thiếu dữ liệu yêu cầu nghỉ phép."))

    employee = _as_employee_id(kwargs.get("employee"))
    if employee:
        _assert_own(employee)
    emp = _resolve(employee)

    leave_type = kwargs.get("leave_type")
    if not leave_type:
        frappe.throw(_("Vui lòng chọn loại nghỉ phép."))

    from_date = kwargs.get("from_date")
    to_date = kwargs.get("to_date")
    if not from_date or not to_date:
        frappe.throw(_("Vui lòng chọn ngày bắt đầu và ngày kết thúc nghỉ."))

    doc = frappe.new_doc("Leave Application")
    doc.update(
        {
            "employee": emp,
            "leave_type": leave_type,
            "from_date": getdate(from_date),
            "to_date": getdate(to_date),
            "half_day": 1 if leave_utils.to_bool(kwargs.get("half_day")) else 0,
            "half_day_date": leave_utils.coerce_date(kwargs.get("half_day_date")) or getdate(from_date),
            "description": kwargs.get("description") or "",
            "posting_date": getdate(frappe_today()),
        }
    )
    if kwargs.get("attach"):
        doc.attach = kwargs["attach"]

    # Designate the approver so HRMS shares the doc with them on insert (HRMS
    # ``share_doc_with_approver`` runs on save) — the approver can then submit
    # (approve) via the proper Frappe flow without any ignore_permissions bypass.
    try:
        from hrms.hr.doctype.leave_application.leave_application import get_leave_approver

        doc.leave_approver = get_leave_approver(emp)
    except Exception:
        doc.leave_approver = frappe.db.get_value("Employee", emp, "leave_approver") or None

    doc.insert()
    _stamp_blackout_decision(doc, leave_type=leave_type, from_date=from_date, to_date=to_date, employee=emp)
    # Keep the application as a Draft (docstatus=0, status 'Open') so it is
    # PENDING approval. Approval = doc.submit() in _approve_one (proper HRMS:
    # on_submit creates the Leave Ledger Entry + sets Approved + audit logs).
    status = doc.status or "Open"

    audit_api.log("Leave Submit", doc=doc.as_dict(), description=f"status → {status}")
    # The draft now shows on the desk-free calendar (Open chip) — refresh cache.
    _touch_calendar(doc)

    return {
        "name": doc.name,
        "status": status,
        "message": _("Đã tạo yêu cầu nghỉ phép {0}.").format(doc.name),
    }


@frappe.whitelist()
def cancel_draft_or_pending(name: str | None = None) -> dict:
    """Cancel an Open/Draft Leave Application outright (the owner only).

    Returns ``{ name, status }``. Already-Approved applications must go through
    ``request_cancellation`` (HR Manager cancels on the bench).
    """
    if not name:
        frappe.throw(_("Thiếu mã đơn nghỉ phép."))
    doc = frappe.get_doc("Leave Application", name)
    _assert_own(doc.employee)

    if doc.status in ("Approved", "Rejected"):
        frappe.throw(
            _("Đơn đã duyệt không thể tự hủy — vui lòng xin hủy qua HR."),
            frappe.PermissionError,
        )
    if doc.docstatus == 0:
        snapshot = doc.as_dict()
        # plan-leave-deskfree D2: the gege permission matrix intentionally strips
        # ``delete`` from Employee (F6) — elevate ONLY this delete after the
        # ``_assert_own`` gate, mirroring the scoped bypass in ``_save_or_submit``.
        _flags = getattr(doc, "flags", None)
        if _flags is not None:
            _flags.ignore_permissions = True
        try:
            doc.delete()
        finally:
            if _flags is not None:
                _flags.ignore_permissions = False
        audit_api.log("Leave Cancel", doc=snapshot, description="Draft deleted by owner")
        # Draft deletion fires no doc_events hook — refresh the calendar cache
        # so the Open chip disappears (the docstatus-1 cancel path is covered
        # by on_leave_cancel).
        _touch_calendar(snapshot)
        return {"name": name, "status": "Cancelled"}
    # docstatus 1 (Open) → cancel.
    doc.cancel()
    audit_api.log("Leave Cancel", doc=doc.as_dict(), description="Open application cancelled by owner")
    return {"name": name, "status": doc.status or "Cancelled"}


@frappe.whitelist()
def update_draft(name: str | None = None, **kwargs) -> dict:
    """plan-leave-deskfree §3.1 — sửa một đơn chưa duyệt (owner hoặc HR hộ).

    Chấp nhận payload như ``apply`` (``leave_type / from_date / to_date /
    half_day / half_day_date / description``). Hai nhánh:

    * ``status == "Open"``     → ``doc.save()`` proper flow (Employee có write
      perm theo permission matrix — validate HRMS chạy đủ, không bypass).
    * ``status == "Rejected"`` → set fields + reset ``status="Open"`` rồi save
      với scoped ``ignore_permissions`` — status là field permlevel-1 của HRMS
      (chỉ Leave Approver ghi được), cùng precedent ``_save_or_submit``.

    Returns ``{ name, status, message }``.
    """
    if not name:
        frappe.throw(_("Thiếu mã đơn nghỉ phép."))
    doc = frappe.get_doc("Leave Application", name)
    _assert_own(doc.employee)

    if getattr(doc, "docstatus", 0) != 0:
        frappe.throw(_("Chỉ sửa được đơn chưa duyệt (Nháp)."), frappe.ValidationError)
    if doc.status not in ("Open", "Rejected"):
        frappe.throw(_("Đơn ở trạng thái hiện tại không thể sửa."), frappe.ValidationError)
    was_rejected = doc.status == "Rejected"

    if kwargs.get("leave_type"):
        doc.leave_type = kwargs["leave_type"]
    if kwargs.get("from_date"):
        doc.from_date = getdate(kwargs["from_date"])
    if kwargs.get("to_date"):
        doc.to_date = getdate(kwargs["to_date"])
    doc.half_day = 1 if leave_utils.to_bool(kwargs.get("half_day")) else 0
    if doc.half_day:
        doc.half_day_date = leave_utils.coerce_date(kwargs.get("half_day_date")) or getattr(
            doc, "from_date", None
        )
    if "description" in kwargs:
        doc.description = kwargs.get("description") or ""

    # BUG #1 pattern (plan-leave-calendar): reload() re-reads DB truth and wipes
    # the in-memory edits — preserve them across the reload, and catch the race
    # where HR decided while the owner was editing.
    wanted = {
        key: getattr(doc, key, None)
        for key in ("leave_type", "from_date", "to_date", "half_day", "half_day_date", "description")
    }
    doc.reload()
    if getattr(doc, "docstatus", 0) != 0:
        frappe.throw(_("Đơn nghỉ đã được duyệt bởi người khác — không thể sửa."), frappe.ValidationError)
    for key, value in wanted.items():
        if value is not None:
            setattr(doc, key, value)

    if was_rejected:
        doc.status = "Open"  # the edited request re-enters the approval pipeline

    _flags = getattr(doc, "flags", None)
    if was_rejected and _flags is not None:
        _flags.ignore_permissions = True
    try:
        doc.save()
    finally:
        if was_rejected and _flags is not None:
            _flags.ignore_permissions = False

    _stamp_blackout_decision(
        doc,
        leave_type=getattr(doc, "leave_type", None),
        from_date=getattr(doc, "from_date", None),
        to_date=getattr(doc, "to_date", None),
        employee=getattr(doc, "employee", None),
    )
    audit_api.log(
        "Leave Update Draft",
        doc=doc.as_dict(),
        description=("gửi lại đơn sau khi bị từ chối: " if was_rejected else "sửa đơn nháp: ")
        + str(doc.name),
    )
    _touch_calendar(doc)
    return {
        "name": doc.name,
        "status": doc.status or "Open",
        "message": _("Đã cập nhật đơn nghỉ phép {0}.").format(doc.name),
    }


@frappe.whitelist()
def delete_draft(name: str | None = None) -> dict:
    """plan-leave-deskfree §3.2 — xoá hẳn một đơn docstatus-0 (Open/Rejected).

    Chủ yếu phục vụ đơn **Rejected** (owner dọn đơn bị từ chối không muốn sửa
    lại — ``cancel_draft_or_pending`` đang chặn status Rejected). Employee role
    không có delete perm trong permission matrix (F6) nên scoped bypass SAU
    gate ``_assert_own`` + guard docstatus (draft chưa có Leave Ledger Entry
    nên xoá an toàn — không ảnh hưởng số dư).
    """
    if not name:
        frappe.throw(_("Thiếu mã đơn nghỉ phép."))
    doc = frappe.get_doc("Leave Application", name)
    _assert_own(doc.employee)

    if getattr(doc, "docstatus", 0) != 0:
        frappe.throw(_("Chỉ xoá được đơn chưa duyệt (Nháp)."), frappe.ValidationError)
    if doc.status not in ("Open", "Rejected"):
        frappe.throw(_("Đơn ở trạng thái hiện tại không thể xoá."), frappe.ValidationError)

    snapshot = doc.as_dict()
    _flags = getattr(doc, "flags", None)
    if _flags is not None:
        _flags.ignore_permissions = True
    try:
        doc.delete()
    finally:
        if _flags is not None:
            _flags.ignore_permissions = False
    audit_api.log("Leave Delete Draft", doc=snapshot, description="draft deleted by owner")
    # Draft deletion fires no doc_events hook — refresh the calendar cache so
    # the chip disappears (mirror cancel_draft_or_pending).
    _touch_calendar(snapshot)
    return {"name": name}


@frappe.whitelist()
def get_leave_application(name: str | None = None) -> dict:
    """plan-leave-deskfree §3.3 — chi tiết một đơn cho drawer self-service.

    Returns ``{ name, doc, attachments, cancellation, can }`` với ``can`` là
    action-matrix suy ra từ ``(docstatus, status)`` — FE hiển thị nút đúng flow
    (parity docstatus matrix của các trang desk-free khác).
    """
    if not name:
        frappe.throw(_("Thiếu mã đơn nghỉ phép."))
    doc = frappe.get_doc("Leave Application", name)
    _assert_own(doc.employee)

    docstatus = getattr(doc, "docstatus", 0)
    status = getattr(doc, "status", None) or ""
    open_cancellation = _open_cancellation_request(name)

    fields = list(
        dict.fromkeys(_application_fields() + ["posting_date", "owner", "leave_approver", "company"])
    )
    detail = {f: getattr(doc, f, None) for f in fields}

    attachments: list[dict] = []
    try:
        attachments = frappe.db.get_all(
            "File",
            filters={"attached_to_doctype": "Leave Application", "attached_to_name": name},
            fields=["name", "file_name", "file_url", "is_private", "file_size"],
        )
    except Exception:
        attachments = []

    cancellation = None
    try:
        rows = frappe.db.get_all(
            "VN Leave Cancellation Request",
            filters={"leave_application": name},
            fields=["name", "status", "reason", "rejection_reason"],
            order_by="creation desc",
            limit_page_length=1,
        )
        cancellation = rows[0] if rows else None
    except Exception:
        cancellation = None

    # Activity timeline (plan-leave-deskfree §3.3 — audit-only, P1): the VN
    # Audit Event rows referencing this application (Leave Submit/Approve/
    # Reject/Cancel + the desk-free Update/Delete audits).
    activity: list[dict] = []
    try:
        activity = frappe.db.get_all(
            "VN Audit Event",
            filters={"reference_doctype": "Leave Application", "reference_name": name},
            fields=["name", "audit_type", "employee", "work_date", "description", "creation"],
            order_by="creation desc",
            limit_page_length=20,
        )
    except Exception:
        activity = []

    can = {
        "edit": docstatus == 0 and status in ("Open", "Rejected"),
        "delete": docstatus == 0 and status in ("Open", "Rejected"),
        "resubmit": docstatus == 0 and status == "Rejected",
        "cancel_draft": docstatus == 0 and status == "Open",
        "request_cancel": docstatus == 1 and not open_cancellation,
    }
    return {
        "name": name,
        "doc": detail,
        "attachments": attachments,
        "cancellation": cancellation,
        "activity": activity,
        "can": can,
    }


@frappe.whitelist()
def my_leave_allocations(employee: str | None = None, leave_type: str = "") -> list[dict]:
    """plan-leave-deskfree §3.6 (P1) — phân tích số dư theo Leave Allocation.

    Minh bạch parity Desk: mỗi lần cấp phép (kỳ, cấp mới / chuyển tiếp / tổng)
    cho một leave type — modal "Chi tiết phép" từ thẻ balance.
    """
    emp = _resolve(employee)
    _assert_own(emp)

    filters = {"employee": emp, "docstatus": 1}
    if (leave_type or "").strip():
        filters["leave_type"] = leave_type.strip()

    try:
        return frappe.db.get_all(
            "Leave Allocation",
            filters=filters,
            fields=[
                "name",
                "leave_type",
                "from_date",
                "to_date",
                "new_leaves_allocated",
                "carry_forwarded_leaves_sum",
                "total_leaves_allocated",
                "leave_policy_assignment",
            ],
            order_by="from_date desc",
            limit_page_length=50,
        )
    except Exception:
        return []


@frappe.whitelist()
def my_leave_ledger(
    employee: str | None = None,
    leave_type: str = "",
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """plan-leave-deskfree §3.6 (P1) — Leave Ledger Entry timeline của caller.

    Source of truth của số dư: mỗi dòng cấp (+) / trừ (−) / hết hạn, kèm
    ``transaction_type`` (Leave Allocation / Leave Application / Leave
    Encashment) + ``transaction_name`` để truy vết.
    """
    emp = _resolve(employee)
    _assert_own(emp)

    filters = {"employee": emp}
    if (leave_type or "").strip():
        filters["leave_type"] = leave_type.strip()
    if from_date or to_date:
        filters["from_date"] = ["between", [from_date or to_date, to_date or from_date]]

    limit = min(max(cint(limit) or 50, 1), 200)
    try:
        return frappe.db.get_all(
            "Leave Ledger Entry",
            filters=filters,
            fields=[
                "name",
                "employee",
                "leave_type",
                "transaction_type",
                "transaction_name",
                "leaves",
                "is_carry_forward",
                "is_expired",
                "from_date",
                "to_date",
                "creation",
            ],
            order_by="creation desc",
            limit=limit,
        )
    except Exception:
        return []


@frappe.whitelist()
def request_cancellation(name: str | None = None, reason: str | None = None) -> dict:
    """File a ``VN Leave Cancellation Request`` for an Approved/Open leave.

    Creates the dedicated cancellation-request document (routing it to
    ``Pending Manager`` so it shows in the Approval Inbox), back-links it on
    the Leave Application's ``vn_cancellation_request`` custom field (when the
    field is installed) and stamps ``vn_cancellation_requested``. An audit
    comment is appended to the leave timeline.

    Returns ``{ name, cancellation_request, status, message }``.
    """
    if not name:
        frappe.throw(_("Thiếu mã đơn nghỉ phép."))
    doc = frappe.get_doc("Leave Application", name)
    _assert_own(doc.employee)

    if not leave_utils.can_request_cancellation(doc.status):
        frappe.throw(
            _("Chỉ đơn đã duyệt/đang chờ mới có thể xin hủy."),
            frappe.PermissionError,
        )

    # Reject a duplicate open cancellation request for the same leave.
    existing = _open_cancellation_request(name)
    if existing:
        return {
            "name": name,
            "cancellation_request": existing,
            "status": doc.status,
            "message": _("Đã có yêu cầu hủy {0} đang chờ xử lý.").format(existing),
        }

    payload = leave_utils.cancellation_request_payload(
        name,
        employee=doc.employee,
        reason=reason,
        requested_by=frappe.session.user,
    )
    cr = frappe.new_doc("VN Leave Cancellation Request")
    cr.update(payload)
    cr.insert()
    send_for_approval(cr)
    cancellation_request = cr.name

    # Share the cancellation request with the employee's approver so they can act
    # on it via the unified inbox (proper Frappe DocShare — the owner shares; the
    # approver then has read/write). Mirrors HRMS ``share_doc_with_approver`` and
    # avoids the doc-level permission gap (HR Manager role grant alone is not
    # sufficient at doc level for this doctype).
    try:
        from hrms.hr.doctype.leave_application.leave_application import get_leave_approver

        approver = get_leave_approver(cr.employee)
        if approver and not frappe.db.exists(
            "DocShare",
            {"share_doctype": "VN Leave Cancellation Request", "share_name": cr.name, "user": approver},
        ):
            frappe.share.add_docshare(
                "VN Leave Cancellation Request",
                cr.name,
                approver,
                read=1,
                write=1,
                submit=1,
                share=1,
            )
    except Exception:
        frappe.log_error(title="share cancellation request with approver failed")

    # Back-link + flag on the Leave Application when the custom fields exist.
    _stamp_cancellation_link(name, cancellation_request, reason)

    # Audit comment (best-effort) so HR sees the request on the bench timeline.
    try:
        doc.add_comment(
            "Comment",
            _("Yêu cầu hủy {0}: {1}").format(cancellation_request, reason or ""),
        )
    except Exception:
        pass

    return {
        "name": name,
        "cancellation_request": cancellation_request,
        "status": doc.status,
        "message": _("Đã gửi yêu cầu hủy đơn {0}. HR sẽ xử lý sớm.").format(name),
    }


@frappe.whitelist()
def my_cancellation_requests(
    employee: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    """The caller's cancellation requests (optional date window on ``work_date``).

    HR/Managers may pass any ``employee``; a plain Employee may only list own.
    """
    emp = _resolve(employee)
    _assert_own(emp)

    filters = {"employee": emp}
    if from_date or to_date:
        filters["work_date"] = ["between", [from_date or to_date, to_date or from_date]]

    return _list_cancellation_requests(filters)


@frappe.whitelist()
def all_cancellation_requests(
    status: str | None = None,
    employee: str | None = None,
    department: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    """Every cancellation request across the company (HR/Manager only).

    Optional ``status`` filter (workflow_state), an ``employee`` filter and a
    date window on ``work_date``. ``department`` narrows the inbox to a team:
    the cancellation DocType has no ``department`` column, so it is resolved
    into the set of employees in that department (mirror of the leave-approval
    inbox filter). An empty roster short-circuits to no rows; an explicit
    ``employee`` outside the department likewise yields nothing.
    """
    if not _is_manager():
        frappe.throw(
            _("Chỉ HR/Quản lý mới xem được toàn bộ yêu cầu hủy."),
            frappe.PermissionError,
        )

    filters: dict = {}
    if status:
        filters["workflow_state"] = status
    if department:
        roster = _employees_in_department(department)
        if not roster:
            return []
        if employee:
            # Both filters supplied — the explicit employee must belong to the
            # department, otherwise the intersection is empty.
            if employee not in roster:
                return []
            filters["employee"] = employee
        else:
            filters["employee"] = ["in", roster]
    elif employee:
        filters["employee"] = employee
    if from_date or to_date:
        filters["work_date"] = ["between", [from_date or to_date, to_date or from_date]]

    return _list_cancellation_requests(filters)


@frappe.whitelist()
def approve_cancellation(name: str | None = None) -> dict:
    """Approve a cancellation request and cancel the linked Leave Application.

    HR/Manager only. Sets ``workflow_state=Approved``, stamps ``approved_by``/
    ``approved_at``, cancels the linked Leave Application (restoring the leave
    balance) and flags ``attendance_recalculated`` for recalculation. The
    request stays ``docstatus=0`` (consistent with the matrix-driven inbox).
    """
    _require_manager()
    if not name:
        frappe.throw(_("Thiếu mã yêu cầu hủy."))
    return _approve_cancellation_one(name)


def _approve_cancellation_one(name: str) -> dict:
    """Per-document cancellation approve (assumes the manager gate already
    passed). Raises on invalid state so a caller (single endpoint / bulk loop)
    can decide how to surface the failure. Shared with
    ``bulk_approve_cancellations``."""
    cr = frappe.get_doc("VN Leave Cancellation Request", name)
    if cr.workflow_state == "Approved":
        return {
            "name": name,
            "status": "Approved",
            "message": _("Yêu cầu hủy {0} đã được duyệt.").format(name),
        }
    if cr.workflow_state not in ("Pending Manager", "Pending HR", "Draft"):
        frappe.throw(
            _("Yêu cầu hủy đang ở trạng thái {0} — không thể duyệt.").format(cr.workflow_state),
            frappe.PermissionError,
        )

    cancelled_leave = False
    if cr.leave_application:
        cancelled_leave = _cancel_linked_leave(cr.leave_application)

    cr.workflow_state = "Approved"
    cr.approved_by = frappe.session.user
    try:
        cr.approved_at = frappe.utils.now()
    except Exception:
        pass
    if cancelled_leave:
        cr.attendance_recalculated = 1
    cr.save()

    try:
        notify.push_notification(
            employee=cr.employee,
            notification_type="Leave",
            title=_("Yêu cầu hủy phép đã được duyệt"),
            message=_("Yêu cầu hủy phép {0} của bạn đã được duyệt.").format(name),
            reference_doctype="VN Leave Cancellation Request",
            reference_name=name,
        )
    except Exception:
        pass

    # Audit the HR-driven leave cancellation (company resolved from the linked
    # Leave Application — the cancellation-request DocType has no company field).
    try:
        company = (
            frappe.db.get_value("Leave Application", cr.leave_application, "company")
            if cr.leave_application
            else None
        )
        audit_api.log(
            "Leave Cancel",
            company=company,
            employee=cr.employee,
            reference_doctype="VN Leave Cancellation Request",
            reference_name=name,
            description="HR duyệt yêu cầu hủy phép",
        )
    except Exception:
        pass

    # The linked leave just got cancelled — refresh the desk-free calendar
    # cache for its scope (approve-cancellation runs no Leave Application
    # doc_events hook itself).
    _touch_calendar_by_name(cr.leave_application)

    return {
        "name": name,
        "status": "Approved",
        "leave_cancelled": cancelled_leave,
        "message": _("Đã duyệt yêu cầu hủy {0}.").format(name),
    }


@frappe.whitelist()
def reject_cancellation(name: str | None = None, rejection_reason: str | None = None) -> dict:
    """Reject a cancellation request (HR/Manager only)."""
    _require_manager()
    if not name:
        frappe.throw(_("Thiếu mã yêu cầu hủy."))
    return _reject_cancellation_one(name, rejection_reason)


def _reject_cancellation_one(name: str, rejection_reason: str | None = None) -> dict:
    """Per-document cancellation reject (assumes the manager gate already
    passed). Shared with ``bulk_reject_cancellations``."""
    cr = frappe.get_doc("VN Leave Cancellation Request", name)
    if cr.workflow_state == "Rejected":
        return {
            "name": name,
            "status": "Rejected",
            "message": _("Yêu cầu hủy {0} đã bị từ chối.").format(name),
        }
    if cr.workflow_state in ("Approved", "Cancelled"):
        frappe.throw(
            _("Yêu cầu hủy đã chốt — không thể từ chối."),
            frappe.PermissionError,
        )

    cr.workflow_state = "Rejected"
    cr.rejection_reason = (rejection_reason or "").strip()
    cr.save()

    try:
        notify.push_notification(
            employee=cr.employee,
            notification_type="Leave",
            title=_("Yêu cầu hủy phép bị từ chối"),
            message=_("Yêu cầu hủy phép {0} của bạn đã bị từ chối.").format(name),
            reference_doctype="VN Leave Cancellation Request",
            reference_name=name,
        )
    except Exception:
        pass

    return {
        "name": name,
        "status": "Rejected",
        "message": _("Đã từ chối yêu cầu hủy {0}.").format(name),
    }


@frappe.whitelist()
def bulk_approve_cancellations(names=None) -> dict:
    """Approve many cancellation requests in one HR action.

    ``names`` may be a JSON array, a comma-separated string, or a list. Each
    request is approved independently (cancel the linked leave, stamp
    workflow, notify, audit): a single failure (wrong state / locked) is
    recorded in ``failed`` and does not abort the run, so HR can clear the
    whole queue at once and review any leftovers afterwards. Returns a summary
    ``{total, succeeded, failed, counts}`` (see ``merge_bulk_results``).
    """
    _require_manager()
    name_list = leave_utils.normalize_name_list(names)
    succeeded: list[str] = []
    failed: list[dict] = []
    for name in name_list:
        try:
            res = _approve_cancellation_one(name)
            succeeded.append(res.get("name") or name)
        except Exception as exc:
            frappe.log_error(title=f"leave.bulk_approve_cancellation {name} failed")
            failed.append({"name": name, "error": str(exc)})
    return leave_utils.merge_bulk_results(succeeded, failed)


@frappe.whitelist()
def bulk_reject_cancellations(names=None, rejection_reason: str | None = None) -> dict:
    """Reject many cancellation requests in one HR action.

    ``rejection_reason`` is stamped on every rejected request (and surfaced in
    the employee notification). Like the bulk-approve, one failure does not
    abort the rest. Returns ``{total, succeeded, failed, counts}``.
    """
    _require_manager()
    name_list = leave_utils.normalize_name_list(names)
    succeeded: list[str] = []
    failed: list[dict] = []
    for name in name_list:
        try:
            res = _reject_cancellation_one(name, rejection_reason)
            succeeded.append(res.get("name") or name)
        except Exception as exc:
            frappe.log_error(title=f"leave.bulk_reject_cancellation {name} failed")
            failed.append({"name": name, "error": str(exc)})
    return leave_utils.merge_bulk_results(succeeded, failed)


# --------------------------------------------------------------------------- #
# HR leave-approval queue (plan §11.5 / doctype-design A.6)
# --------------------------------------------------------------------------- #
# Frappe HR routes Leave Application approval through the standard
# "Open → Approved/Rejected" status flow. These endpoints surface that flow in
# the SPA so HR can review and act on pending leaves (especially those stamped
# ``vn_requires_blackout_approval``) without leaving the portal. They gate on
# HR Manager / System Manager (``_require_manager``) exactly like the
# cancellation-request inbox.
@frappe.whitelist()
def pending_leave_approvals(
    blackout_only: int | bool | str = 0,
    employee: str | None = None,
    department: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    """Open (awaiting-approval) Leave Applications for the HR inbox.

    Returns rows shaped like ``my_applications`` (plus the blackout custom
    fields when migrated). ``blackout_only`` narrows the list to applications
    that overlapped a Block / Require-HR-Approval blackout window so HR can
    triage the high-attention requests first. ``employee`` / ``department``
    narrow the inbox so HR can triage by person or team when the queue is long.
    """
    _require_manager()

    filters: dict = {"status": "Open"}
    if employee:
        filters["employee"] = employee
    if department:
        filters["department"] = department
    if from_date or to_date:
        filters["from_date"] = ["between", [from_date or to_date, to_date or from_date]]

    try:
        rows = frappe.db.get_all(
            "Leave Application",
            filters=filters,
            fields=_application_fields(),
            order_by="posting_date desc, creation desc",
            limit_page_length=500,
        )
    except Exception:
        frappe.log_error(title="leave.pending_leave_approvals list failed")
        return []

    if leave_utils._truthy(blackout_only):
        rows = [r for r in rows if leave_utils.is_blackout_flagged(r)]
    return leave_utils.sort_pending_approvals(rows)


@frappe.whitelist()
def approve_leave_application(name: str | None = None) -> dict:
    """Approve an Open Leave Application (HR/Manager only).

    Sets ``status="Approved"`` + stamps ``leave_approver`` with the session
    user; best-effort submit when the document is still a Draft so the leave
    ledger updates. Idempotent (already Approved → no-op). Audit + notify the
    employee so the bell reflects the outcome.
    """
    _require_manager()
    if not name:
        frappe.throw(_("Thiếu mã đơn nghỉ phép."))
    return _approve_one(name)


def _approve_one(name: str) -> dict:
    """Per-document approve (assumes the manager gate already passed). Raises
    on invalid state so a caller (single endpoint / bulk loop) can decide how
    to surface the failure. Shared with ``bulk_approve_leave_applications``."""
    doc = frappe.get_doc("Leave Application", name)
    if doc.status == "Approved":
        return {
            "name": name,
            "status": "Approved",
            "message": _("Đơn nghỉ phép {0} đã được duyệt.").format(name),
        }
    if doc.status not in ("Open", "Draft"):
        frappe.throw(
            _("Đơn nghỉ đang ở trạng thái {0} — không thể duyệt.").format(doc.status),
            frappe.PermissionError,
        )

    doc.status = "Approved"
    try:
        # The approver is the session user; ``leave.apply`` already designated +
        # HRMS-shared the doc with the approver at creation, so the submit's
        # permission check passes (proper Frappe — no ignore_permissions).
        doc.leave_approver = frappe.session.user
    except Exception:
        pass
    _save_or_submit(doc)

    _after_leave_decision(doc, approved=True)
    # plan-handover-deskfree P1 (H9): a policy with require_handover=1 mints a
    # Pending handover task for the approved leave (best-effort, never fails
    # the approval itself).
    try:
        from gege_hr.gege_hr.api.handover import _maybe_mint_handover

        _maybe_mint_handover(doc)
    except Exception:
        frappe.log_error(title="handover._maybe_mint_handover wiring failed")
    return {
        "name": name,
        "status": "Approved",
        "message": _("Đã duyệt đơn nghỉ phép {0}.").format(name),
    }


@frappe.whitelist()
def reject_leave_application(name: str | None = None, rejection_reason: str | None = None) -> dict:
    """Reject an Open Leave Application (HR/Manager only).

    Sets ``status="Rejected"`` and stamps the reason into the document's
    description when a reason is supplied (Leave Application has no dedicated
    rejection-reason field in standard Frappe HR). Idempotent + audit + notify.
    """
    _require_manager()
    if not name:
        frappe.throw(_("Thiếu mã đơn nghỉ phép."))
    return _reject_one(name, rejection_reason)


def _reject_one(name: str, rejection_reason: str | None = None) -> dict:
    """Per-document reject (assumes the manager gate already passed). Shared
    with ``bulk_reject_leave_applications``."""
    doc = frappe.get_doc("Leave Application", name)
    if doc.status == "Rejected":
        return {
            "name": name,
            "status": "Rejected",
            "message": _("Đơn nghỉ phép {0} đã bị từ chối.").format(name),
        }
    if doc.status not in ("Open", "Draft"):
        frappe.throw(
            _("Đơn nghỉ đang ở trạng thái {0} — không thể từ chối.").format(doc.status),
            frappe.PermissionError,
        )

    doc.status = "Rejected"
    reason = (rejection_reason or "").strip()
    if reason:
        try:
            # Append the reason to the description so HR/employee can see why;
            # standard Frappe HR has no separate rejection-reason field.
            existing = (doc.description or "").strip()
            doc.description = f"{existing}\n\n[Lý do từ chối] {reason}" if existing else reason
        except Exception:
            pass
    _save_or_submit(doc)

    _after_leave_decision(doc, approved=False, reason=reason)
    return {
        "name": name,
        "status": "Rejected",
        "message": _("Đã từ chối đơn nghỉ phép {0}.").format(name),
    }


@frappe.whitelist()
def bulk_approve_leave_applications(names=None) -> dict:
    """Approve many Open Leave Applications in one HR action.

    ``names`` may be a JSON array, a comma-separated string, or a list. Each
    document is approved independently: a single failure (wrong state / locked)
    is recorded in ``failed`` and does not abort the run, so HR can act on the
    whole queue at once and review any leftovers afterwards. Returns a summary
    ``{total, succeeded, failed, counts}`` (see ``merge_bulk_results``).
    """
    _require_manager()
    name_list = leave_utils.normalize_name_list(names)
    succeeded: list[str] = []
    failed: list[dict] = []
    for name in name_list:
        try:
            res = _approve_one(name)
            succeeded.append(res.get("name") or name)
        except Exception as exc:
            frappe.log_error(title=f"leave.bulk_approve {name} failed")
            failed.append({"name": name, "error": str(exc)})
    return leave_utils.merge_bulk_results(succeeded, failed)


@frappe.whitelist()
def bulk_reject_leave_applications(names=None, rejection_reason: str | None = None) -> dict:
    """Reject many Open Leave Applications in one HR action.

    ``rejection_reason`` is stamped on every rejected document (and surfaced in
    the employee notification + audit). Like the bulk-approve, one failure does
    not abort the rest. Returns ``{total, succeeded, failed, counts}``.
    """
    _require_manager()
    name_list = leave_utils.normalize_name_list(names)
    succeeded: list[str] = []
    failed: list[dict] = []
    for name in name_list:
        try:
            res = _reject_one(name, rejection_reason)
            succeeded.append(res.get("name") or name)
        except Exception as exc:
            frappe.log_error(title=f"leave.bulk_reject {name} failed")
            failed.append({"name": name, "error": str(exc)})
    return leave_utils.merge_bulk_results(succeeded, failed)


@frappe.whitelist()
def leave_approval_options() -> dict:
    """Distinct employees + departments present in the current Open leave
    queue, so the SPA can populate the inbox filters without a second query
    of its own. HR/Manager only; best-effort (swallow → empty options)."""
    _require_manager()
    try:
        rows = frappe.db.get_all(
            "Leave Application",
            filters={"status": "Open"},
            fields=["employee", "employee_name", "department"],
            limit_page_length=500,
        )
    except Exception:
        frappe.log_error(title="leave.leave_approval_options failed")
        return {"employees": [], "departments": []}

    employees: list[dict] = []
    seen_emp: set[str] = set()
    departments: list[dict] = []
    seen_dep: set[str] = set()
    for r in rows:
        emp = (r.get("employee") or "").strip()
        if emp and emp not in seen_emp:
            seen_emp.add(emp)
            employees.append({"value": emp, "label": r.get("employee_name") or emp})
        dep = (r.get("department") or "").strip()
        if dep and dep not in seen_dep:
            seen_dep.add(dep)
            departments.append({"value": dep, "label": dep})
    employees.sort(key=lambda x: x["label"].lower())
    departments.sort(key=lambda x: x["label"].lower())
    return {"employees": employees, "departments": departments}


@frappe.whitelist()
def cancellation_options() -> dict:
    """Distinct employees + departments present in the current non-terminal
    cancellation queue, so the SPA can populate the inbox filters without a
    second query of its own.

    Department is resolved per employee in one batch — the cancellation DocType
    has no ``department`` column (unlike Leave Application). HR/Manager only;
    best-effort (swallow → empty options) so the inbox still renders on an
    older bench.
    """
    _require_manager()
    try:
        rows = frappe.db.get_all(
            "VN Leave Cancellation Request",
            filters={"workflow_state": ["in", ["Draft", "Pending Manager", "Pending HR"]]},
            fields=["employee", "employee_name"],
            limit_page_length=500,
        )
    except Exception:
        frappe.log_error(title="leave.cancellation_options failed")
        return {"employees": [], "departments": []}

    employees: list[dict] = []
    seen_emp: set[str] = set()
    departments: list[dict] = []
    seen_dep: set[str] = set()
    dept_by_emp = _departments_by_employee([r.get("employee") for r in rows if r.get("employee")])
    for r in rows:
        emp = (r.get("employee") or "").strip()
        if emp and emp not in seen_emp:
            seen_emp.add(emp)
            employees.append({"value": emp, "label": r.get("employee_name") or emp})
        dep = (dept_by_emp.get(emp) or "").strip()
        if dep and dep not in seen_dep:
            seen_dep.add(dep)
            departments.append({"value": dep, "label": dep})
    employees.sort(key=lambda x: x["label"].lower())
    departments.sort(key=lambda x: x["label"].lower())
    return {"employees": employees, "departments": departments}


def _save_or_submit(doc) -> None:
    """Persist a leave decision through the **proper Frappe flow** (no bypass).

    * ``docstatus=0`` (Draft) → ``doc.submit()`` — runs ``validate`` + ``on_submit``:
      HRMS creates the **Leave Ledger Entry** (balance deducted) AND fires the
      hooks that write the audit trail (Activity/VN Audit Event). ``submit`` also
      derives ``status='Approved'`` correctly.
    * ``docstatus>=1`` (already submitted) → ``doc.save()`` — runs ``validate``.

    We deliberately do NOT use ``ignore_permissions`` or raw ``db.set_value``:
    those skip ``validate``/``on_submit`` so the ledger + audit logs would never
    be written, and the action would not be recorded against a properly
    permission-checked approver. If a 403 occurs the correct fix is to grant the
    approver's role the write/submit permission on Leave Application (Role
    Permissions Manager) — that keeps the permission audit trail intact. Any
    failure is re-raised so the endpoint surfaces the real error.

    Concurrency: two approvers clicking at the same moment both read the OLD
    balance (the other's ledger entry not yet committed) and both submit →
    balance goes negative. Serialize per-employee by taking a row lock on the
    employee's Leave Allocation rows (SELECT ... FOR UPDATE) for the duration
    of the submit: the second request blocks until the first commits, then
    HRMS validate() sees the fresh balance and throws the insufficient-balance
    error instead of letting both through.
    """
    with _employee_leave_lock(doc.employee):
        if getattr(doc, "docstatus", 0) == 0:
            # reload() re-reads DB truth — which reverts the in-memory decision
            # (status back to Open) that HRMS forbids submitting. Preserve the
            # decision fields across the reload, then submit.
            wanted_status = getattr(doc, "status", None)
            wanted_description = getattr(doc, "description", None)
            wanted_approver = getattr(doc, "leave_approver", None)
            doc.reload()
            if getattr(doc, "docstatus", 0) != 0:
                frappe.throw(
                    _("Đơn nghỉ đã được duyệt bởi người khác — không duyệt 2 lần."),
                    frappe.ValidationError,
                )
            if wanted_status:
                doc.status = wanted_status
            if wanted_description is not None:
                doc.description = wanted_description
            if wanted_approver:
                doc.leave_approver = wanted_approver
            # HRMS guards ``status`` at permlevel 1 (approver-only field).
            # validate_higher_perm_levels() silently reverts a permlevel-1
            # change back to the DB value for users whose grants live in the
            # gege Custom DocPerm matrix (permlevel 0) — so our Approved /
            # Rejected decision would be wiped before on_submit, which then
            # throws ("Only ... 'Approved' and 'Rejected' can be submitted").
            # This endpoint already runs the server-side HR-manager gate, so
            # elevate ONLY this save: every validate, the Leave Ledger Entry
            # and the audit hooks still execute (Frappe's own approve flow
            # uses the same flag).
            _flags = getattr(doc, "flags", None)  # bench-free stub docs may omit flags
            if _flags is not None:
                _flags.ignore_permissions = True
            try:
                doc.submit()
            finally:
                if _flags is not None:
                    _flags.ignore_permissions = False
            return
        doc.save()


@contextmanager
def _employee_leave_lock(employee: str):
    """Row-lock the employee's leave allocations (FOR UPDATE) inside the open
    transaction. Locks are held until COMMIT/ROLLBACK — i.e. until the request
    finishes — so concurrent approve/submit for the SAME employee serialize
    here, while different employees never block each other. Uses the shared
    db.sql transaction of the request (no autocommit)."""
    if employee:
        frappe.db.sql(
            "SELECT name FROM `tabLeave Allocation`"
            " WHERE employee = %(employee)s AND docstatus < 2"
            " FOR UPDATE",
            {"employee": employee},
        )
    yield


def _after_leave_decision(doc, *, approved: bool, reason: str = "") -> None:
    """Best-effort audit + notify after an HR leave approve/reject (never
    aborts the decision — mirrors ``notify.push_notification`` swallow pattern)."""
    try:
        notify.push_notification(
            employee=doc.employee,
            notification_type="Leave",
            title=(_("Đơn nghỉ phép đã được duyệt") if approved else _("Đơn nghỉ phép bị từ chối")),
            message=(
                _("Đơn nghỉ {0} của bạn đã được duyệt.").format(doc.name)
                if approved
                else (
                    _("Đơn nghỉ {0} bị từ chối{1}").format(
                        doc.name,
                        f": {reason}" if reason else "",
                    )
                )
            ),
            reference_doctype="Leave Application",
            reference_name=doc.name,
        )
    except Exception:
        pass

    try:
        audit_api.log(
            "Leave Approve" if approved else "Leave Reject",
            company=getattr(doc, "company", None),
            employee=doc.employee,
            work_date=getattr(doc, "from_date", None),
            reference_doctype="Leave Application",
            reference_name=doc.name,
            description=(
                "HR duyệt đơn nghỉ phép"
                if approved
                else f"HR từ chối đơn nghỉ phép{(': ' + reason) if reason else ''}"
            ),
        )
    except Exception:
        pass

    # Reject (status flip on a docstatus-0 draft) fires no doc_events hook —
    # refresh the calendar cache for both approve and reject so the chip
    # recolors immediately (approve is also covered by on_leave_submit).
    _touch_calendar(doc)


# --------------------------------------------------------------------------- #
# Cancellation-request internals
# --------------------------------------------------------------------------- #
def _require_manager() -> None:
    if not _is_manager():
        frappe.throw(
            _("Chỉ HR/Quản lý mới thực hiện được thao tác này."),
            frappe.PermissionError,
        )


def _list_cancellation_requests(filters: dict) -> list[dict]:
    # The cancellation DocType has no ``department`` column — drop it from the
    # SELECT (it would raise ``Unknown column``) and resolve it per employee in
    # one batch afterwards, so HR can see each requester's team on the inbox row.
    select_fields = [f for f in leave_utils._CANCELLATION_ROW_FIELDS if f != "department"]
    try:
        rows = frappe.db.get_all(
            "VN Leave Cancellation Request",
            filters=filters,
            fields=select_fields,
            order_by="requested_at desc",
            limit=500,
        )
    except Exception:
        frappe.log_error(title="VN Leave Cancellation Request list failed")
        return []

    dept_by_emp = _departments_by_employee([r.get("employee") for r in rows if r.get("employee")])
    out: list[dict] = []
    for r in rows:
        emp = (r.get("employee") or "").strip() if isinstance(r, dict) else ""
        if emp:
            r["department"] = dept_by_emp.get(emp) or ""
        out.append(leave_utils.cancellation_row(r))
    return out


def _employees_in_department(department: str) -> list[str]:
    """Names of the employees in ``department`` (best-effort). The cancellation
    DocType has no ``department`` column, so the inbox's department filter is
    resolved into a roster here and applied as an ``employee IN (...)`` clause."""
    try:
        rows = frappe.db.get_all(
            "Employee",
            filters={"department": department},
            fields=["name"],
        )
    except Exception:
        frappe.log_error(title="leave._employees_in_department failed")
        return []
    return [r["name"] for r in rows if r.get("name")]


def _departments_by_employee(employee_names: list) -> dict:
    """Map ``employee → department`` for a batch of employee names (one query).
    Best-effort: any failure returns an empty map so the caller renders no
    department options rather than crashing the inbox."""
    names = [n for n in (employee_names or []) if n]
    if not names:
        return {}
    try:
        rows = frappe.db.get_all(
            "Employee",
            filters={"name": ["in", names]},
            fields=["name", "department"],
        )
    except Exception:
        frappe.log_error(title="leave._departments_by_employee failed")
        return {}
    return {r["name"]: (r.get("department") or "") for r in rows}


def _open_cancellation_request(leave_application: str) -> str | None:
    """Name of a still-open (non-terminal) cancellation request for a leave."""
    try:
        return frappe.db.get_value(
            "VN Leave Cancellation Request",
            {
                "leave_application": leave_application,
                "workflow_state": ["in", ["Draft", "Pending Manager", "Pending HR"]],
            },
            "name",
        )
    except Exception:
        return None


def _stamp_cancellation_link(leave_application: str, cancellation_request: str, reason: str | None) -> None:
    """Back-link ``vn_cancellation_request`` + flag on the Leave Application."""
    try:
        meta = frappe.get_meta("Leave Application")
        if not meta or not meta.has_field("vn_cancellation_requested"):
            return
    except Exception:
        return
    try:
        updates = {"vn_cancellation_requested": 1}
        if meta.has_field("vn_cancellation_request"):
            updates["vn_cancellation_request"] = cancellation_request
        if reason and meta.has_field("vn_cancellation_reason"):
            updates["vn_cancellation_reason"] = reason
        frappe.db.set_value("Leave Application", leave_application, updates)
    except Exception:
        frappe.log_error(title="Leave cancellation back-link failed")


def _cancel_linked_leave(leave_application: str) -> bool:
    """Cancel an Approved Leave Application (restoring leave balance)."""
    try:
        leave = frappe.get_doc("Leave Application", leave_application)
    except Exception:
        frappe.log_error(title="Leave Application load for cancellation failed")
        return False
    try:
        if leave.docstatus == 1:
            leave.cancel()
            return True
        # Already cancelled / draft — nothing to cancel.
        return leave.docstatus == 2
    except Exception:
        frappe.log_error(title="Leave Application cancel failed")
        return False


def _has_vn_cancellation_field() -> bool:
    """Whether the ``vn_cancellation_requested`` custom field exists yet."""
    try:
        meta = frappe.get_meta("Leave Application")
        return meta and meta.has_field("vn_cancellation_requested")
    except Exception:
        return False


def _application_fields() -> list[str]:
    """Base Leave Application list fields, plus the blackout custom fields when
    the migrated ``vn_`` fields exist on this site (so an old bench without the
    fields does not raise ``Unknown column``)."""
    fields = [
        "name",
        "employee",
        "employee_name",
        "leave_type",
        "from_date",
        "to_date",
        "total_leave_days",
        "half_day",
        "half_day_date",
        "status",
        "posting_date",
        "description",
        "department",
        "docstatus",
    ]
    try:
        meta = frappe.get_meta("Leave Application")
        for extra in ("vn_requires_blackout_approval", "vn_blackout_decision"):
            if meta and meta.has_field(extra):
                fields.append(extra)
    except Exception:
        pass
    return fields


def _stamp_blackout_decision(doc, leave_type, from_date, to_date, employee) -> None:
    """Best-effort: stamp ``vn_requires_blackout_approval`` / ``vn_blackout_decision``
    on a freshly created Leave Application when its date window overlaps an
    active blackout rule that blocks or requires HR approval. Persisted so HR
    reviewing the document knows it overlapped a restriction window; never aborts
    the apply flow (preview is the real enforcement point)."""
    try:
        from gege_hr.gege_hr.api import leave_blackout as blackout_api
        from gege_hr.gege_hr.utils import leave_blackout as blackout_utils

        decision = blackout_api.evaluate_leave_blackout(
            from_date=str(getdate(from_date)),
            to_date=str(getdate(to_date)),
            leave_type=leave_type,
            employee=employee,
        )
        stamp = blackout_utils.blackout_decision_fields(decision)
        if not stamp:
            return
        meta = frappe.get_meta("Leave Application")
        for key, value in stamp.items():
            if meta and meta.has_field(key):
                doc.db_set(key, value)
                doc.set(key, value)
    except Exception:
        frappe.log_error(title="leave.apply blackout stamp failed")


# --------------------------------------------------------------------------- #
# Hooks — desk-free leave calendar cache refresh (plan leave-calendar C3)
# --------------------------------------------------------------------------- #
def _touch_calendar(doc) -> None:
    """Best-effort calendar cache refresh + realtime ping for a leave doc/row
    carrying ``employee`` / ``from_date`` / ``to_date`` (never raises)."""
    try:
        from gege_hr.gege_hr.api.leave_calendar import touch_leave_calendar

        touch_leave_calendar(doc)
    except Exception:
        frappe.log_error(title="leave._touch_calendar failed")


def _touch_calendar_by_name(name: str | None) -> None:
    """``_touch_calendar`` for a Leave Application name (one light lookup)."""
    if not name:
        return
    try:
        row = frappe.db.get_value(
            "Leave Application", name, ["employee", "from_date", "to_date"], as_dict=True
        )
        if row:
            _touch_calendar(row)
    except Exception:
        frappe.log_error(title="leave._touch_calendar_by_name failed")


def on_leave_submit(doc, method: str | None = None) -> None:
    """Leave Application on_submit → refresh leave calendar cache + notify."""
    _touch_calendar(doc)


def on_leave_cancel(doc, method: str | None = None) -> None:
    """Leave Application on_cancel → refresh leave calendar cache."""
    _touch_calendar(doc)
