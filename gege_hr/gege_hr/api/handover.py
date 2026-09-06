"""Leave handover task API — plan v5 §11.5 / doctype-design §32 (Post-MVP)
+ plan-handover-deskfree-complete (desk-free parity, 2026-09).

CRUD + lifecycle for **VN Leave Handover Task**. The SPA contract maps to:

  * ``my_handovers``   → tasks where the caller is ``to_employee`` (or HR: all)
  * ``leave_handovers``→ manager list filtered by leave application / employee
  * ``create_handover``→ mint a Pending task (validates the Leave Application)
  * ``update_handover_status`` → drive Pending → In Progress → Completed
    (Completed **submits** — docstatus 1; cancelling a submitted doc cancels)
  * ``get_handover``   → detail + attachments + leave meta + ``can`` matrix
  * ``update_handover``→ edit a non-terminal draft (race-guarded)
  * ``delete_handover``→ delete a draft (scoped bypass + audit)
  * ``handover_leave_options`` → approved Leave Applications for the picker

Pure lifecycle math lives in ``utils/handover.py`` (bench-free). Bench loaders
here are guarded so a missing table degrades gracefully.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import now

from gege_hr.gege_hr.api import audit as audit_api
from gege_hr.gege_hr.utils import employee as emp_utils, handover as handover_utils, notify, pagination

DOCTYPE = "VN Leave Handover Task"
_LIST_FIELDS = [
    "name",
    "leave_application",
    "from_employee",
    "to_employee",
    "handover_date",
    "status",
    "description",
    "attachment",
    "completed_at",
    "completed_by",
    "note",
    "docstatus",
    "owner",
    "modified",
]


# --------------------------------------------------------------------------- #
# Permission helpers
# --------------------------------------------------------------------------- #
def _is_manager() -> bool:
    roles = set(emp_utils.get_user_roles() or [])
    return bool(roles & emp_utils.HR_MANAGER_ROLES) or bool(roles & emp_utils.HR_USER_ROLES)


def _require_manager() -> None:
    if not _is_manager():
        frappe.throw(_("Chỉ HR User / HR Manager mới được thực hiện."), frappe.PermissionError)


def _current_employee() -> str | None:
    return emp_utils.get_employee_for_user()


def _table_ready() -> bool:
    try:
        return bool(frappe.db.table_exists(DOCTYPE))
    except Exception:
        return False


def _docstatus(doc) -> int:
    """Safe docstatus read (stubs / legacy rows may not carry the attr)."""
    return getattr(doc, "docstatus", 0) or 0


def _doc_snapshot(doc) -> dict:
    """Best-effort ``as_dict()`` for audit logging (stub-safe)."""
    as_dict = getattr(doc, "as_dict", None)
    if callable(as_dict):
        try:
            return dict(as_dict())
        except Exception:
            pass
    return {"name": getattr(doc, "name", None), "status": getattr(doc, "status", None)}


def _publish_handover() -> None:
    """Best-effort realtime ping so an open list tab can offer a refresh
    (plan-handover-deskfree H8 — parity ``leave_calendar_updated``)."""
    pub = getattr(frappe, "publish_realtime", None)
    if pub is None:
        return
    try:
        pub("handover_updated", {"doctype": DOCTYPE})
    except Exception:
        log = getattr(frappe, "log_error", None)
        if callable(log):
            try:
                log(title="handover.publish_realtime failed")
            except Exception:
                pass


def _attach_employee_names(rows: list[dict]) -> list[dict]:
    """Enrich rows with ``from_employee_name`` / ``to_employee_name`` (one
    batched Employee lookup per page; degrades silently)."""
    ids = sorted({r.get(key) for r in rows for key in ("from_employee", "to_employee") if r.get(key)})
    names: dict = {}
    if ids:
        try:
            for row in frappe.db.get_all(
                "Employee", filters={"name": ("in", ids)}, fields=["name", "employee_name"]
            ):
                names[str(row.get("name"))] = row.get("employee_name") or ""
        except Exception:
            frappe.log_error(title="handover._attach_employee_names failed")
    for r in rows:
        r["from_employee_name"] = names.get(r.get("from_employee") or "", "") or None
        r["to_employee_name"] = names.get(r.get("to_employee") or "", "") or None
    return rows


def _assert_active_employee(employee: str) -> None:
    """The receiver must be an active Employee (plan §3.2)."""
    try:
        status = frappe.db.get_value("Employee", employee, "status")
    except Exception:
        status = None
    if status != "Active":
        frappe.throw(
            _("Người nhận không hợp lệ hoặc không còn làm việc."),
            frappe.ValidationError,
        )


def _validate_leave_link(leave_application: str | None, from_employee: str | None) -> None:
    """A handover must reference an approved Leave Application owned by the
    departing employee (plan §3.5 — G3; HR may link any employee's leave)."""
    if not leave_application:
        return
    try:
        la = frappe.db.get_value(
            "Leave Application",
            str(leave_application).strip(),
            ["docstatus", "status", "employee"],
            as_dict=True,
        )
    except Exception:
        la = None
    if la is None:
        frappe.throw(_("Đơn nghỉ phép không tồn tại."), frappe.ValidationError)
    if (la.get("docstatus") or 0) != 1:
        frappe.throw(
            _("Đơn nghỉ phép chưa được duyệt — không thể tạo bàn giao."),
            frappe.ValidationError,
        )
    if not _is_manager() and from_employee and la.get("employee") != from_employee:
        frappe.throw(
            _("Đơn nghỉ phép này không phải của người bàn giao."),
            frappe.ValidationError,
        )


# --------------------------------------------------------------------------- #
# Read endpoints
# --------------------------------------------------------------------------- #
def _match_handover_row(row: dict, search: str | None) -> bool:
    """Generic server-side free-text match across a handover row's values
    (DNA §6.6 D, HR-BL-09)."""
    q = (search or "").strip().lower()
    if not q:
        return True
    return any(q in str(v).lower() for v in row.values() if v is not None)


@frappe.whitelist()
def my_handovers(
    status: str | None = None,
    search: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    page: int = 1,
    page_size: int = 0,
) -> list[dict] | dict:
    """Tasks handed *to* the caller (the receiver must action them).

    ``search`` OR-matches a free-text query across the normalised row's values,
    applied server-side (DNA §6.6 D, HR-BL-09) — ready for the SPA broad search.

    Pagination is **opt-in** (DNA §6.6 A): pass ``page`` + a positive
    ``page_size`` to receive ``{"data": [...], "total": int, "summary": None}``
    (post-query filter → ``total`` is the filtered list length); without
    ``page_size`` the legacy bare-list return is preserved.
    """
    emp = _current_employee()
    if not emp or not _table_ready():
        return {"data": [], "total": 0, "summary": None} if page_size else []
    # List filters (DNA §6.6 B): a dict cannot hold two conditions on the same
    # field, so the handover_date range is expressed as separate >= / <= entries.
    filters: list = [["to_employee", "=", emp]]
    if status:
        filters.append(["status", "=", status])
    if date_from:
        filters.append(["handover_date", ">=", date_from])
    if date_to:
        filters.append(["handover_date", "<=", date_to])
    try:
        rows = frappe.db.get_all(DOCTYPE, filters=filters, fields=_LIST_FIELDS, order_by="handover_date desc")
    except Exception:
        frappe.log_error(title="handover.my_handovers failed")
        return {"data": [], "total": 0, "summary": None} if page_size else []
    out = _attach_employee_names([handover_utils.handover_row(r) for r in rows])
    return pagination.paginate_filtered(
        [r for r in out if _match_handover_row(r, search)], page=page, page_size=page_size
    )


@frappe.whitelist()
def leave_handovers(
    leave_application: str | None = None,
    employee: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 0,
) -> list[dict] | dict:
    """Manager list. HR sees all; otherwise scoped to the caller's involvement.

    ``search`` OR-matches a free-text query across the normalised row's values,
    applied server-side (DNA §6.6 D) — the same broad-search contract as
    ``my_handovers``.

    ``employee`` matches rows where the employee is **either** side of the
    handover (G1 fix — the old filter targeted a non-existent ``employee``
    column and silently swallowed the SQL error). Pagination is **opt-in**
    (DNA §6.6 A): pass ``page`` + a positive ``page_size`` to receive
    ``{"data": [...], "total": int, "summary": {"pending": int,
    "in_progress": int}}``; the legacy bare-list return is preserved otherwise.
    """
    if not _table_ready():
        return {"data": [], "total": 0, "summary": None} if page_size else []
    # List filters (DNA §6.6 B): handover_date range as separate >= / <= entries.
    filters: list = []
    or_filters: list = []
    if leave_application:
        filters.append(["leave_application", "=", leave_application])
    if status:
        filters.append(["status", "=", status])
    if employee:
        or_filters = [["from_employee", "=", employee], ["to_employee", "=", employee]]
    if date_from:
        filters.append(["handover_date", ">=", date_from])
    if date_to:
        filters.append(["handover_date", "<=", date_to])
    try:
        rows = frappe.db.get_all(
            DOCTYPE,
            filters=filters,
            or_filters=or_filters,
            fields=_LIST_FIELDS,
            order_by="handover_date desc",
        )
    except Exception:
        frappe.log_error(title="handover.leave_handovers failed")
        return {"data": [], "total": 0, "summary": None} if page_size else []
    # Broad search (DNA §6.6 D) over the normalised row's values, then scope.
    out = [r for r in (handover_utils.handover_row(r) for r in rows) if _match_handover_row(r, search)]
    if not _is_manager():
        # A non-HR user may only see rows where they are from or to.
        emp = _current_employee()
        if not emp:
            out = []
        else:
            out = [r for r in out if r.get("from_employee") == emp or r.get("to_employee") == emp]
    out = _attach_employee_names(out)
    if not page_size:
        return out
    summary = {
        "pending": sum(1 for r in out if r.get("status") == "Pending"),
        "in_progress": sum(1 for r in out if r.get("status") == "In Progress"),
    }
    return pagination.paginate_filtered(out, page=page, page_size=page_size, summary=summary)


@frappe.whitelist()
def suggest_handover_receivers(employee: str | None = None) -> list[dict]:
    """Suggest likely receivers for a handover (auto-suggest in the create form).

    Resolves the departing employee's ``reports_to`` / ``department`` / ``company``,
    then pulls active colleagues (same department + their direct reports + the
    manager) and ranks them via ``utils.handover.suggest_receivers``. The pool
    is **company-scoped** to the departing employee so that, in a multi-company
    tenant, an HR user can only receive suggestions from their own company's
    headcount. HR may pass any ``employee``; a plain employee resolves to
    themselves. Best-effort: any failure (missing Employee record / no table)
    degrades to ``[]`` so the create form never breaks.
    """
    if not _table_ready():
        return []
    emp = employee or _current_employee()
    if not emp:
        return []
    try:
        meta = (
            frappe.db.get_value(
                "Employee",
                emp,
                ["reports_to", "department", "company"],
                as_dict=True,
            )
            or {}
        )
    except Exception:
        frappe.log_error(title="handover.suggest_handover_receivers meta failed")
        return []
    reports_to = meta.get("reports_to")
    department = meta.get("department")
    company = (meta.get("company") or "").strip() or None
    try:
        # Pool: the manager (if any), direct reports of this employee, and
        # same-department peers. We fetch a flat list and let the pure helper
        # rank + de-dupe + exclude self. When we know the departing employee's
        # company, scope both queries to it so multi-company tenants never leak
        # headcount across company boundaries. A blank company (older bench /
        # Employee without company) falls back to the unscoped behaviour.
        colleague_rows: list[dict] = []
        clauses: list[list] = [["status", "=", "Active"]]
        if company:
            clauses.append(["company", "=", company])
        if department:
            clauses.append(["department", "=", department])
        rows = frappe.db.get_list(
            "Employee",
            filters=clauses,
            fields=["name", "employee_name", "reports_to", "department"],
            order_by="employee_name asc",
            limit_page_length=80,
        )
        colleague_rows.extend(rows)
        # Direct reports: people whose manager is this employee (same company).
        report_clauses: list[list] = [["status", "=", "Active"], ["reports_to", "=", emp]]
        if company:
            report_clauses.append(["company", "=", company])
        reports = frappe.db.get_list(
            "Employee",
            filters=report_clauses,
            fields=["name", "employee_name", "reports_to", "department"],
            order_by="employee_name asc",
            limit_page_length=40,
        )
        colleague_rows.extend(reports)
    except Exception:
        frappe.log_error(title="handover.suggest_handover_receivers load failed")
        return []
    return handover_utils.suggest_receivers(emp, reports_to, department, colleague_rows)


@frappe.whitelist()
def handover_leave_options(
    from_employee: str | None = None,
    search: str = "",
    limit: int = 20,
) -> list[dict]:
    """Approved Leave Applications usable as the handover anchor (plan §3.6).

    Non-HR callers are pinned to their own applications (G3 — replaces the
    generic ``search_link`` picker in the SPA); HR may pass any
    ``from_employee``. Row shape: ``{name, employee, from_employee_name,
    leave_type, from_date, to_date, total_leave_days, posting_date}``.
    """
    if not _table_ready():
        return []
    is_hr = _is_manager()
    emp = from_employee if (is_hr and from_employee) else _current_employee()
    if not emp:
        return []
    cap = max(1, min(pagination.as_int(limit, 20), 50))
    try:
        rows = frappe.db.get_all(
            "Leave Application",
            filters={"docstatus": 1, "employee": emp},
            fields=[
                "name",
                "employee",
                "leave_type",
                "from_date",
                "to_date",
                "total_leave_days",
                "posting_date",
            ],
            order_by="posting_date desc",
            limit_page_length=cap,
        )
    except Exception:
        frappe.log_error(title="handover.handover_leave_options failed")
        return []
    out = _attach_employee_names(rows)
    q = (search or "").strip().lower()
    if q:
        out = [
            r
            for r in out
            if any(q in str(r.get(key) or "").lower() for key in ("name", "leave_type", "from_employee_name"))
        ]
    return out


@frappe.whitelist()
def get_handover(name: str) -> dict:
    """Detail + attachments + leave meta + server-driven ``can`` matrix
    (plan §3.1 — the SPA never re-derives permissions locally)."""
    _require_table()
    doc = _get_owned_or_managed(name)
    row = handover_utils.handover_row(_doc_snapshot(doc))
    is_hr = _is_manager()
    emp = _current_employee()
    ds = int(row.get("docstatus") or 0)
    status = row.get("status")
    from_me = bool(emp and row.get("from_employee") == emp)
    to_me = bool(emp and row.get("to_employee") == emp)
    editable = ds == 0 and status in ("Pending", "In Progress")

    out: dict = dict(row)
    for key, field in (
        ("from_employee_name", "from_employee"),
        ("to_employee_name", "to_employee"),
    ):
        emp_id = row.get(field)
        try:
            out[key] = frappe.db.get_value("Employee", emp_id, "employee_name") if emp_id else None
        except Exception:
            out[key] = None

    leave = None
    if row.get("leave_application"):
        try:
            leave = frappe.db.get_value(
                "Leave Application",
                row["leave_application"],
                [
                    "name",
                    "employee",
                    "leave_type",
                    "from_date",
                    "to_date",
                    "total_leave_days",
                    "status",
                    "docstatus",
                ],
                as_dict=True,
            )
        except Exception:
            frappe.log_error(title="handover.get_handover leave meta failed")
            leave = None
    out["leave"] = leave

    try:
        out["attachments"] = frappe.db.get_all(
            "File",
            filters={"attached_to_doctype": DOCTYPE, "attached_to_name": name},
            fields=["name", "file_name", "file_url", "is_private", "file_size"],
        )
    except Exception:
        frappe.log_error(title="handover.get_handover attachments failed")
        out["attachments"] = []

    out["can"] = {
        "edit": editable and (is_hr or from_me),
        "delete": ds == 0 and (is_hr or from_me),
        "start": editable and status == "Pending" and (is_hr or to_me),
        "complete": editable and (is_hr or to_me),
        "cancel": (editable and (is_hr or from_me)) or (ds == 1 and is_hr),
        "reopen": ds == 0 and status == "Cancelled" and (is_hr or from_me or to_me),
        "recreate": status == "Cancelled",
        "upload": editable and (is_hr or from_me or to_me),
        "remove_file": editable and (is_hr or from_me or to_me),
    }
    # Activity timeline (plan §3.1 P1 — audit-only, parity leave B3): the VN
    # Audit Event rows referencing this task (Submit/Cancel/Update/Delete).
    try:
        out["activity"] = frappe.db.get_all(
            "VN Audit Event",
            filters={"reference_doctype": DOCTYPE, "reference_name": name},
            fields=["name", "audit_type", "employee", "work_date", "description", "creation"],
            order_by="creation desc",
            limit_page_length=20,
        )
    except Exception:
        frappe.log_error(title="handover.get_handover activity failed")
        out["activity"] = []

    out["is_manager"] = is_hr
    return out


def _maybe_mint_handover(la_doc) -> None:
    """Auto-mint a Pending handover task after a leave is approved (P1 — H9).

    Reads the active VN Leave Policy Extension of the leave type: when
    ``require_handover`` is set and no non-terminal task exists for the leave,
    a task is minted from the leave's ``vn_handover_employee`` receiver (+
    ``vn_handover_note``). When the receiver is missing, the departing
    employee gets a reminder notification instead. Best-effort — a failure
    here must NEVER fail the leave approval.
    """
    if not _table_ready():
        return
    la_name = getattr(la_doc, "name", None)
    employee = getattr(la_doc, "employee", None)
    leave_type = getattr(la_doc, "leave_type", None)
    if not la_name or not employee or not leave_type:
        return
    try:
        require = frappe.db.get_value(
            "VN Leave Policy Extension",
            {"leave_type": leave_type, "is_active": 1},
            "require_handover",
        )
    except Exception:
        frappe.log_error(title="handover._maybe_mint_handover policy lookup failed")
        return
    if not require or int(require or 0) != 1:
        return
    try:
        open_tasks = frappe.db.get_all(
            DOCTYPE,
            filters={"leave_application": la_name, "status": ("not in", ("Completed", "Cancelled"))},
            limit_page_length=1,
        )
    except Exception:
        open_tasks = []
    if open_tasks:
        return
    receiver = getattr(la_doc, "vn_handover_employee", None)
    if not receiver:
        try:
            receiver = frappe.db.get_value("Leave Application", la_name, "vn_handover_employee")
        except Exception:
            receiver = None
    note = getattr(la_doc, "vn_handover_note", None)
    if not receiver:
        # No receiver designated — remind the departing employee to mint one.
        notify.push_notification(
            employee=employee,
            notification_type="Leave",
            title="Cần tạo bàn giao công việc",
            message=(
                f"Đơn nghỉ phép {la_name} của bạn yêu cầu bàn giao — "
                "hãy tạo nhiệm vụ bàn giao cho đồng nghiệp."
            ),
            reference_doctype="Leave Application",
            reference_name=la_name,
        )
        return
    try:
        payload = handover_utils.handover_payload(
            leave_application=la_name,
            from_employee=employee,
            to_employee=receiver,
            handover_date=getattr(la_doc, "from_date", None) or str(now())[:10],
            description=f"Bàn giao tự động cho đơn {la_name}" + (f" — {note}" if note else ""),
            note=note or None,
        )
        doc = frappe.get_doc(payload)
        doc.insert(ignore_permissions=True)
        audit_api.log(
            "Handover Auto Mint",
            doc=_doc_snapshot(doc),
            description=f"tự tạo theo policy cho đơn {la_name}",
        )
        _publish_handover()
        notify.push_notification(
            employee=receiver,
            notification_type="Leave",
            title="Có nhiệm vụ bàn giao mới",
            message=f"Bạn vừa nhận bàn giao công việc từ {employee}.",
            reference_doctype=DOCTYPE,
            reference_name=doc.name,
        )
    except Exception:
        frappe.log_error(title="handover._maybe_mint_handover mint failed")


# --------------------------------------------------------------------------- #
# Write endpoints
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def create_handover(
    leave_application: str,
    from_employee: str,
    to_employee: str,
    handover_date: str,
    description: str,
    attachment: str | None = None,
    note: str | None = None,
    status: str = "Pending",
) -> dict:
    """Mint a handover task (manager or the departing employee)."""
    _require_table()
    if not _is_manager():
        # A non-HR user may only create a handover for themselves.
        emp = _current_employee()
        if emp and from_employee and emp != from_employee:
            frappe.throw(_("Bạn chỉ có thể tạo bàn giao cho chính mình."), frappe.PermissionError)
    try:
        payload = handover_utils.handover_payload(
            leave_application=leave_application,
            from_employee=from_employee,
            to_employee=to_employee,
            handover_date=handover_date,
            description=description,
            attachment=attachment,
            note=note,
            status=status,
        )
    except ValueError as exc:
        frappe.throw(str(exc), frappe.ValidationError)
    # G3: the leave must be approved and belong to the departing employee
    # (HR may link anyone's); the receiver must still be active.
    _validate_leave_link(payload["leave_application"], payload["from_employee"])
    _assert_active_employee(payload["to_employee"])
    doc = frappe.get_doc(payload)
    doc.insert(ignore_permissions=_is_manager())
    _publish_handover()
    # Best-effort notification to the receiver.
    notify.push_notification(
        employee=to_employee,
        notification_type="Leave",
        title="Có nhiệm vụ bàn giao mới",
        message=f"Bạn vừa nhận bàn giao công việc từ {from_employee}.",
        reference_doctype=DOCTYPE,
        reference_name=doc.name,
    )
    return {
        "name": doc.name,
        "status": doc.status,
        "message": _("Đã tạo nhiệm vụ bàn giao."),
    }


@frappe.whitelist()
def update_handover(name: str, **kwargs) -> dict:
    """Edit a non-terminal draft handover (owner = from_employee, or HR).

    Whitelisted fields: ``to_employee / handover_date / description / note``
    (HR may additionally change ``from_employee / leave_application``).
    Race-guarded: reload before applying so a concurrent transition (e.g. the
    receiver completing the task) is detected (plan §3.2).
    """
    _require_table()
    doc = _get_owned_or_managed(name)
    is_hr = _is_manager()
    emp = _current_employee()
    if _docstatus(doc) != 0:
        frappe.throw(
            _("Nhiệm vụ đã hoàn thành (đã khóa) — không sửa được."),
            frappe.ValidationError,
        )
    if doc.status not in ("Pending", "In Progress"):
        frappe.throw(_("Chỉ sửa được nhiệm vụ chưa kết thúc."), frappe.ValidationError)
    if not is_hr and (not emp or doc.from_employee != emp):
        frappe.throw(
            _("Chỉ người bàn giao hoặc HR mới được sửa nhiệm vụ này."),
            frappe.PermissionError,
        )
    allowed = (
        "to_employee",
        "handover_date",
        "description",
        "note",
        "from_employee",
        "leave_application",
    )
    wanted: dict = {k: v for k, v in (kwargs or {}).items() if k in allowed and v is not None}
    if not is_hr:
        # Identity fields are HR-only (plan H3).
        for key in ("from_employee", "leave_application"):
            wanted.pop(key, None)
    if not wanted:
        frappe.throw(_("Không có trường nào hợp lệ để cập nhật."), frappe.ValidationError)
    if "description" in wanted and not str(wanted["description"]).strip():
        frappe.throw(_("Nội dung bàn giao không được để trống."), frappe.ValidationError)
    if "handover_date" in wanted:
        coerced = handover_utils._coerce_date(wanted["handover_date"])
        if not coerced:
            frappe.throw(_("Ngày bàn giao không hợp lệ."), frappe.ValidationError)
        wanted["handover_date"] = coerced
    new_to = wanted.get("to_employee", doc.to_employee)
    new_from = wanted.get("from_employee", doc.from_employee)
    if new_to and new_to == new_from:
        frappe.throw(_("Người nhận phải khác người bàn giao."), frappe.ValidationError)
    if "to_employee" in wanted and new_to != doc.to_employee:
        _assert_active_employee(new_to)
    if "leave_application" in wanted and wanted["leave_application"] != doc.leave_application:
        _validate_leave_link(wanted["leave_application"], new_from)
    # Race-guard (BUG #1 precedent): reload the DB truth, re-check the state,
    # then re-apply the wanted fields on top.
    reload_fn = getattr(doc, "reload", None)
    if callable(reload_fn):
        reload_fn()
    if _docstatus(doc) != 0 or doc.status not in ("Pending", "In Progress"):
        frappe.throw(_("Nhiệm vụ vừa được người khác cập nhật."), frappe.ValidationError)
    for key, value in wanted.items():
        setattr(doc, key, value)
    doc.save(ignore_permissions=is_hr)
    audit_api.log("Handover Update", doc=_doc_snapshot(doc), description=f"đã sửa nhiệm vụ {name}")
    _publish_handover()
    return {"name": doc.name, "status": doc.status, "message": _("Đã cập nhật nhiệm vụ bàn giao.")}


@frappe.whitelist()
def update_handover_status(name: str, status: str, note: str | None = None) -> dict:
    """Drive the handover lifecycle (plan §3.4 — H1).

    * Completing a draft **submits** the doc (docstatus 0 → 1: immutable,
      Version-audited) and stamps ``completed_at/by``.
    * Cancelling a submitted doc runs ``doc.cancel()`` (docstatus 2) — the
      Frappe-standard correction path; HR-only.
    * Cancelling a draft keeps docstatus 0 (draft void) and stays re-openable.
    """
    _require_table()
    doc = _get_owned_or_managed(name)
    current = doc.status
    docstatus = _docstatus(doc)
    if not handover_utils.can_transition(current, status, docstatus=docstatus):
        frappe.throw(
            _("Không thể chuyển từ '{0}' sang '{1}'.").format(current, status),
            frappe.ValidationError,
        )
    # Idempotent no-op on a locked (submitted/cancelled) doc — nothing to save.
    if docstatus != 0 and status == current:
        return {
            "name": doc.name,
            "status": doc.status,
            "docstatus": docstatus,
            "message": _("Đã cập nhật."),
        }
    is_hr = _is_manager()
    # Cancelling a submitted handover is an HR-only correction path (H1).
    if status == "Cancelled" and docstatus == 1 and not is_hr:
        frappe.throw(_("Chỉ HR mới được hủy nhiệm vụ đã hoàn thành."), frappe.PermissionError)
    doc.status = status
    if status == "Completed":
        doc.completed_at = now()
        doc.completed_by = frappe.session.user
    if note and str(note).strip():
        doc.note = str(note).strip()
    audit_action = "Handover Status"
    if status == "Completed" and docstatus == 0:
        # H1: submit → lock. Employee role has no submit perm on the DocType,
        # so the call runs under a scoped bypass (precedent leave._approve_one).
        audit_action = "Handover Submit"
        try:
            frappe.flags.ignore_permissions = True
            doc.submit()
        finally:
            frappe.flags.ignore_permissions = False
    elif status == "Cancelled" and docstatus == 1:
        # Fields are set above — cancel() persists them with the doc.
        audit_action = "Handover Cancel"
        try:
            frappe.flags.ignore_permissions = True
            doc.cancel()
        finally:
            frappe.flags.ignore_permissions = False
    else:
        doc.save(ignore_permissions=is_hr)
    audit_api.log(audit_action, doc=_doc_snapshot(doc), description=f"{name}: {current} → {status}")
    # Notify the original owner when their handover is completed/cancelled.
    if status in ("Completed", "Cancelled") and doc.from_employee:
        notify.push_notification(
            employee=doc.from_employee,
            notification_type="Leave",
            title=f"Bàn giao đã {status.lower()}",
            message=f"Nhiệm vụ bàn giao {name} đã được cập nhật.",
            reference_doctype=DOCTYPE,
            reference_name=name,
        )
    # The receiver must learn a submitted task was pulled back by HR.
    if status == "Cancelled" and docstatus == 1 and doc.to_employee:
        notify.push_notification(
            employee=doc.to_employee,
            notification_type="Leave",
            title="Bàn giao đã bị hủy",
            message=f"Nhiệm vụ bàn giao {name} đã bị HR hủy sau khi hoàn thành.",
            reference_doctype=DOCTYPE,
            reference_name=name,
        )
    _publish_handover()
    return {
        "name": doc.name,
        "status": doc.status,
        "docstatus": _docstatus(doc),
        "message": _("Đã cập nhật."),
    }


@frappe.whitelist()
def delete_handover(name: str) -> dict:
    """Delete a draft handover (docstatus 0, owner or HR — plan §3.3)."""
    _require_table()
    doc = _get_owned_or_managed(name)
    if _docstatus(doc) != 0:
        frappe.throw(_("Chỉ xoá được nhiệm vụ chưa hoàn thành."), frappe.ValidationError)
    snapshot = _doc_snapshot(doc)
    # Employee role has no delete perm on the DocType — scoped bypass AFTER
    # the ownership guard (precedent leave.delete_draft, plan-leave D2).
    try:
        frappe.flags.ignore_permissions = True
        frappe.delete_doc(DOCTYPE, name)
    finally:
        frappe.flags.ignore_permissions = False
    audit_api.log("Handover Delete", doc=snapshot, description=f"đã xoá nhiệm vụ {name}")
    _publish_handover()
    return {"name": name, "message": _("Đã xoá nhiệm vụ bàn giao.")}


# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #
def _require_table() -> None:
    if not _table_ready():
        frappe.throw(_("Tính năng bàn giao chưa được cài đặt."), frappe.ValidationError)


def _get_owned_or_managed(name: str):
    doc = frappe.get_doc(DOCTYPE, name)
    if _is_manager():
        return doc
    emp = _current_employee()
    if emp and (doc.from_employee == emp or doc.to_employee == emp):
        return doc
    frappe.throw(_("Bạn không có quyền trên nhiệm vụ bàn giao này."), frappe.PermissionError)
