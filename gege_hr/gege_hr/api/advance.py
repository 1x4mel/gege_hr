"""
Salary Advance Request API — plan v5 §10.5 / doctype-design §23.

Milestone-2 read + submit endpoints fronting the VN Salary Advance Request
DocType. They map 1:1 to the frontend ``gege_hr.gege_hr.api.advance.<fn>`` calls
in ``hr-ui/src/api/index.js``:

  * ``my_advance_requests``   — the caller's own advance requests (date window,
    status + amount-range filters, search, pagination)
  * ``all_advance_requests``  — manager list across all employees (status/date/
    employee/payment filters, search, pagination + summary)
  * ``preview_eligibility``   — policy-driven cap preview with structured reasons
  * ``submit_advance_request``— create a Draft advance request (validation,
    eligible-amount calc + naming handled by the DocType)
  * ``get_advance_request``   — full detail payload (timeline + comments +
    attachments + linked docs + action flags) for the SPA detail view
  * ``update_advance_request``— edit amount/reason/posting_date of an own
    Draft/Pending request (validate re-runs server-side)
  * ``resubmit_advance_request``— clone a Rejected request into a fresh one
  * ``cancel_advance_request``— revoke a Draft/Pending request
  * ``add_advance_comment``   — append a Comment row on the request
  * ``mark_paid``             — record an approved advance as paid (materialises
    the Additional Salary deduction via the DocType ``on_update`` hook)
  * ``reverse_advance_payment``— undo a Paid advance: cancel its Additional
    Salary deduction and return it to Approved (or Cancelled)
  * ``export_advance_csv``    — manager-only CSV export of the filtered list

The lifecycle is workflow-driven (Draft → Pending Manager → Pending HR →
Approved → Paid / Rejected / Cancelled) and is routed by the unified approval
inbox (:mod:`gege_hr.gege_hr.api.approval`) via a VN Approval Matrix with
``transaction_type = Salary Advance Request``. Every mutation publishes a
``advance_updated`` realtime ping (plans/advance-deskfree-complete.md §2.10).
"""

from __future__ import annotations

import csv
import io

import frappe
from frappe import _
from frappe.utils import getdate

from gege_hr.gege_hr.api import audit as audit_api
from gege_hr.gege_hr.utils import employee as emp_utils, pagination
from gege_hr.gege_hr.utils.advance import REPAYMENT_PLAN_NEXT_MONTH
from gege_hr.gege_hr.utils.request_workflow import send_for_approval

DOCTYPE = "VN Salary Advance Request"
POLICY_DOCTYPE = "VN Salary Advance Policy"

# Realtime channel for open /hr/advance tabs (plans/advance-deskfree §2.10).
_REALTIME_EVENT = "advance_updated"

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

# Detail payload fields — everything the SPA detail page renders, incl. the
# linked-doc references + provenance (track_changes parity without the Desk).
_DETAIL_FIELDS = _LIST_FIELDS + [
    "linked_additional_salary",
    "linked_payment_entry",
    "owner",
    "created_by",
    "modified_by",
    "creation",
    "modified",
]

# workflow_states where the requester may still edit their own request.
_EDITABLE_STATES = ("Draft", "Pending Manager", "Pending HR")


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
    """Best-effort eligible-amount preview for the SPA form (plan §22 / §2.7).

    Resolves the best-matching active policy for the employee and computes the
    cap via :func:`compute_eligible_amount`. Returns the structured shape
    ``{ policy, policy_details, eligible_amount, within_limit, reasons }`` where
    ``reasons`` is a list of ``{code, message, ok}`` checks (policy found /
    within cap / before cutoff / monthly quota left) so the SPA renders WHY an
    amount is allowed or blocked without re-implementing the rules. Any bench
    lookup failure degrades to a permissive empty preview so the SPA form never
    hard-blocks on a missing policy.
    """
    from gege_hr.gege_hr.utils.advance import (
        compute_eligible_amount,
        is_past_cutoff,
        pick_advance_policy,
    )

    def _permissive(reasons=None):
        return {
            "policy": None,
            "policy_details": None,
            "eligible_amount": 0,
            "within_limit": True,
            "quota_left": None,
            "reasons": reasons or [],
        }

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
                "is_active",
                "modified",
            ],
        )
    except Exception:
        return _permissive()

    policy = pick_advance_policy(rows, attrs)
    if not policy:
        return _permissive(
            [
                {
                    "code": "no_policy",
                    "message": _("Chưa cấu hình chính sách ứng lương — không giới hạn."),
                    "ok": True,
                }
            ]
        )

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
    within = eligible <= 0 or req <= eligible + 0.01

    reasons: list[dict] = [
        {
            "code": "policy",
            "message": _("Chính sách: {0}").format(policy.get("name")),
            "ok": True,
        },
        {
            "code": "limit",
            "message": (
                _("Hạn mức ứng tối đa: {0} đ").format(f"{eligible:,.0f}")
                if eligible > 0
                else _("Chưa xác định được hạn mức (thiếu lương cơ bản).")
            ),
            "ok": within,
        },
    ]

    # Cutoff advisory (against today — the submit validate re-checks the real
    # posting_date server-side, this is a form-time hint only).
    cutoff = policy.get("cutoff_day")
    try:
        past_cutoff = bool(cutoff and is_past_cutoff(getdate(), cutoff))
    except Exception:
        past_cutoff = False
    if cutoff:
        reasons.append(
            {
                "code": "cutoff",
                "message": (
                    _("Đã quá ngày cutoff ({0}) trong tháng.").format(cutoff)
                    if past_cutoff
                    else _("Còn hạn trước cutoff ngày {0}.").format(cutoff)
                ),
                "ok": not past_cutoff,
            }
        )

    # Monthly quota advisory (read-only count of this month's live requests).
    used = _month_quota_used(employee)
    quota_left = _quota_left(policy, used)
    if quota_left is not None:
        cap = int(policy.get("max_requests_per_month") or 0)
        reasons.append(
            {
                "code": "quota",
                "message": (
                    _("Còn {0}/{1} lượt ứng trong tháng.").format(quota_left, cap)
                    if quota_left > 0
                    else _("Đã hết {0} lượt ứng trong tháng.").format(cap)
                ),
                "ok": quota_left > 0,
            }
        )

    return {
        "policy": policy.get("name"),
        "policy_details": {
            "max_percentage": policy.get("max_percentage"),
            "max_fixed_amount": policy.get("max_fixed_amount"),
            "cutoff_day": cutoff,
            "max_requests_per_month": policy.get("max_requests_per_month"),
        },
        "eligible_amount": eligible,
        "within_limit": within,
        "quota_left": quota_left,
        "reasons": reasons,
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


def _amount_filter_rows(
    rows: list[dict],
    status: str | None,
    min_amount=None,
    max_amount=None,
) -> list[dict]:
    """Post-query status + requested-amount window filter (HR-BL-08 server-side).

    Kept in Python (like :func:`_filter_rows`) so the simple stub-frappe test
    harness and per-DocType column availability stay predictable.
    """

    def _f(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    out = rows
    if (status or "").strip():
        wanted = status.strip()
        out = [r for r in out if (r.get("workflow_state") or "") == wanted]
    if min_amount not in (None, ""):
        lo = _f(min_amount)
        out = [r for r in out if _f(r.get("requested_amount")) >= lo]
    if max_amount not in (None, ""):
        hi = _f(max_amount)
        out = [r for r in out if _f(r.get("requested_amount")) <= hi]
    return out


def _publish_advance(doc=None) -> None:
    """Realtime ping for open ``/hr/advance`` tabs (plan §2.10). Best-effort.

    Broadcasts the doc identity + state so both the employee list and the
    detail page can refresh without F5 — mirrors ``_publish_overtime``.
    """
    try:
        frappe.publish_realtime(
            _REALTIME_EVENT,
            {
                "doctype": DOCTYPE,
                "name": getattr(doc, "name", None) if doc is not None else None,
                "employee": getattr(doc, "employee", None) if doc is not None else None,
                "workflow_state": getattr(doc, "workflow_state", None)
                if doc is not None
                else None,
                "payment_status": getattr(doc, "payment_status", None)
                if doc is not None
                else None,
            },
        )
    except Exception:
        pass


def _month_quota_used(employee: str, exclude_name: str | None = None) -> int:
    """Count this month's live (non-rejected/cancelled) requests — read-only
    twin of ``VNSalaryAdvanceRequest._assert_monthly_quota`` for the preview."""
    try:
        month_start = getdate().replace(day=1)
        return frappe.db.count(
            DOCTYPE,
            filters={
                "employee": employee,
                "workflow_state": ["not in", ["Rejected", "Cancelled"]],
                "docstatus": ["<", 2],
                "posting_date": [">=", month_start],
                "name": ["!=", exclude_name or ""],
            },
        )
    except Exception:
        return 0


def _quota_left(policy: dict | None, used: int) -> int | None:
    """Remaining requests this month, or ``None`` when the policy has no cap."""
    if not policy:
        return None
    try:
        cap = int(policy.get("max_requests_per_month") or 0)
    except (TypeError, ValueError):
        return None
    return max(0, cap - used) if cap > 0 else None


@frappe.whitelist()
def my_advance_requests(
    employee: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    status: str | None = None,
    min_amount=None,
    max_amount=None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 0,
) -> list[dict] | dict:
    """Plan §10.5 — the caller's advance requests, optionally narrowed by date.

    ``search`` OR-matches a free-text query across the row's text fields
    (name / reason / posting_date / employee / employee_name / status),
    applied server-side (DNA §6.6 D, HR-BL-08). ``status`` narrows the
    workflow_state and ``min_amount``/``max_amount`` bound the requested
    amount — both server-side too (retires the client-side HR-BL-08
    workaround in AdvanceView). Managers (HR Manager/System Manager) may
    pass any ``employee``; a plain Employee is scoped to own.

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
    filtered = _amount_filter_rows(filtered, status, min_amount, max_amount)
    return pagination.paginate_filtered(filtered, page=page, page_size=page_size)


@frappe.whitelist()
def all_advance_requests(
    status: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    employee: str | None = None,
    payment_status: str | None = None,
    search: str | None = None,
    page: int = 1,
    page_size: int = 0,
) -> list[dict] | dict:
    """Manager-only (HR/Manager) list of *every* salary advance request across
    all employees — drives the SPA "Mark Paid / Reverse" admin panel.

    Optional filters: ``status`` (workflow_state, e.g. ``Approved``/``Paid``),
    a ``posting_date`` date window, ``employee``, ``payment_status`` and a
    free-text ``search`` (name / reason / employee / employee_name). Plain
    Employees are rejected.

    Pagination is opt-in like :func:`my_advance_requests`; when active the
    envelope carries a ``summary`` aggregate (``count`` / ``total_requested``
    / ``total_approved``) computed over the FULL filtered set so the panel's
    summary tiles stay correct under paging.
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
    if employee:
        filters["employee"] = employee
    if payment_status:
        filters["payment_status"] = payment_status

    rows = frappe.db.get_all(
        DOCTYPE,
        filters=filters,
        fields=_LIST_FIELDS,
        order_by="posting_date desc, creation desc",
        limit_page_length=pagination.MAX_PAGE_SIZE * 10,
    )
    filtered = _filter_rows(
        rows,
        search,
        ("name", "reason", "posting_date", "employee", "employee_name", "status"),
    )

    def _f(v):
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0

    summary = None
    if page_size:
        summary = {
            "count": len(filtered),
            "total_requested": round(sum(_f(r.get("requested_amount")) for r in filtered), 2),
            "total_approved": round(sum(_f(r.get("approved_amount")) for r in filtered), 2),
        }
    return pagination.paginate_filtered(
        filtered, page=page, page_size=page_size, summary=summary
    )


@frappe.whitelist()
def preview_eligibility(
    employee: str | None = None,
    requested_amount=None,
) -> dict:
    """Plan §10.5 — eligible-amount preview for the SPA form (no row created).

    Returns the structured shape (plan §2.7): ``{ policy, policy_details,
    eligible_amount, within_limit, reasons: [{code, message, ok}] }`` so the
    submit modal renders WHY an amount is allowed/blocked (policy found /
    within cap / before cutoff / monthly quota left) without client-side
    re-implementation of the rules.
    """
    emp = _resolve(employee)
    _assert_own(emp)
    return _preview_eligibility(emp, requested_amount)


def _create_and_route(
    *,
    employee: str,
    posting_date,
    requested_amount,
    reason: str,
    salary_advance_policy: str | None = None,
    payroll_period: str | None = None,
) -> object:
    """Shared creation path for submit + resubmit (plan §2.3).

    Inserts a Draft request, routes it into the approval pipeline and stamps
    the audit row. Returns the inserted Document.
    """
    doc = frappe.new_doc(DOCTYPE)
    doc.update(
        {
            "employee": employee,
            "posting_date": getdate(posting_date) if posting_date else None,
            "requested_amount": requested_amount,
            "reason": reason or "",
            # Single repayment method (2026-08 rule): always Next Month —
            # deduct from the payroll period containing the posting_date,
            # disbursed the following month. Any client-sent value is ignored.
            "repayment_plan": REPAYMENT_PLAN_NEXT_MONTH,
            "workflow_state": "Draft",
            "docstatus": 0,
        }
    )
    if salary_advance_policy:
        doc.salary_advance_policy = salary_advance_policy
    if payroll_period:
        doc.payroll_period = payroll_period

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
    _publish_advance(doc)
    return doc


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

    doc = _create_and_route(
        employee=emp,
        posting_date=kwargs.get("posting_date"),
        requested_amount=kwargs.get("requested_amount"),
        reason=kwargs.get("reason") or "",
        salary_advance_policy=kwargs.get("salary_advance_policy"),
        payroll_period=kwargs.get("payroll_period"),
    )
    return {
        "name": doc.name,
        "status": doc.workflow_state,
        "eligible_amount": doc.eligible_amount,
        "message": _("Đã tạo yêu cầu ứng lương {0}.").format(doc.name),
    }


# --------------------------------------------------------------------------- #
# Detail / edit / resubmit / comment — plans/advance-deskfree-complete.md §2
# --------------------------------------------------------------------------- #
def _timeline(name: str) -> dict:
    """Approval-log + audit-event rows for the detail view (best-effort)."""
    approval_logs: list[dict] = []
    audit_events: list[dict] = []
    try:
        if frappe.db.table_exists("VN Approval Log"):  # type: ignore[attr-defined]
            approval_logs = frappe.db.get_all(
                "VN Approval Log",
                filters={"reference_doctype": DOCTYPE, "reference_name": name},
                fields=[
                    "name",
                    "action",
                    "from_state",
                    "to_state",
                    "actor",
                    "comment",
                    "action_at",
                ],
                order_by="action_at desc",
                limit_page_length=100,
            )
    except Exception:
        approval_logs = []
    try:
        if frappe.db.table_exists("VN Audit Event"):  # type: ignore[attr-defined]
            audit_events = frappe.db.get_all(
                "VN Audit Event",
                filters={"reference_doctype": DOCTYPE, "reference_name": name},
                fields=[
                    "name",
                    "audit_type",
                    "actor",
                    "description",
                    "old_value",
                    "new_value",
                    "created_at",
                ],
                order_by="created_at desc",
                limit_page_length=100,
            )
    except Exception:
        audit_events = []
    return {"approval_logs": approval_logs, "audit": audit_events}


def _comments(name: str) -> list[dict]:
    """Comment thread on the request (Frappe Comment doctype, best-effort)."""
    try:
        return frappe.db.get_all(
            "Comment",
            filters={
                "comment_type": "Comment",
                "reference_doctype": DOCTYPE,
                "reference_name": name,
            },
            fields=["name", "owner", "content", "creation"],
            order_by="creation asc",
            limit_page_length=200,
        )
    except Exception:
        return []


def _attachments(name: str) -> list[dict]:
    """Files attached to the request (Frappe File doctype, best-effort)."""
    try:
        return frappe.db.get_all(
            "File",
            filters={"attached_to_doctype": DOCTYPE, "attached_to_name": name},
            fields=["name", "file_name", "file_url", "file_size", "owner", "creation"],
            order_by="creation asc",
            limit_page_length=100,
        )
    except Exception:
        return []


def _linked_meta(doc) -> dict:
    """Live docstatus of the linked Additional Salary / Payment Entry."""
    out = {
        "additional_salary_status": None,
        "additional_salary_docstatus": None,
        "payment_entry_status": None,
        "payment_entry_docstatus": None,
    }
    try:
        ad = (getattr(doc, "linked_additional_salary", "") or "").strip()
        if ad:
            out["additional_salary_status"] = frappe.db.get_value(
                "Additional Salary", ad, "status"
            )
            out["additional_salary_docstatus"] = frappe.db.get_value(
                "Additional Salary", ad, "docstatus"
            )
    except Exception:
        pass
    try:
        pe = (getattr(doc, "linked_payment_entry", "") or "").strip()
        if pe:
            out["payment_entry_status"] = frappe.db.get_value(
                "Payment Entry", pe, "status"
            )
            out["payment_entry_docstatus"] = frappe.db.get_value(
                "Payment Entry", pe, "docstatus"
            )
    except Exception:
        pass
    return out


def _can_flags(doc) -> dict:
    """Server-computed action matrix for the detail view (plan §2.1)."""
    state = getattr(doc, "workflow_state", "") or ""
    docstatus = getattr(doc, "docstatus", 0) or 0
    manager = _is_manager()
    pending = docstatus == 0 and state in _EDITABLE_STATES
    return {
        "edit": pending,
        "cancel": pending,
        "mark_paid": manager and state == "Approved" and docstatus < 2,
        "reverse": manager and state == "Paid" and docstatus < 2,
        "resubmit": state in ("Rejected", "Cancelled") and docstatus < 2,
    }


@frappe.whitelist()
def get_advance_request(name: str | None = None) -> dict:
    """Full detail payload for the SPA detail page (plan §2.1).

    Returns ``{ doc, timeline: {approval_logs, audit}, comments, attachments,
    linked, can }``. The caller must be the owner or an HR/Manager.
    """
    if not name:
        frappe.throw(_("Thiếu mã yêu cầu ứng lương."))
    doc = frappe.get_doc(DOCTYPE, name)
    _assert_own(doc.employee)
    payload = {f: doc.get(f) for f in _DETAIL_FIELDS}
    return {
        "doc": payload,
        "timeline": _timeline(name),
        "comments": _comments(name),
        "attachments": _attachments(name),
        "linked": _linked_meta(doc),
        "can": _can_flags(doc),
    }


@frappe.whitelist()
def update_advance_request(
    name: str | None = None,
    requested_amount=None,
    reason: str | None = None,
    posting_date=None,
) -> dict:
    """Edit an own Draft/Pending advance request (plan §2.2).

    Only ``requested_amount`` / ``reason`` / ``posting_date`` are editable —
    employee, policy and repayment plan are immutable by design (2026-08
    single-method rule). ``doc.save()`` re-runs ``validate()`` so the eligible
    cap, monthly quota (the count excludes the row itself) and cutoff are all
    re-checked server-side. Audit-stamped + realtime-published.
    """
    if not name:
        frappe.throw(_("Thiếu mã yêu cầu ứng lương."))
    doc = frappe.get_doc(DOCTYPE, name)
    _assert_own(doc.employee)
    if doc.docstatus != 0 or (doc.workflow_state or "") not in _EDITABLE_STATES:
        frappe.throw(_("Chỉ yêu cầu đang chờ duyệt mới sửa được."))

    old_amount = doc.requested_amount
    old_reason = doc.reason or ""
    old_date = doc.posting_date
    changed = False
    if requested_amount not in (None, "") and str(requested_amount) != str(
        doc.requested_amount
    ):
        doc.requested_amount = requested_amount
        changed = True
    if reason is not None and (reason or "") != old_reason:
        doc.reason = reason or ""
        changed = True
    if posting_date not in (None, ""):
        try:
            new_date = getdate(posting_date)
        except Exception:
            frappe.throw(_("Ngày yêu cầu không hợp lệ."))
        if str(new_date) != str(old_date or ""):
            doc.posting_date = new_date
            changed = True
    if not changed:
        frappe.throw(_("Không có thay đổi để cập nhật."))

    doc.save()
    audit_api.log(
        "Advance Update",
        doc=doc.as_dict(),
        work_date=doc.posting_date,
        description=f"Cập nhật yêu cầu {name}",
        old_value={"requested_amount": old_amount, "posting_date": str(old_date or "")},
        new_value={"requested_amount": doc.requested_amount, "posting_date": str(doc.posting_date or "")},
    )
    _publish_advance(doc)
    return {
        "name": name,
        "status": doc.workflow_state,
        "requested_amount": doc.requested_amount,
        "message": _("Đã cập nhật yêu cầu ứng lương {0}.").format(name),
    }


@frappe.whitelist()
def resubmit_advance_request(name: str | None = None) -> dict:
    """Clone a Rejected request into a fresh pending one (plan §2.3).

    The original row is left untouched (audit history preserved); a NEW request
    is created via the same :func:`_create_and_route` path as a first submit —
    posting_date defaults to today so cutoff/quota apply afresh.
    """
    if not name:
        frappe.throw(_("Thiếu mã yêu cầu ứng lương."))
    doc = frappe.get_doc(DOCTYPE, name)
    _assert_own(doc.employee)
    if (doc.workflow_state or "") not in ("Rejected", "Cancelled") or doc.docstatus >= 2:
        frappe.throw(_("Chỉ yêu cầu đã từ chối mới có thể tạo lại."))

    new_doc = _create_and_route(
        employee=doc.employee,
        posting_date=getdate(),
        requested_amount=doc.requested_amount,
        reason=doc.reason or "",
        salary_advance_policy=doc.salary_advance_policy,
        payroll_period=doc.payroll_period,
    )
    return {
        "name": new_doc.name,
        "status": new_doc.workflow_state,
        "original": name,
        "eligible_amount": new_doc.eligible_amount,
        "message": _("Đã tạo lại yêu cầu ứng lương {0} từ {1}.").format(new_doc.name, name),
    }


@frappe.whitelist()
def add_advance_comment(name: str | None = None, comment: str | None = None) -> dict:
    """Append a Comment row on the request (plan §2.8).

    The owner and HR/Managers may comment. ``ignore_permissions`` after the
    own-check because the Employee role has no create perm on Comment.
    """
    if not name:
        frappe.throw(_("Thiếu mã yêu cầu ứng lương."))
    if not (comment or "").strip():
        frappe.throw(_("Nội dung bình luận không được để trống."))
    doc = frappe.get_doc(DOCTYPE, name)
    _assert_own(doc.employee)
    row = frappe.new_doc("Comment")
    row.update(
        {
            "comment_type": "Comment",
            "reference_doctype": DOCTYPE,
            "reference_name": name,
            "content": comment.strip(),
        }
    )
    row.insert(ignore_permissions=True)
    _publish_advance(doc)
    return {
        "name": row.name,
        "owner": row.owner,
        "content": row.content,
        "message": _("Đã gửi bình luận."),
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
    _publish_advance(doc)
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
    _publish_advance(doc)

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
    _publish_advance(doc)
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


# --------------------------------------------------------------------------- #
# CSV export — plans/advance-deskfree-complete.md §2.9
# --------------------------------------------------------------------------- #
_EXPORT_MAX_ROWS = 2000


def _build_advance_csv(rows: list[dict]) -> str:
    """Excel-safe CSV (UTF-8 BOM) of the filtered advance list."""
    header = [
        "Mã yêu cầu",
        "Mã nhân viên",
        "Tên nhân viên",
        "Ngày yêu cầu",
        "Số tiền xin",
        "Hạn mức",
        "Số tiền duyệt",
        "Trạng thái",
        "Thanh toán",
        "Lý do",
    ]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for r in rows:
        writer.writerow(
            [
                r.get("name"),
                r.get("employee"),
                r.get("employee_name"),
                str(r.get("posting_date") or ""),
                r.get("requested_amount"),
                r.get("eligible_amount"),
                r.get("approved_amount"),
                r.get("workflow_state"),
                r.get("payment_status"),
                (r.get("reason") or "").replace("\n", " "),
            ]
        )
    return "\ufeff" + buf.getvalue()


@frappe.whitelist()
def export_advance_csv(
    status: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    employee: str | None = None,
    payment_status: str | None = None,
    search: str | None = None,
    download: int = 0,
) -> dict:
    """Manager-only CSV export of the filtered advance list (plan §2.9).

    Same filter contract as :func:`all_advance_requests` (minus pagination).
    Returns ``{filename, content, rows, truncated}``; with ``download=1`` the
    response is switched to a binary file download.
    """
    if not _is_manager():
        frappe.throw(
            _("Bạn không có quyền xuất danh sách ứng lương."),
            frappe.PermissionError,
        )

    filters = {}
    if status:
        filters["workflow_state"] = status
    if from_date or to_date:
        filters["posting_date"] = ["between", [from_date or to_date, to_date or from_date]]
    if employee:
        filters["employee"] = employee
    if payment_status:
        filters["payment_status"] = payment_status

    rows = frappe.db.get_all(
        DOCTYPE,
        filters=filters,
        fields=_LIST_FIELDS,
        order_by="posting_date desc, creation desc",
        limit_page_length=_EXPORT_MAX_ROWS + 1,  # +1 detects the truncation
    )
    truncated = len(rows) > _EXPORT_MAX_ROWS
    rows = rows[:_EXPORT_MAX_ROWS]
    rows = _filter_rows(
        rows,
        search,
        ("name", "reason", "posting_date", "employee", "employee_name", "status"),
    )

    # The export itself is a sensitive read — stamp it (best-effort).
    try:
        audit_api.log(
            "Manual Override",
            company=(rows[0].get("company") if rows else None),
            description=_("Xuất CSV ứng lương: {0} dòng").format(len(rows)),
            new_value={
                "filters": {k: v for k, v in filters.items()},
                "rows": len(rows),
                "truncated": truncated,
            },
        )
    except Exception:
        frappe.log_error(title="advance.export stamp failed")

    csv_text = _build_advance_csv(rows)
    filename = f"advance_export_{getdate().isoformat()}.csv"
    if download:
        frappe.response.filename = filename
        frappe.response.filecontent = csv_text.encode("utf-8")
        frappe.response.type = "binary"
        return {"rows": len(rows), "truncated": truncated}
    return {
        "filename": filename,
        "content": csv_text,
        "rows": len(rows),
        "truncated": truncated,
    }
