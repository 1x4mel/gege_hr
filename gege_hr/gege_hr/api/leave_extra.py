"""NEW-3 (hr-gap-audit 🟥) — Leave Encashment + Compensatory Leave API.

Reuses Frappe HR's ``Leave Encashment`` and ``Compensatory Leave Request``
DocTypes (no duplicate doctype) and exposes a DNA-compliant surface to the
portal: an employee lists/submits leave encashment (nghỉ phép đổi tiền) and
comp-off requests (nghỉ bù); an HR/Manager lists all + approves/rejects. Pure
helpers split out for unit testing. Mirrors ``api/expense.py``.

Desk-free P0 (plan leave-extra-deskfree-complete): detail endpoints with a
server-side can-matrix (``get_leave_encashment`` / ``get_comp_off``), create
context (``leave_extra_context`` — balances + earning components + leave
period health), draft edit (``update_*``), employee withdraw (``withdraw_*``),
draft delete (``delete_leave_extra_draft``) and realtime ``leave_extra_updated``
published from every mutation.

Schema note (plan-test-complete-hr-extra G4/P0bis): the stock HRMS doctypes do
NOT fit the portal contract — Leave Encashment's native ``status`` Select has
no "Rejected" option, and Compensatory Leave Request has no ``status`` column
at all (its real end-date field is ``work_end_date``, not ``work_to_date``).
The portal lifecycle therefore lives in the ``vn_status`` / ``vn_note`` custom
fields (see ``custom_fields.py`` → ``_PORTAL_LIFECYCLE_FIELDS``): every row
created/approved/rejected through the portal stamps ``vn_status`` (Draft /
Approved / Rejected) and lists coalesce ``status = vn_status || docstatus
fallback`` so the SPA contract stays unchanged.
"""

from __future__ import annotations

import frappe

from gege_hr.gege_hr.utils import notify, pagination

ENCASHMENT_DOCTYPE = "Leave Encashment"
COMPOFF_DOCTYPE = "Compensatory Leave Request"

# Portal status vocabulary (vn_status custom field on both DocTypes).
PORTAL_STATUSES = ("Draft", "Approved", "Rejected")

# docstatus → portal-status fallback for legacy rows created before vn_status
# existed (or directly in the HRMS desk).
_DOCSTATUS_FALLBACK = {0: "Draft", 1: "Approved", 2: "Cancelled"}

_ENCASH_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "leave_type",
    "encashment_days",
    "encashment_amount",
    "docstatus",
    "vn_status",
    "vn_note",
]
_COMPOFF_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "leave_type",
    "work_from_date",
    "work_end_date",  # HRMS field — projected back to `work_to_date` for the SPA
    "reason",
    "docstatus",
    "vn_status",
    "vn_note",
]


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def encashment_amount(days, per_day: float = 0.0) -> float:
    """Estimated encashment payout = days × per-day rate (helper for the form)."""
    return round(_num(days) * _num(per_day), 2)


# --------------------------------------------------------------------------- #
# Pure portal-status helpers (bench-free — unit tested directly)
# --------------------------------------------------------------------------- #
def portal_status(vn_status, docstatus=0) -> str:
    """Coalesce the row status shown to the SPA.

    ``vn_status`` (set by every portal write) wins; otherwise fall back to a
    docstatus mapping so legacy/desk-created rows still render something sane.
    """
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


def project_row(row: dict, *, compoff: bool = False) -> dict:
    """DB row → SPA contract: coalesced ``status`` + ``work_to_date`` alias."""
    out = dict(row or {})
    out["status"] = portal_status(out.pop("vn_status", None), out.pop("docstatus", 0))
    if compoff:
        out["work_to_date"] = out.pop("work_end_date", None)
    return out


def append_note(existing: str | None, reason: str | None) -> str:
    """Append a reject reason to the portal note (" | "-joined, idempotent-ish)."""
    note = (str(existing or "")).strip()
    why = (str(reason or "")).strip()
    if not why:
        return note
    tag = f"Từ chối: {why}"
    if tag in note:
        return note
    return f"{note} | {tag}".strip(" |")


def _cancel_linked_additional_salary(doc) -> None:
    """Pre-cancel the minted Additional Salary (as Administrator, same scoped
    precedent as approve) and unlink it so Leave Encashment cancel passes the
    core link check. Best-effort — failures log, the cancel above still runs."""
    ref = getattr(doc, "additional_salary", None)
    if not ref:
        return
    try:
        salary = frappe.get_doc("Additional Salary", ref)
        if salary and int(salary.get("docstatus") or 0) == 1:
            prev_user = frappe.session.user
            try:
                frappe.set_user("Administrator")
                salary.flags.ignore_permissions = True
                salary.cancel()
            finally:
                frappe.set_user(prev_user)
        frappe.db.set_value(ENCASHMENT_DOCTYPE, doc.get("name"), "additional_salary", "")
        doc.additional_salary = ""
    except Exception:
        frappe.log_error(title="leave_encashment.cancel additional_salary failed")


def append_withdraw_note(existing: str | None, reason: str | None) -> str:
    """Employee-withdraw note — same join semantics as ``append_note``."""
    note = (str(existing or "")).strip()
    why = (str(reason or "")).strip() or "Nhân viên tự rút đơn"
    tag = f"Rút đơn: {why}"
    if tag in note:
        return note
    return f"{note} | {tag}".strip(" |")


# --------------------------------------------------------------------------- #
# Desk-free P0 helpers (plan leave-extra-deskfree-complete §2.0)
# --------------------------------------------------------------------------- #
def _publish_leave_extra(doctype, name, employee=None, status=None) -> None:
    """Realtime ping for open ``/hr/leave-extra`` tabs. Best-effort — never raises."""
    try:
        frappe.publish_realtime(
            "leave_extra_updated",
            {"doctype": doctype, "name": name, "employee": employee, "status": status},
        )
    except Exception:
        pass


def _safe_row(doctype: str, name, fields: list) -> dict | None:
    try:
        rows = frappe.get_all(doctype, filters={"name": name}, fields=fields, limit_page_length=1) or []
        return rows[0] if rows else None
    except Exception:
        return None


def _linked(doc) -> dict:
    """Records HRMS mints on submit (F1/F2): Additional Salary (encashment —
    link field ``additional_salary`` stamped by HRMS ``on_submit``) and Leave
    Allocation (comp-off — link field ``leave_allocation``)."""
    out: dict = {}
    if getattr(doc, "doctype", None) == ENCASHMENT_DOCTYPE:
        ref = doc.get("additional_salary")
        if ref:
            out["additional_salary"] = _safe_row(
                "Additional Salary", ref, ["name", "status", "docstatus", "amount"]
            )
    else:
        ref = doc.get("leave_allocation")
        if ref:
            out["leave_allocation"] = _safe_row(
                "Leave Allocation",
                ref,
                [
                    "name",
                    "new_leaves_allocated",
                    "total_leaves_allocated",
                    "from_date",
                    "to_date",
                    "docstatus",
                ],
            )
    return out


def _attachments(doctype: str, name) -> list:
    try:
        return (
            frappe.get_all(
                "File",
                filters={"attached_to_doctype": doctype, "attached_to_name": name},
                fields=["name", "file_name", "file_url", "is_private", "file_size"],
                order_by="creation desc",
                limit_page_length=20,
            )
            or []
        )
    except Exception:
        return []


def _activity_rows(doctype: str, name, limit: int = 15) -> list:
    """Timeline: Version (ai sửa gì) + Comment (trao đổi), newest first."""
    rows: list = []
    try:
        rows += [
            {
                "type": "version",
                "owner": r.get("owner"),
                "creation": r.get("modified"),
                "data": r.get("data"),
            }
            for r in frappe.get_all(
                "Version",
                filters={"ref_doctype": doctype, "docname": name},
                fields=["name", "owner", "modified", "data"],
                order_by="modified desc",
                limit_page_length=limit,
            )
            or []
        ]
    except Exception:
        pass
    try:
        rows += [
            {
                "type": "comment",
                "owner": r.get("owner"),
                "creation": r.get("creation"),
                "content": r.get("content"),
            }
            for r in frappe.get_all(
                "Comment",
                filters={"reference_doctype": doctype, "reference_name": name, "comment_type": "Comment"},
                fields=["name", "owner", "creation", "content"],
                order_by="creation desc",
                limit_page_length=limit,
            )
            or []
        ]
    except Exception:
        pass
    rows.sort(key=lambda r: str(r.get("creation") or ""), reverse=True)
    return rows[:limit]


def _detail_can(doc, *, is_hr: bool | None = None, caller_emp=None) -> dict:
    """Action matrix for the drawer (§3 — the BE is the single source of truth).

    owner‖HR may edit/withdraw/delete a docstatus-0 row; approve/reject are
    HR-only and only where a decision is still meaningful; ``resend`` covers
    Rejected rows (copy-to-new prefill on the SPA).
    """
    if is_hr is None:
        is_hr = _is_manager()
    if caller_emp is None:
        try:
            caller_emp = frappe.db.get_value("Employee", {"user_id": frappe.session.user})
        except Exception:
            caller_emp = None
    mine = bool(caller_emp) and doc.get("employee") == caller_emp
    allowed = mine or is_hr
    ds = int(doc.get("docstatus") or 0)
    vn = str(doc.get("vn_status") or "").strip() or "Draft"
    is_draft = ds == 0 and vn == "Draft"
    return {
        "edit": allowed and ds == 0 and vn in ("Draft", "Rejected"),
        "withdraw": allowed and is_draft,
        "delete": allowed and ds == 0,
        "approve": is_hr and is_draft,
        "reject": is_hr and (is_draft or ds == 1),
        "resend": allowed and ((ds == 0 and vn == "Rejected") or ds == 2),
        "comment": allowed,
        "attach": allowed and ds == 0,
    }


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


def _notify_outcome(employee: str, name: str, doctype: str, outcome: str, label: str, state: str) -> None:
    """Best-effort employee notification — never aborts the transition."""
    try:
        notify.push_notification(
            employee=employee,
            notification_type="Leave",
            title=f"Yêu cầu {label} đã {outcome}",
            message=f"Yêu cầu {label} của bạn đã được {outcome} ({state}).",
            reference_doctype=doctype,
            reference_name=name,
            action_url="/leave-extra",
        )
    except Exception:
        pass


def remaining_leave_days(employee: str, leave_type: str):
    """Remaining balance for ``leave_type`` — None when it cannot be resolved.

    Primary path delegates to HRMS ``get_leave_balance_on`` (handles pending
    allocations, expired periods, taken leaves). The bare-allocation fallback
    covers stub environments where only Leave Allocation rows are seeded.
    """
    try:
        from frappe.utils import getdate

        today = getdate()
    except Exception:
        from datetime import date as _d

        today = _d.today()
    try:
        from hrms.hr.utils import get_leave_balance_on  # type: ignore

        return float(get_leave_balance_on(employee, leave_type, today) or 0.0)
    except ImportError:
        pass
    except Exception:
        return None
    try:
        alloc = frappe.db.get_value(
            "Leave Allocation",
            {
                "employee": employee,
                "leave_type": leave_type,
                "from_date": ["<=", str(today)],
                "to_date": [">=", str(today)],
                "docstatus": 1,
            },
            "total_leaves_allocated",
        )
        if alloc is None:
            return 0.0
        return _num(alloc)
    except Exception:
        return None


def _or_filters_for(doctype, q):
    """Broad-search OR LIKE covering every text + numeric content field (DNA §6.6 A).

    Datetime columns (creation / work_*_date) are deliberately NOT `like`-d —
    Frappe casts the value to datetime and raises ParserError, breaking the whole
    query. Date filtering uses a `creation` day-range in the popover instead.
    """
    like = f"%{pagination.escape_like(q)}%"
    base = [
        ["employee_name", "like", like],
        ["name", "like", like],
        ["leave_type", "like", like],
    ]
    if doctype == ENCASHMENT_DOCTYPE:
        return base + [
            ["encashment_days", "like", like],
            ["encashment_amount", "like", like],
        ]
    return base + [["reason", "like", like]]


def _list(
    doctype,
    fields,
    filters,
    status,
    search,
    page,
    page_size,
    leave_type=None,
    date_from=None,
    date_to=None,
    days_field=None,
    days_min=None,
    days_max=None,
) -> dict:
    flt = list(filters or [])
    if status:
        flt.append(["vn_status", "=", status])
    if leave_type:
        flt.append(["leave_type", "=", leave_type])
    # Date range on `creation` (day-range, NOT `like` datetime — DNA §6.6 A).
    if date_from:
        flt.append(["creation", ">=", f"{date_from} 00:00:00"])
    if date_to:
        flt.append(["creation", "<=", f"{date_to} 23:59:59"])
    # Numeric range as two list conditions (DNA §6.6 B — NOT `between`).
    if days_field:
        if days_min not in (None, ""):
            flt.append([days_field, ">=", _num(days_min)])
        if days_max not in (None, ""):
            flt.append([days_field, "<=", _num(days_max)])

    or_filters = None
    q = (search or "").strip()
    if q:
        or_filters = _or_filters_for(doctype, q)

    page = max(1, int(page or 1))
    page_size = max(1, int(page_size or 20))
    is_compoff = doctype == COMPOFF_DOCTYPE
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
        # `frappe.db.count` does not accept `or_filters` → count via names (DNA §6.6 A).
        total = len(
            frappe.get_all(
                doctype, filters=flt or None, or_filters=or_filters, fields=["name"], limit_page_length=0
            )
            or []
        )
    except Exception:
        frappe.log_error(title=f"leave_extra.list {doctype} failed")
        return {"data": [], "total": 0}
    return {
        "data": [project_row(r, compoff=is_compoff) for r in rows],
        "total": total,
    }


# --------------------------------------------------------------------------- #
# Leave Encashment
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def my_leave_encashments(
    employee=None,
    status=None,
    search=None,
    page=1,
    page_size=20,
    leave_type=None,
    date_from=None,
    date_to=None,
    days_min=None,
    days_max=None,
):
    emp = _resolve(employee)
    _assert_own(emp)
    return _list(
        ENCASHMENT_DOCTYPE,
        _ENCASH_FIELDS,
        [["employee", "=", emp]],
        status,
        search,
        page,
        page_size,
        leave_type=leave_type,
        date_from=date_from,
        date_to=date_to,
        days_field="encashment_days",
        days_min=days_min,
        days_max=days_max,
    )


@frappe.whitelist()
def all_leave_encashments(
    status=None,
    search=None,
    page=1,
    page_size=20,
    leave_type=None,
    date_from=None,
    date_to=None,
    days_min=None,
    days_max=None,
):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager xem tất cả.")
    return _list(
        ENCASHMENT_DOCTYPE,
        _ENCASH_FIELDS,
        None,
        status,
        search,
        page,
        page_size,
        leave_type=leave_type,
        date_from=date_from,
        date_to=date_to,
        days_field="encashment_days",
        days_min=days_min,
        days_max=days_max,
    )


def _default_leave_period(company: str | None = None):
    """The active Leave Period covering today (HRMS marks it mandatory on
    Leave Encashment). Best-effort — None when no submitted period exists.

    Prefers ``is_active=1`` periods (HRMS allocation lookups filter on it);
    falls back to any submitted period covering today so the mandatory link
    can still be satisfied, with HRMS validate surfacing data issues.
    """
    try:
        from frappe.utils import getdate

        today = str(getdate())
    except Exception:
        from datetime import date as _d

        today = _d.today().isoformat()
    try:
        base = {
            "from_date": ["<=", today],
            "to_date": [">=", today],
            "docstatus": 1,
        }
        if company:
            base["company"] = company
        rows = frappe.get_all(
            "Leave Period",
            filters={**base, "is_active": 1},
            pluck="name",
            order_by="from_date desc",
            limit_page_length=1,
        )
        if not rows:
            rows = frappe.get_all(
                "Leave Period", filters=base, pluck="name", order_by="from_date desc", limit_page_length=1
            )
        return rows[0] if rows else None
    except Exception:
        return None


@frappe.whitelist()
def submit_leave_encashment(employee=None, leave_type=None, encashment_days=None, earning_component=None):
    emp = _resolve(employee)
    _assert_own(emp)
    days = _num(encashment_days)
    if not leave_type or days <= 0:
        frappe.throw("Cần loại phép + số ngày đổi > 0.")
    # G3 — never request more days than the employee still has allocated.
    remaining = remaining_leave_days(emp, leave_type)
    if remaining is not None and days > remaining + 1e-9:
        frappe.throw(f"Số ngày đổi ({days:g}) vượt số dư phép còn lại ({remaining:g}) của loại phép này.")
    company = frappe.db.get_value("Employee", emp, "company")
    doc = frappe.new_doc(ENCASHMENT_DOCTYPE)
    doc.employee = emp
    doc.leave_type = leave_type
    doc.encashment_days = days
    # HRMS validates a mandatory leave_period — resolve the active one so the
    # portal submission works without HR pre-linking it in the desk.
    doc.leave_period = _default_leave_period(company)
    if earning_component:
        doc.earning_component = earning_component
    doc.company = company
    doc.vn_status = "Draft"
    doc.insert(ignore_permissions=True)
    _publish_leave_extra(ENCASHMENT_DOCTYPE, doc.name, emp, "Draft")
    return {"name": doc.name, "encashment_days": days, "encashment_amount": doc.get("encashment_amount")}


@frappe.whitelist()
def approve_leave_encashment(name=None):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager duyệt.")
    doc = frappe.get_doc(ENCASHMENT_DOCTYPE, name)
    if getattr(doc, "docstatus", 0) == 2:
        frappe.throw("Yêu cầu đã bị hủy, không thể duyệt.")
    # Manager-gated above. HRMS's on_submit inserts an Additional Salary for the
    # employee — Frappe's per-employee User-Permission link check blocks a
    # manager from creating payroll rows for OTHER employees, so the submit (and
    # its side-effects) runs as Administrator, then the session is restored.
    prev_user = frappe.session.user
    try:
        frappe.set_user("Administrator")
        doc.flags.ignore_permissions = True
        if getattr(doc, "docstatus", 0) == 0:
            doc.vn_status = "Approved"  # stamped BEFORE submit so hooks see it
            try:
                doc.submit()
            except Exception as exc:
                frappe.log_error(title="leave_encashment.submit failed")
                frappe.throw(f"Duyệt đổi phép thất bại: {exc}")
        else:
            doc.vn_status = "Approved"
            doc.save(ignore_permissions=True)
    finally:
        frappe.set_user(prev_user)
    _publish_leave_extra(ENCASHMENT_DOCTYPE, name, doc.get("employee"), "Approved")
    _notify_outcome(doc.get("employee"), name, ENCASHMENT_DOCTYPE, "duyệt", "đổi phép", "Approved")
    return {"name": doc.name, "status": portal_status("Approved", getattr(doc, "docstatus", 0))}


@frappe.whitelist()
def reject_leave_encashment(name=None, reason=None):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager từ chối.")
    doc = frappe.get_doc(ENCASHMENT_DOCTYPE, name)
    note = append_note(getattr(doc, "vn_note", None), reason)
    doc.flags.ignore_permissions = True
    if getattr(doc, "docstatus", 0) == 0:
        doc.vn_status = "Rejected"
        doc.vn_note = note
        doc.save(ignore_permissions=True)
    else:
        if getattr(doc, "docstatus", 0) == 1:
            _cancel_linked_additional_salary(doc)
            try:
                # HRMS on_cancel intends to cancel the minted Additional
                # Salary itself, but the core link check blocks cancel BEFORE
                # on_cancel runs — pre-cancel above + ignore_links here.
                doc.flags.ignore_links = True
                doc.cancel()
            except Exception as exc:
                frappe.log_error(title="leave_encashment.cancel failed")
                frappe.throw(f"Hủy đổi phép thất bại: {exc}")
        try:
            frappe.db.set_value(ENCASHMENT_DOCTYPE, name, {"vn_status": "Rejected", "vn_note": note})
        except Exception:
            frappe.log_error(title="leave_encashment.reject set_value failed")
    _publish_leave_extra(ENCASHMENT_DOCTYPE, name, doc.get("employee"), "Rejected")
    _notify_outcome(doc.get("employee"), name, ENCASHMENT_DOCTYPE, "từ chối", "đổi phép", "Rejected")
    return {"name": name, "status": "Rejected", "note": note}


# --------------------------------------------------------------------------- #
# Compensatory Leave (comp-off)
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def my_comp_off_requests(
    employee=None,
    status=None,
    search=None,
    page=1,
    page_size=20,
    leave_type=None,
    date_from=None,
    date_to=None,
):
    emp = _resolve(employee)
    _assert_own(emp)
    return _list(
        COMPOFF_DOCTYPE,
        _COMPOFF_FIELDS,
        [["employee", "=", emp]],
        status,
        search,
        page,
        page_size,
        leave_type=leave_type,
        date_from=date_from,
        date_to=date_to,
    )


@frappe.whitelist()
def all_comp_off_requests(
    status=None,
    search=None,
    page=1,
    page_size=20,
    leave_type=None,
    date_from=None,
    date_to=None,
):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager xem tất cả.")
    return _list(
        COMPOFF_DOCTYPE,
        _COMPOFF_FIELDS,
        None,
        status,
        search,
        page,
        page_size,
        leave_type=leave_type,
        date_from=date_from,
        date_to=date_to,
    )


@frappe.whitelist()
def submit_comp_off(employee=None, leave_type=None, work_from_date=None, work_to_date=None, reason=None):
    emp = _resolve(employee)
    _assert_own(emp)
    if not (work_from_date and work_to_date):
        frappe.throw("Cần ngày bắt đầu + kết thúc làm bù.")
    # G1 — reject inverted date ranges at the door.
    if not date_order_ok(work_from_date, work_to_date):
        frappe.throw("Ngày kết thúc làm bù không được trước ngày bắt đầu.")
    company = frappe.db.get_value("Employee", emp, "company")
    doc = frappe.new_doc(COMPOFF_DOCTYPE)
    doc.employee = emp
    doc.leave_type = leave_type or "Compensatory Off"
    doc.work_from_date = work_from_date
    doc.work_end_date = work_to_date  # ← HRMS field (work_to_date does not exist)
    doc.reason = reason or ""
    doc.company = company
    doc.vn_status = "Draft"
    doc.insert(ignore_permissions=True)
    _publish_leave_extra(COMPOFF_DOCTYPE, doc.name, emp, "Draft")
    return {"name": doc.name, "work_from_date": work_from_date, "work_to_date": work_to_date}


@frappe.whitelist()
def approve_comp_off(name=None):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager duyệt.")
    doc = frappe.get_doc(COMPOFF_DOCTYPE, name)
    if getattr(doc, "docstatus", 0) == 2:
        frappe.throw("Yêu cầu đã bị hủy, không thể duyệt.")
    # Manager-gated above; HRMS's on_submit creates a Leave Allocation for the
    # employee, which the per-employee User-Permission link check would block
    # for any employee but the manager's own — run as Administrator, restore.
    prev_user = frappe.session.user
    try:
        frappe.set_user("Administrator")
        doc.flags.ignore_permissions = True
        if getattr(doc, "docstatus", 0) == 0:
            doc.vn_status = "Approved"
            try:
                doc.submit()
            except Exception as exc:
                frappe.log_error(title="comp_off.submit failed")
                frappe.throw(f"Duyệt làm bù thất bại: {exc}")
        else:
            doc.vn_status = "Approved"
            doc.save(ignore_permissions=True)
    finally:
        frappe.set_user(prev_user)
    _publish_leave_extra(COMPOFF_DOCTYPE, name, doc.get("employee"), "Approved")
    _notify_outcome(doc.get("employee"), name, COMPOFF_DOCTYPE, "duyệt", "nghỉ bù", "Approved")
    return {"name": doc.name, "status": portal_status("Approved", getattr(doc, "docstatus", 0))}


@frappe.whitelist()
def reject_comp_off(name=None, reason=None):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager từ chối.")
    doc = frappe.get_doc(COMPOFF_DOCTYPE, name)
    doc.flags.ignore_permissions = True  # manager-gated; bypass per-employee link check
    note = append_note(getattr(doc, "vn_note", None), reason)
    if getattr(doc, "docstatus", 0) == 0:
        doc.vn_status = "Rejected"
        doc.vn_note = note
        doc.save(ignore_permissions=True)
    else:
        if getattr(doc, "docstatus", 0) == 1:
            try:
                # Same link-check shield as the encashment path (the comp-off
                # cancel adjusts its Leave Allocation via on_cancel).
                doc.flags.ignore_links = True
                doc.cancel()
            except Exception as exc:
                frappe.log_error(title="comp_off.cancel failed")
                frappe.throw(f"Hủy nghỉ bù thất bại: {exc}")
        try:
            frappe.db.set_value(COMPOFF_DOCTYPE, name, {"vn_status": "Rejected", "vn_note": note})
        except Exception:
            frappe.log_error(title="comp_off.reject set_value failed")
    _publish_leave_extra(COMPOFF_DOCTYPE, name, doc.get("employee"), "Rejected")
    _notify_outcome(doc.get("employee"), name, COMPOFF_DOCTYPE, "từ chối", "nghỉ bù", "Rejected")
    return {"name": name, "status": "Rejected", "note": note}


# --------------------------------------------------------------------------- #
# Desk-free P0 — detail / context / update / withdraw / delete
# (plan leave-extra-deskfree-complete §2.1-§2.6)
# --------------------------------------------------------------------------- #
_ENCASH_DETAIL_FIELDS = _ENCASH_FIELDS + [
    "earning_component",
    "leave_period",
    "leave_allocation",
    "encashment_date",
    "additional_salary",
    "currency",
    "company",
    "owner",
    "creation",
    "modified",
    "modified_by",
]
_COMPOFF_DETAIL_FIELDS = _COMPOFF_FIELDS + [
    "leave_allocation",
    "company",
    "owner",
    "creation",
    "modified",
    "modified_by",
]


def _get_detail(doctype: str, name, fields: list, *, compoff: bool) -> dict:
    if not name:
        frappe.throw("Thiếu mã yêu cầu.")
    doc = frappe.get_doc(doctype, name)
    if not doc:
        frappe.throw("Không tìm thấy yêu cầu.")
    _assert_own(doc.get("employee"))
    row = project_row({f: doc.get(f) for f in fields}, compoff=compoff)
    row["docstatus"] = int(doc.get("docstatus") or 0)
    return {
        "doc": row,
        "links": _linked(doc),
        "attachments": _attachments(doctype, name),
        "activity": _activity_rows(doctype, name),
        "can": _detail_can(doc),
    }


@frappe.whitelist()
def get_leave_encashment(name=None) -> dict:
    """§2.2 — one encashment for the self-service drawer (can-matrix truth)."""
    return _get_detail(ENCASHMENT_DOCTYPE, name, _ENCASH_DETAIL_FIELDS, compoff=False)


@frappe.whitelist()
def get_comp_off(name=None) -> dict:
    """§2.2 — one comp-off request for the self-service drawer."""
    return _get_detail(COMPOFF_DOCTYPE, name, _COMPOFF_DETAIL_FIELDS, compoff=True)


@frappe.whitelist()
def leave_extra_context() -> dict:
    """§2.1 — create-form context: options + live balances + earning
    components + active Leave Period health, so the SPA submits without desk."""
    base = leave_extra_options() or {}
    balances: dict = {}
    emp = None
    try:
        emp = frappe.db.get_value("Employee", {"user_id": frappe.session.user})
    except Exception:
        emp = None
    if emp:
        for t in base.get("leave_types") or []:
            balances[t] = remaining_leave_days(emp, t)
    try:
        # NOTE: the Select value is "Earning" (capitalised) — filter in Python
        # (case-insensitive) so the dropdown survives schema-casing drift.
        comp_rows = (
            frappe.get_all(
                "Salary Component",
                filters={"disabled": 0},
                fields=["name", "type"],
                limit_page_length=200,
            )
            or []
        )
        earning = sorted(
            r.get("name") for r in comp_rows if str(r.get("type") or "").lower() == "earning"
        )
    except Exception:
        earning = []
    company = None
    if emp:
        try:
            company = frappe.db.get_value("Employee", emp, "company")
        except Exception:
            company = None
    period = _default_leave_period(company)
    return {
        **base,
        "balances": balances,
        "earning_components": earning,
        "leave_period": period,
        "health": "ok" if period else "no_leave_period",
    }


def _reload(doc) -> None:
    """Best-effort race-guard reload before a draft write."""
    try:
        doc.reload()
    except Exception:
        pass


def _load_owned_draft(doctype: str, name, locked_msg: str):
    if not name:
        frappe.throw("Thiếu mã yêu cầu.")
    doc = frappe.get_doc(doctype, name)
    if not doc:
        frappe.throw("Không tìm thấy yêu cầu.")
    _assert_own(doc.get("employee"))
    if int(doc.get("docstatus") or 0) != 0:
        frappe.throw(locked_msg)
    return doc


def _reset_and_save(doc) -> None:
    """Save a draft with scoped bypass; a Rejected draft resets to Draft."""
    if str(doc.get("vn_status") or "").strip() == "Rejected":
        doc.vn_status = "Draft"
    try:
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)
    finally:
        try:
            doc.flags.ignore_permissions = False
        except Exception:
            pass


@frappe.whitelist()
def update_leave_encashment(name=None, leave_type=None, encashment_days=None, earning_component=None) -> dict:
    """§2.3 — edit a Draft encashment (owner or HR); re-runs the submit-path
    validations (G3 balance gate) and resets a Rejected draft to Draft."""
    doc = _load_owned_draft(ENCASHMENT_DOCTYPE, name, "Chỉ sửa được yêu cầu chưa duyệt.")
    _reload(doc)
    new_type = leave_type or doc.get("leave_type")
    if encashment_days not in (None, ""):
        new_days = _num(encashment_days)
    else:
        new_days = _num(doc.get("encashment_days"))
    if not new_type or new_days <= 0:
        frappe.throw("Cần loại phép + số ngày đổi > 0.")
    remaining = remaining_leave_days(doc.get("employee"), new_type)
    if remaining is not None and new_days > remaining + 1e-9:
        frappe.throw(
            f"Số ngày đổi ({new_days:g}) vượt số dư phép còn lại ({remaining:g}) của loại phép này."
        )
    doc.leave_type = new_type
    doc.encashment_days = new_days
    if earning_component:
        doc.earning_component = earning_component
    _reset_and_save(doc)
    _publish_leave_extra(ENCASHMENT_DOCTYPE, doc.get("name"), doc.get("employee"), "Draft")
    return {
        "name": doc.get("name"),
        "status": "Draft",
        "encashment_days": new_days,
        "encashment_amount": doc.get("encashment_amount"),
    }


@frappe.whitelist()
def update_comp_off(name=None, work_from_date=None, work_to_date=None, reason=None) -> dict:
    """§2.3 — edit a Draft comp-off (owner or HR); date-order gate (G1)."""
    doc = _load_owned_draft(COMPOFF_DOCTYPE, name, "Chỉ sửa được yêu cầu chưa duyệt.")
    _reload(doc)
    new_from = work_from_date or doc.get("work_from_date")
    new_to = work_to_date or doc.get("work_to_date") or doc.get("work_end_date")
    if not (new_from and new_to):
        frappe.throw("Cần ngày bắt đầu + kết thúc làm bù.")
    if not date_order_ok(new_from, new_to):
        frappe.throw("Ngày kết thúc làm bù không được trước ngày bắt đầu.")
    doc.work_from_date = new_from
    doc.work_end_date = new_to
    if reason is not None:
        doc.reason = reason
    _reset_and_save(doc)
    _publish_leave_extra(COMPOFF_DOCTYPE, doc.get("name"), doc.get("employee"), "Draft")
    return {
        "name": doc.get("name"),
        "status": "Draft",
        "work_from_date": new_from,
        "work_to_date": new_to,
    }


def _withdraw(doctype: str, name, note) -> dict:
    """§2.4 — employee withdraws their own Draft (vn_status Rejected + note)."""
    doc = _load_owned_draft(doctype, name, "Chỉ rút được yêu cầu chưa duyệt.")
    vn = str(doc.get("vn_status") or "").strip() or "Draft"
    if vn != "Draft":
        frappe.throw("Yêu cầu đã có kết quả, không thể rút.")
    text = append_withdraw_note(doc.get("vn_note"), note)
    doc.vn_status = "Rejected"
    doc.vn_note = text
    try:
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)
    finally:
        try:
            doc.flags.ignore_permissions = False
        except Exception:
            pass
    _publish_leave_extra(doctype, name, doc.get("employee"), "Rejected")
    return {"name": name, "status": "Rejected", "note": text}


@frappe.whitelist()
def withdraw_leave_encashment(name=None, note=None) -> dict:
    return _withdraw(ENCASHMENT_DOCTYPE, name, note)


@frappe.whitelist()
def withdraw_comp_off(name=None, note=None) -> dict:
    return _withdraw(COMPOFF_DOCTYPE, name, note)


@frappe.whitelist()
def delete_leave_extra_draft(doctype=None, name=None) -> dict:
    """§2.5 — hard-delete a docstatus-0 row (trash); scoped bypass per F7."""
    if doctype not in (ENCASHMENT_DOCTYPE, COMPOFF_DOCTYPE):
        frappe.throw("Loại yêu cầu không hợp lệ.")
    doc = _load_owned_draft(doctype, name, "Chỉ xoá được bản nháp.")
    _publish_leave_extra(doctype, name, doc.get("employee"), "Deleted")
    try:
        frappe.delete_doc(doctype, name, ignore_permissions=True)
    except TypeError:
        frappe.delete_doc(doctype, name)
    return {"name": name, "deleted": True}


@frappe.whitelist()
def add_leave_extra_comment(doctype=None, name=None, text=None) -> dict:
    """§2.7 (P1) — comment thread entry (Frappe Comment, scoped bypass F7)."""
    if doctype not in (ENCASHMENT_DOCTYPE, COMPOFF_DOCTYPE):
        frappe.throw("Loại yêu cầu không hợp lệ.")
    content = (str(text or "")).strip()
    if not content:
        frappe.throw("Nội dung bình luận không được để trống.")
    doc = frappe.get_doc(doctype, name)
    if not doc:
        frappe.throw("Không tìm thấy yêu cầu.")
    _assert_own(doc.get("employee"))
    row = frappe.get_doc(
        {
            "doctype": "Comment",
            "comment_type": "Comment",
            "reference_doctype": doctype,
            "reference_name": name,
            "content": content,
        }
    )
    try:
        row.flags.ignore_permissions = True
        row.insert(ignore_permissions=True)
    finally:
        try:
            row.flags.ignore_permissions = False
        except Exception:
            pass
    _publish_leave_extra(doctype, name, doc.get("employee"), "Comment")
    return {
        "name": row.get("name"),
        "content": content,
        "owner": getattr(row, "owner", None) or frappe.session.user,
    }


@frappe.whitelist()
def bulk_leave_extra_action(doctype=None, names=None, action=None, reason=None) -> dict:
    """§2.7 (P1) — bulk approve/reject through the SAME endpoints (Administrator
    set_user path preserved). Partial-safe: per-row try/except, never throws."""
    if doctype not in (ENCASHMENT_DOCTYPE, COMPOFF_DOCTYPE):
        frappe.throw("Loại yêu cầu không hợp lệ.")
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager thao tác loạt.")
    if action not in ("approve", "reject"):
        frappe.throw("Hành động không hợp lệ.")
    if isinstance(names, str):
        names = [n for n in (x.strip() for x in names.split(",")) if n]
    names = list(names or [])
    if not names:
        frappe.throw("Chưa chọn yêu cầu nào.")
    if action == "reject":
        fn = reject_leave_encashment if doctype == ENCASHMENT_DOCTYPE else reject_comp_off
    else:
        fn = approve_leave_encashment if doctype == ENCASHMENT_DOCTYPE else approve_comp_off
    updated: list = []
    failed: list = []
    for n in names:
        try:
            if action == "reject":
                fn(n, reason)
            else:
                fn(n)
            updated.append(n)
        except Exception as exc:
            failed.append({"name": n, "reason": str(exc)})
    return {"updated": updated, "failed": failed}


@frappe.whitelist()
def upload_leave_extra_attachment(doctype=None, name=None, is_private=1, **kwargs) -> dict:
    """§2.7 (P1, ATT fallback) — thin wrapper for when the native
    ``/api/method/upload_file`` is permission-blocked (a plain Employee often
    lacks write on the target doctype); ownership-gated + scoped bypass F7.

    NOTE: frappe does NOT map multipart FILES onto whitelisted kwargs — the
    file arrives via ``frappe.request.files['file']`` (both paths probed)."""
    if doctype not in (ENCASHMENT_DOCTYPE, COMPOFF_DOCTYPE):
        frappe.throw("Loại yêu cầu không hợp lệ.")
    doc = frappe.get_doc(doctype, name)
    if not doc:
        frappe.throw("Không tìm thấy yêu cầu.")
    _assert_own(doc.get("employee"))
    file = kwargs.get("file")
    if file is None and getattr(frappe, "request", None) is not None:
        files = getattr(frappe.request, "files", None) or {}
        try:
            file = files.get("file")
        except Exception:
            file = None
    if not file:
        frappe.throw("Thiếu tệp đính kèm.")
    from frappe.utils.file_manager import save_file

    filename = getattr(file, "filename", None) or "attachment"
    content = file.read() if hasattr(file, "read") else (file or b"")
    try:
        private = bool(int(is_private or 0))
    except (TypeError, ValueError):
        private = bool(is_private)
    # save_file on this frappe build has no ignore_permissions kwarg — use the
    # scoped global flag (has_permission honours frappe.flags.ignore_permissions).
    try:
        frappe.flags.ignore_permissions = True
        out = save_file(filename, content, doctype, name, is_private=private)
    finally:
        try:
            frappe.flags.ignore_permissions = False
        except Exception:
            pass
    _publish_leave_extra(doctype, name, doc.get("employee"), "Attachment")
    return {"name": out.get("name"), "file_name": out.get("file_name"), "file_url": out.get("file_url")}


@frappe.whitelist()
def leave_extra_summary() -> dict:
    """§2.7 (P1) — HR tab tiles: counts per portal status for both doctypes."""
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager xem tổng quan.")
    out: dict = {}
    for key, dt in (("encash", ENCASHMENT_DOCTYPE), ("compoff", COMPOFF_DOCTYPE)):
        counts = {s: 0 for s in PORTAL_STATUSES}
        try:
            rows = frappe.get_all(dt, fields=["vn_status"], limit_page_length=0) or []
        except Exception:
            rows = []
        for r in rows:
            s = (r.get("vn_status") or "").strip()
            if s:
                counts[s] = counts.get(s, 0) + 1
        out[key] = counts
    return out


def _status_options(doctype: str) -> list:
    """Distinct portal-status values for the gear-popover dropdown (DNA §6.4 / §6.2).

    Based on the ``vn_status`` custom field (portal source of truth), NOT the
    native Leave Encashment ``status`` — those values (Unpaid/Paid) belong to
    the payroll-payment lifecycle and would mislead the portal filter.
    """
    opts: list = list(PORTAL_STATUSES)
    try:
        opts += [
            r
            for r in frappe.db.get_all(doctype, fields=["vn_status"], pluck=True, limit_page_length=0) or []
            if r
        ]
    except Exception:
        pass
    seen, out = set(), []
    for o in opts:
        if o not in seen:
            seen.add(o)
            out.append(o)
    return out


def _encashable_flag() -> str:
    """The Leave Type "encashable" column for the installed HRMS version.

    Older HRMS versions expose ``is_encash``; this site's HRMS uses
    ``allow_encashment``. Falls back to ``allow_encashment`` when the meta
    cannot be read (bench-free tests).
    """
    try:
        for flag in ("allow_encashment", "is_encash"):
            df = frappe.get_meta("Leave Type").get_field(flag)
            if df:
                return flag
    except Exception:
        pass
    return "allow_encashment"


@frappe.whitelist()
def leave_extra_options() -> dict:
    """Filter options for the gear popover (DNA §6.4 / §6.2)."""
    try:
        leave_types = (
            frappe.get_all("Leave Type", filters={_encashable_flag(): 1}, pluck="name", order_by="name asc")
            or []
        )
    except Exception:
        leave_types = []
    return {
        "leave_types": leave_types,
        "encash_statuses": _status_options(ENCASHMENT_DOCTYPE),
        "compoff_statuses": _status_options(COMPOFF_DOCTYPE),
    }
