"""Checkout-miss API — HR admin list/resolve + employee explain endpoints.

Fronts the VN Checkout Miss doctype created by the auto-close engine
(``gege_hr.gege_hr.utils.checkout_miss``). HR can list + resolve tickets
(waive/penalise/close); employees can view their own tickets and submit an
explanation (optionally opening a Correction Request for a real late checkout).
"""
from __future__ import annotations

import frappe
from frappe import _

from gege_hr.gege_hr.api import audit as audit_api
from gege_hr.gege_hr.utils import employee as emp_utils

MISS_DOCTYPE = "VN Checkout Miss"

_MISS_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "work_date",
    "shift_type",
    "status",
    "occurrence_no",
    "auto_checkout_at",
    "explanation",
    "evidence_ref",
    "correction_request",
    "penalty_amount",
    "penalty_waived",
    "grace_deadline",
    "resolved_by",
    "resolved_on",
    "note",
    "company",
]


def _require_hr() -> None:
    roles = set(emp_utils.get_user_roles() or [])
    if not (roles & emp_utils.HR_MANAGER_ROLES):
        frappe.throw(_("Bạn không có quyền HR."), frappe.PermissionError)


_STATUS_FOR_ACTION = {"waive": "Waived", "penalise": "Penalised", "close": "Closed"}

# BUG-7 fix: state machine — ``Closed`` is terminal, and a ticket can't be
# re-resolved to the status it already holds (no-op guard).
_ALLOWED_ACTIONS = {
    "Pending": {"waive", "penalise", "close"},
    "Explained": {"waive", "penalise", "close"},
    "Waived": {"penalise", "close"},
    "Penalised": {"waive", "close"},
    "Closed": set(),
}


def _default_penalty_amount() -> float:
    """Penalty amount from VN HR Portal Setting.

    ``0`` stays ``0`` (a legitimate "no penalty" config) — only an unset
    (``None``) field falls back to the engine default (mirrors ``_config()``
    in utils.checkout_miss — never ``or``).
    """
    try:
        v = frappe.db.get_single_value("VN HR Portal Setting", "vn_cm_penalty_amount")
    except Exception:
        v = None
    if v is None:
        from gege_hr.gege_hr.utils.checkout_miss import DEFAULTS

        return float(DEFAULTS["penalty_amount"])
    return float(v)


def _payroll_state_for(ticket: dict) -> str | None:
    """Payroll impact state for a ticket's ``work_date`` (BUG-2 fix).

    ``None``         — no payroll period covers the date (safe to resolve);
    ``"calculated"`` — a period is Calculated: resolving is allowed but the
                       response flags ``payroll_recalc_required`` so HR
                       recalculates before approving;
    ``"locked"``     — the period is Approved AND salary slips were generated:
                       the ticket must not be mutated; handle the refund via
                       VN Payroll Adjustment instead.
    """
    work_date = ticket.get("work_date")
    if not work_date:
        return None
    try:
        period = frappe.db.get_value(
            "VN Payroll Review Period",
            filters={
                "company": ticket.get("company"),
                "from_date": ["<=", work_date],
                "to_date": [">=", work_date],
                "docstatus": ["<", 2],
            },
            fieldname=["name", "status"],
            as_dict=True,
        )
        if not period:
            return None
        if period.get("status") == "Approved":
            has_slip = frappe.db.exists(
                "VN Payroll Review Line",
                {
                    "payroll_review_period": period.get("name"),
                    "employee": ticket.get("employee"),
                    "salary_slip": ["is", "set"],
                },
            )
            return "locked" if has_slip else "calculated"
        if period.get("status") == "Calculated":
            return "calculated"
    except Exception:
        frappe.log_error(title="checkout_miss._payroll_state_for failed")
    return None


@frappe.whitelist()
def list_checkout_misses(
    status: str | None = None,
    employee: str | None = None,
    search: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """HR — paginated list of checkout-miss tickets with optional filters."""
    _require_hr()
    filters: list = [["docstatus", "<", 2]]
    if status:
        filters.append(["status", "=", status])
    if employee:
        filters.append(["employee", "=", emp_utils.emp_name(employee)])
    or_filters = None
    if search:
        like = f"%{search}%"
        or_filters = [
            ["employee_name", "like", like],
            ["employee", "like", like],
            ["shift_type", "like", like],
        ]
    try:
        return (
            frappe.db.get_all(
                MISS_DOCTYPE,
                filters=filters,
                or_filters=or_filters,
                fields=_MISS_FIELDS,
                order_by="work_date desc, creation desc",
                limit_page_length=max(1, min(int(limit or 100), 500)),
            )
            or []
        )
    except Exception:
        frappe.log_error(title="checkout_miss.list failed")
        return []


@frappe.whitelist()
def resolve_checkout_miss(
    name: str, action: str, note: str | None = None, waive_penalty: int = 0
) -> dict:
    """HR — resolve a ticket. ``action`` ∈ waive / penalise / close.

    BUG-7 fix: state machine — ``Closed`` is terminal and a no-op re-resolve
    is rejected; updates go through ``doc.save()`` so the doctype's own
    validate/on_update hooks fire (no raw ``db.set_value`` bypass).
    BUG-4 fix: penalising a first-N (amount=0) ticket stamps the configured
    penalty so the action actually deducts.
    BUG-2 fix: refuses to mutate a ticket whose payroll period is Approved
    with generated slips; flags ``payroll_recalc_required`` when the period
    is only Calculated so HR recalculates before approving.
    """
    _require_hr()
    name = (name or "").strip()
    action = (action or "").strip().lower()
    if action not in ("waive", "penalise", "close"):
        frappe.throw(_("Hành động không hợp lệ."))
    if not name or not frappe.db.exists(MISS_DOCTYPE, name):
        frappe.throw(_("Ticket không tồn tại."))

    doc = frappe.get_doc(MISS_DOCTYPE, name)
    current = (doc.status or "").strip()
    if action not in _ALLOWED_ACTIONS.get(current, set()):
        if current == "Closed":
            frappe.throw(_("Ticket đã đóng — không thể thay đổi."))
        frappe.throw(
            _("Không thể chuyển ticket từ trạng thái {0} sang {1}.").format(
                current, _STATUS_FOR_ACTION[action]
            )
        )
    if _STATUS_FOR_ACTION[action] == current:
        frappe.throw(_("Ticket đã ở trạng thái {0}.").format(current))

    payroll_state = _payroll_state_for(
        {"work_date": doc.work_date, "employee": doc.employee, "company": doc.company}
    )
    if payroll_state == "locked":
        frappe.throw(
            _(
                "Kỳ lương chứa ngày {0} đã phê duyệt và sinh phiếu lương — hãy xử "
                "lý bù/trừ qua VN Payroll Adjustment thay vì sửa ticket."
            ).format(doc.work_date),
            frappe.ValidationError,
        )

    doc.status = _STATUS_FOR_ACTION[action]
    doc.resolved_by = frappe.session.user
    doc.resolved_on = frappe.utils.now()
    doc.note = note or ""
    if action == "waive":
        doc.penalty_waived = 1
    elif action == "penalise":
        doc.penalty_waived = 0
        if not float(doc.penalty_amount or 0):
            doc.penalty_amount = _default_penalty_amount()
    # Role check already done via _require_hr() — bypass row-level perms so an
    # HR Manager without an explicit doctype perm can still resolve.
    doc.save(ignore_permissions=True)
    try:
        # FIX (audit silent no-op): log() requires a company to attribute the
        # event to, and the type must exist in AUDIT_TYPES — without the full
        # context the call returned None and NO audit row was ever written.
        audit_api.log(
            "Checkout Miss Resolve",
            company=doc.company,
            employee=doc.employee,
            work_date=doc.work_date,
            reference_doctype=MISS_DOCTYPE,
            reference_name=name,
            description=f"{action} ticket {name}" + (f" — {note}" if note else ""),
            old_value=current,
            new_value=_STATUS_FOR_ACTION[action],
        )
    except Exception:
        pass
    result = frappe.db.get_value(MISS_DOCTYPE, name, _MISS_FIELDS, as_dict=True) or {}
    result["payroll_recalc_required"] = payroll_state == "calculated"
    return result


@frappe.whitelist()
def my_checkout_misses(status: str | None = None) -> list[dict]:
    """Employee — their own checkout-miss tickets (Pending ones needing action)."""
    emp = emp_utils.get_employee_for_user()
    if not emp:
        return []
    filters = [["employee", "=", emp], ["docstatus", "<", 2]]
    if status:
        filters.append(["status", "=", status])
    try:
        return (
            frappe.db.get_all(
                MISS_DOCTYPE,
                filters=filters,
                fields=_MISS_FIELDS,
                order_by="work_date desc",
                limit_page_length=50,
            )
            or []
        )
    except Exception:
        return []


@frappe.whitelist()
def explain_checkout_miss(
    name: str,
    explanation: str,
    evidence_ref: str | None = None,
    correction_checkout_time: str | None = None,
) -> dict:
    """Employee — submit an explanation for a checkout-miss ticket.

    If ``correction_checkout_time`` is supplied, a VN Attendance Correction
    Request is opened so HR can verify the real late checkout and grant OT.
    """
    explanation = (explanation or "").strip()
    if not explanation:
        frappe.throw(_("Nội dung giải trình là bắt buộc."))
    name = (name or "").strip()
    if not name or not frappe.db.exists(MISS_DOCTYPE, name):
        frappe.throw(_("Ticket không tồn tại."))
    ticket = (
        frappe.db.get_value(MISS_DOCTYPE, name, ["employee", "status"], as_dict=True)
        or {}
    )
    ticket_emp = ticket.get("employee")
    emp = emp_utils.get_employee_for_user()
    # BUG-3 fix: a user with no Employee record must not explain anyone's ticket
    # (the old `if emp and ...` check silently passed when emp was None).
    if not emp or (ticket_emp and emp != ticket_emp):
        frappe.throw(_("Bạn chỉ được giải trình ticket của mình."),
                     frappe.PermissionError)
    # BUG-1 fix: only Pending/Explained tickets accept an explanation — flipping
    # a Penalised ticket back to Explained would silently drop the payroll
    # penalty (load_checkout_miss_penalty only counts Penalised non-waived).
    status = (ticket.get("status") or "").strip()
    if status not in ("Pending", "Explained"):
        frappe.throw(
            _("Ticket ở trạng thái {0} — không thể giải trình nữa.").format(
                status or "?"
            ),
            frappe.ValidationError,
        )
    updates: dict = {
        "explanation": explanation,
        "evidence_ref": evidence_ref or "",
        "status": "Explained",
    }
    # Optionally open a correction request for a real late checkout time.
    cr_name = None
    if correction_checkout_time:
        try:
            work_date = frappe.db.get_value(MISS_DOCTYPE, name, "work_date")
            cr = frappe.get_doc(
                {
                    "doctype": "VN Attendance Correction Request",
                    "employee": ticket_emp,
                    "work_date": work_date,
                    "requested_checkout_time": correction_checkout_time,
                    # BUG-5 fix: link the CR to its ticket so approving the CR
                    # can replace the fake OUT and auto-waive the ticket.
                    "vn_checkout_miss": name,
                    "reason": f"Giải trình quên checkout (ticket {name}): {explanation}",
                }
            )
            cr.insert(ignore_permissions=True)
            cr_name = cr.name
            updates["correction_request"] = cr_name
        except Exception:
            frappe.log_error(title="checkout_miss.explain correction create failed")
    frappe.db.set_value(MISS_DOCTYPE, name, updates)
    # Audit the employee's explanation (best-effort — same contract as resolve).
    try:
        ctx = (
            frappe.db.get_value(MISS_DOCTYPE, name, ["work_date", "company"], as_dict=True)
            or {}
        )
        audit_api.log(
            "Checkout Miss Explain",
            company=ctx.get("company"),
            employee=ticket_emp,
            work_date=ctx.get("work_date"),
            reference_doctype=MISS_DOCTYPE,
            reference_name=name,
            description=f"employee explained: {explanation[:120]}",
            old_value=status,
            new_value="Explained",
        )
    except Exception:
        pass
    return frappe.db.get_value(MISS_DOCTYPE, name, _MISS_FIELDS, as_dict=True) or {}
