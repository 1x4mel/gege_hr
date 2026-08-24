"""NEW-3 (hr-gap-audit 🟥) — Leave Encashment + Compensatory Leave API.

Reuses Frappe HR's ``Leave Encashment`` and ``Compensatory Leave Request``
DocTypes (no duplicate doctype) and exposes a DNA-compliant surface to the
portal: an employee lists/submits leave encashment (nghỉ phép đổi tiền) and
comp-off requests (nghỉ bù); an HR/Manager lists all + approves/rejects. Pure
helpers split out for unit testing. Mirrors ``api/expense.py``.

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
            try:
                doc.cancel()
            except Exception as exc:
                frappe.log_error(title="leave_encashment.cancel failed")
                frappe.throw(f"Hủy đổi phép thất bại: {exc}")
        try:
            frappe.db.set_value(ENCASHMENT_DOCTYPE, name, {"vn_status": "Rejected", "vn_note": note})
        except Exception:
            frappe.log_error(title="leave_encashment.reject set_value failed")
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
                doc.cancel()
            except Exception as exc:
                frappe.log_error(title="comp_off.cancel failed")
                frappe.throw(f"Hủy nghỉ bù thất bại: {exc}")
        try:
            frappe.db.set_value(COMPOFF_DOCTYPE, name, {"vn_status": "Rejected", "vn_note": note})
        except Exception:
            frappe.log_error(title="comp_off.reject set_value failed")
    _notify_outcome(doc.get("employee"), name, COMPOFF_DOCTYPE, "từ chối", "nghỉ bù", "Rejected")
    return {"name": name, "status": "Rejected", "note": note}


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
