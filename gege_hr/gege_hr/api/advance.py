"""
Salary Advance Request API — plan v5 §10.5 / doctype-design §23.

Milestone-2 read + submit endpoints fronting the VN Salary Advance Request
DocType. They map 1:1 to the frontend ``gege_hr.gege_hr.api.advance.<fn>`` calls
in ``hr-ui/src/api/index.js``:

  * ``my_advance_requests``   — the caller's own advance requests (date window)
  * ``submit_advance_request``— create a Draft advance request (validation,
    eligible-amount calc + naming handled by the DocType)
  * ``cancel_advance_request``— revoke a Draft/Pending request
  * ``mark_paid``             — record an approved advance as paid (materialises
    the Additional Salary deduction via the DocType ``on_update`` hook)
  * ``reverse_advance_payment``— undo a Paid advance: cancel its Additional
    Salary deduction and return it to Approved (or Cancelled)

The lifecycle is workflow-driven (Draft → Pending Manager → Pending HR →
Approved → Paid / Rejected / Cancelled) and is routed by the unified approval
inbox (:mod:`gege_hr.gege_hr.api.approval`) via a VN Approval Matrix with
``transaction_type = Salary Advance Request``.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import getdate

from gege_hr.gege_hr.api import audit as audit_api
from gege_hr.gege_hr.utils import employee as emp_utils
from gege_hr.gege_hr.utils import pagination
from gege_hr.gege_hr.utils.advance import REPAYMENT_PLAN_NEXT_MONTH
from gege_hr.gege_hr.utils.request_workflow import send_for_approval

DOCTYPE = "VN Salary Advance Request"
POLICY_DOCTYPE = "VN Salary Advance Policy"

# Row shape returned to the SPA — kept stable/flat so the list renders without
# a second lookup.
_LIST_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "company",
    "posting_date",
    "salary_advance_policy",
    "payroll_period",
    "requested_amount",
    "eligible_amount",
    "approved_amount",
    "repayment_plan",
    "workflow_state",
    "payment_status",
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


def _preview_eligibility(employee: str, requested_amount) -> dict:
    """Best-effort eligible-amount preview for the SPA form (plan §22).

    Resolves the best-matching active policy for the employee and computes the
    cap via :func:`compute_eligible_amount`. Returns ``{ policy, eligible_amount,
    within_limit }``. Any bench lookup failure degrades to a permissive empty
    preview so the SPA form never hard-blocks on a missing policy.
    """
    from gege_hr.gege_hr.utils.advance import (
        compute_eligible_amount,
        pick_advance_policy,
    )

    try:
        company = frappe.db.get_value("Employee", employee, "company")
        attrs = (
            frappe.db.get_value(
                "Employee",
                employee,
                ["company", "branch", "employee_group"],
                as_dict=True,
            )
            or {}
        )
        rows = frappe.db.get_all(
            POLICY_DOCTYPE,
            filters={"company": company, "is_active": 1},
            fields=[
                "name",
                "company",
                "branch",
                "employee_group",
                "max_percentage",
                "max_fixed_amount",
                "min_working_days",
                "max_requests_per_month",
                "cutoff_day",
                "modified",
            ],
        )
    except Exception:
        return {"policy": None, "eligible_amount": 0, "within_limit": True}

    policy = pick_advance_policy(rows, attrs)
    if not policy:
        return {"policy": None, "eligible_amount": 0, "within_limit": True}

    base = 0.0
    for field in ("vn_base_salary", "ctc", "gross_pay"):
        try:
            val = frappe.db.get_value("Employee", employee, field)
        except Exception:
            val = None
        try:
            val = float(val or 0)
        except (TypeError, ValueError):
            val = 0
        if val > 0:
            base = val
            break
    eligible = compute_eligible_amount(base, policy)
    try:
        req = float(requested_amount or 0)
    except (TypeError, ValueError):
        req = 0
    return {
        "policy": policy.get("name"),
        "eligible_amount": eligible,
        "within_limit": eligible <= 0 or req <= eligible + 0.01,
    }


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


@frappe.whitelist()
def my_advance_requests(
    employee: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 0,
) -> list[dict] | dict:
    """Plan §10.5 — the caller's advance requests, optionally narrowed by date.

    ``search`` OR-matches a free-text query across the row's text fields
    (name / reason / posting_date / employee / employee_name / status),
    applied server-side (DNA §6.6 D, HR-BL-08). Managers (HR Manager/System
    Manager) may pass any ``employee``; a plain Employee is scoped to own.

    Pagination is **opt-in** (DNA §6.6 A): pass ``page`` + a positive
    ``page_size`` to receive ``{"data": [...], "total": int, "summary": None}``
    (post-query filter → ``total`` is the filtered list length); without
    ``page_size`` the legacy bare-list return is preserved.
    """
    emp = _resolve(employee)
    _assert_own(emp)

    filters = {"employee": emp}
    if from_date or to_date:
        filters["posting_date"] = ["between", [from_date or to_date, to_date or from_date]]

    rows = frappe.db.get_all(
        DOCTYPE,
        filters=filters,
        fields=_LIST_FIELDS,
        order_by="posting_date desc, creation desc",
        limit_page_length=pagination.MAX_PAGE_SIZE * 10,  # newest-first bound
    )
    filtered = _filter_rows(
        rows,
        search,
        ("name", "reason", "posting_date", "employee", "employee_name", "status"),
    )
    return pagination.paginate_filtered(filtered, page=page, page_size=page_size)


@frappe.whitelist()
def all_advance_requests(
    status: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    """Manager-only (HR/Manager) list of *every* salary advance request across
    all employees — drives the SPA "Mark Paid / Reverse" admin panel.

    Optional filters: ``status`` (workflow_state, e.g. ``Approved``/``Paid``)
    and a ``posting_date`` date window. Plain Employees are rejected.
    """
    if not _is_manager():
        frappe.throw(
            _("Bạn không có quyền xem danh sách ứng lương chung."),
            frappe.PermissionError,
        )

    filters = {}
    if status:
        filters["workflow_state"] = status
    if from_date or to_date:
        filters["posting_date"] = ["between", [from_date or to_date, to_date or from_date]]

    rows = frappe.db.get_all(
        DOCTYPE,
        filters=filters,
        fields=_LIST_FIELDS,
        order_by="posting_date desc, creation desc",
        limit_page_length=pagination.MAX_PAGE_SIZE * 10,
    )
    return rows


@frappe.whitelist()
def preview_eligibility(
    employee: str | None = None,
    requested_amount=None,
) -> dict:
    """Plan §10.5 — eligible-amount preview for the SPA form (no row created)."""
    emp = _resolve(employee)
    _assert_own(emp)
    return _preview_eligibility(emp, requested_amount)


@frappe.whitelist()
def submit_advance_request(**kwargs) -> dict:
    """Plan §10.5 — create a Draft salary advance request.

    Accepts the flat payload the SPA sends: ``employee``, ``posting_date``,
    ``requested_amount``, ``reason`` (optional ``salary_advance_policy``,
    ``payroll_period``). ``repayment_plan`` is NOT client-controllable: the
    single supported method (deduct from the payroll period containing the
    request, disbursed the next month) is forced server-side.

    Returns ``{ name, status, message }``. Validation (amount > 0, within
    eligible amount, cutoff/quota, no-duplicate) runs in
    ``VNSalaryAdvanceRequest.validate``; naming in its ``before_insert`` hook
    (``SAR-YYMMDD-XXXXXX``); ``eligible_amount`` auto-calculated from policy.
    """
    if not kwargs:
        frappe.throw(_("Thiếu dữ liệu yêu cầu ứng lương."))

    employee = (kwargs.get("employee") or "").strip()
    if employee:
        _assert_own(employee)
    emp = _resolve(employee)

    doc = frappe.new_doc(DOCTYPE)
    doc.update(
        {
            "employee": emp,
            "posting_date": getdate(kwargs.get("posting_date")) if kwargs.get("posting_date") else None,
            "requested_amount": kwargs.get("requested_amount"),
            "reason": kwargs.get("reason") or "",
            # Single repayment method (2026-08 rule): always Next Month —
            # deduct from the payroll period containing the posting_date,
            # disbursed the following month. Any client-sent value is ignored.
            "repayment_plan": REPAYMENT_PLAN_NEXT_MONTH,
            "workflow_state": "Draft",
            "docstatus": 0,
        }
    )
    if kwargs.get("salary_advance_policy"):
        doc.salary_advance_policy = kwargs["salary_advance_policy"]
    if kwargs.get("payroll_period"):
        doc.payroll_period = kwargs["payroll_period"]

    # insert(ignore_permissions=True): the endpoint already authorizes via
    # _assert_own() (an employee may only submit for themselves), so the standard
    # doctype create-permission must be bypassed — otherwise an Employee-role
    # user gets a 403 and can never apply for a salary advance. Matches the
    # sibling self-service endpoints (attendance.submit_correction_request,
    # overtime.submit_overtime_request) which already pass ignore_permissions.
    doc.insert(ignore_permissions=True)
    # Move into the approval pipeline (Draft → Pending Manager). Best-effort:
    # stays Draft if the workflow isn't seeded yet.
    send_for_approval(doc)
    audit_api.log(
        "Advance Submit",
        doc=doc.as_dict(),
        work_date=doc.posting_date,
        description=f"Draft → {doc.workflow_state}",
    )
    return {
        "name": doc.name,
        "status": doc.workflow_state,
        "eligible_amount": doc.eligible_amount,
        "message": _("Đã tạo yêu cầu ứng lương {0}.").format(doc.name),
    }


@frappe.whitelist()
def cancel_advance_request(name: str | None = None) -> dict:
    """Cancel a Draft/Pending advance request (set Rejected, keep the audit).

    A dedicated endpoint is preferable to the SPA ``setValue`` fallback so the
    transition is consistent and permission-checked server-side. An already
    Approved/Paid request cannot be cancelled by the requester.
    """
    if not name:
        frappe.throw(_("Thiếu mã yêu cầu ứng lương."))
    doc = frappe.get_doc(DOCTYPE, name)
    _assert_own(doc.employee)
    if doc.docstatus >= 2:
        frappe.throw(_("Yêu cầu này đã bị hủy."))
    if doc.workflow_state in ("Approved", "Paid"):
        frappe.throw(
            _("Yêu cầu đã duyệt không thể hủy từ phía nhân viên."),
            frappe.PermissionError,
        )
    doc.workflow_state = "Rejected"
    if doc.docstatus == 1:
        doc.cancel()
    else:
        doc.save()
    return {
        "name": name,
        "status": doc.workflow_state,
        "message": _("Đã hủy yêu cầu ứng lương {0}.").format(name),
    }


@frappe.whitelist()
def mark_paid(
    name: str | None = None,
    payment_entry: str | None = None,
) -> dict:
    """Plan §23 — record that an approved salary advance has been paid out.

    HR/Payroll manager only. Transitions an ``Approved`` request to ``Paid`` and
    stamps ``payment_status``; the VN Salary Advance Request ``on_update`` hook
    then materialises the linked ERPNext ``Additional Salary`` deduction. An
    optional ``payment_entry`` (Frappe Payment Entry name) is stored on the row.

    Returns ``{ name, status, additional_salary, payment_status, message }``.
    """
    if not name:
        frappe.throw(_("Thiếu mã yêu cầu ứng lương."))
    if not _is_manager():
        frappe.throw(
            _("Chỉ HR/Payroll Manager mới có thể ghi nhận đã thanh toán."),
            frappe.PermissionError,
        )
    doc = frappe.get_doc(DOCTYPE, name)
    if doc.docstatus >= 2:
        frappe.throw(_("Yêu cầu này đã bị hủy."))
    if doc.workflow_state not in ("Approved", "Paid"):
        frappe.throw(_("Chỉ yêu cầu đã duyệt mới có thể ghi nhận thanh toán."))

    # H4 race guard: two "Mark Paid" clicks at once both read state=Approved,
    # both save() → on_update fires twice → TWO Additional Salary rows → the
    # advance is deducted from payroll twice. Claim the transition atomically:
    # only the request that flips Approved → Paid proceeds; the loser reloads,
    # sees Paid, and takes the idempotent path.
    if doc.workflow_state == "Approved":
        from gege_hr.gege_hr.utils._db import guarded_update

        claimed = guarded_update(
            "UPDATE `tabVN Salary Advance Request`"
            " SET workflow_state = 'Paid', payment_status = 'Paid'"
            " WHERE name = %(name)s AND workflow_state = 'Approved'",
            {"name": name},
        )
        if not claimed:
            doc.reload()
            if doc.workflow_state != "Paid":
                frappe.throw(_("Chỉ yêu cầu đã duyệt mới có thể ghi nhận thanh toán."))

    already_paid = doc.workflow_state == "Paid"
    if doc.workflow_state != "Paid":
        doc.workflow_state = "Paid"
        doc.payment_status = "Paid"
        # Canonicalise the paid amount (2026-08 fix): approval flows that
        # never stamp approved_amount left it at 0 — the review-line sum and
        # the Additional Salary deduction then disagreed with the payout.
        # Paying out means the REQUESTED amount was disbursed and approved.
        try:
            if float(doc.approved_amount or 0) <= 0:
                doc.approved_amount = doc.requested_amount
        except (TypeError, ValueError):
            doc.approved_amount = doc.requested_amount
    if payment_entry:
        doc.linked_payment_entry = payment_entry
    doc.save()  # on_update → _create_advance_deduction
    if not already_paid:
        audit_api.log(
            "Advance Approve",
            doc=doc.as_dict(),
            work_date=doc.posting_date,
            description="Approved → Paid",
            old_value="Approved",
            new_value="Paid",
        )

    additional = frappe.db.get_value(DOCTYPE, name, "linked_additional_salary") or ""
    return {
        "name": name,
        "status": doc.workflow_state,
        "additional_salary": additional,
        "payment_status": "Paid",
        "message": (
            _("Yêu cầu {0} đã ghi nhận thanh toán.").format(name)
            if not already_paid
            else _("Yêu cầu {0} đã được ghi nhận thanh toán trước đó.").format(name)
        ),
    }


@frappe.whitelist()
def reverse_advance_payment(
    name: str | None = None,
    cancel_request: bool = False,
) -> dict:
    """Plan §23 — undo a ``Paid`` salary advance.

    HR/Payroll manager only. Cancels the linked ERPNext ``Additional Salary``
    deduction (via the DocType ``on_update``-driven reversal helper) so the
    amount no longer flows onto the Salary Slip, clears the link fields and
    resets ``payment_status`` to ``Unpaid``.

    By default the request is returned to ``Approved`` (so it can be re-paid or
    re-routed). Pass ``cancel_request=True`` to fully cancel the request — the
    DocType ``on_cancel`` hook then ensures the deduction is cancelled too.

    Returns ``{ name, status, additional_salary, payment_status, message }``.
    """
    if not name:
        frappe.throw(_("Thiếu mã yêu cầu ứng lương."))
    if not _is_manager():
        frappe.throw(
            _("Chỉ HR/Payroll Manager mới có thể hoàn trả ứng lương."),
            frappe.PermissionError,
        )

    doc = frappe.get_doc(DOCTYPE, name)
    if doc.docstatus >= 2:
        frappe.throw(_("Yêu cầu này đã bị hủy."))
    if doc.workflow_state != "Paid":
        frappe.throw(_("Chỉ yêu cầu ở trạng thái Paid mới có thể hoàn trả."))

    # Cancel the linked deduction first (best-effort, never aborts the flow).
    # force=True: the doc is still in the Paid state here, but an explicit HR
    # reversal must cancel the deduction NOW (2026-08-20 fix — the pure
    # leave-Paid predicate let the Additional Salary row survive and keep
    # hitting the Salary Slip).
    reversed_deduction = doc._reverse_advance_deduction(force=True)
    if reversed_deduction:
        # The reversal helper wrote link-field resets straight to the DB —
        # reload so the state transition below saves against fresh timestamps
        # instead of raising TimestampMismatchError.
        doc.reload()

    if cancel_request:
        # Full cancel: the on_cancel hook re-runs the (now idempotent) reversal.
        doc.cancel()
        status = "Cancelled"
    else:
        # Return to Approved so the lifecycle can resume.
        doc.workflow_state = "Approved"
        doc.payment_status = "Unpaid"
        doc.save()
        status = doc.workflow_state

    additional = frappe.db.get_value(DOCTYPE, name, "linked_additional_salary") or ""
    return {
        "name": name,
        "status": status,
        "additional_salary": additional,
        "payment_status": "Unpaid",
        "reversed": reversed_deduction,
        "message": (
            _("Đã hoàn trả ứng lương {0}.").format(name)
            if reversed_deduction
            else _("Đã đặt lại trạng thái {0} (không có khoản khấu trừ để hủy).").format(name)
        ),
    }
