"""Expense Claim API — NEW-1 (hr-gap-audit 🟥) + plans/plan-expense-desk-free.md.

Reuses Frappe HR's ``Expense Claim`` DocType (no duplicate doctype) and exposes a
server-side, DNA-compliant surface to the portal. Maps 1:1 to the frontend
``gege_hr.gege_hr.api.expense.<fn>`` calls in ``trader-ui/src/hr/api/index.js``:

  * ``my_expense_claims``     — the caller's own claims (status/employee/date/
    amount filters, broad search, pagination)
  * ``all_expense_claims``    — manager list across all employees (+ summary)
  * ``expense_claim_options`` — active Expense Claim Types + resolved approver
  * ``submit_expense_claim``  — create a Draft claim chờ duyệt (draft-first: the
    old auto-``submit()`` always failed because hrms ``on_submit`` throws while
    ``approval_status == Draft`` — the "zombie draft" bug)
  * ``get_expense_claim``     — full detail payload (timeline + comments +
    attachments + linked + action flags) for the SPA detail view
  * ``update_expense_claim``  — edit rows/date/remark of an own Draft/Rejected
    claim (Rejected resets to Draft so HR can re-approve)
  * ``cancel_expense_claim``  — delete a Draft, or ``doc.cancel()`` a submitted
    claim (manager — reverses GL via the hrms hook)
  * ``amend_expense_claim``   — clone a Cancelled claim via ``amended_from``
  * ``approve_expense_claim`` — approve with per-line ``sanction_amount`` (≤
    claimed), then submit (graceful degrade when accounts are not configured)
  * ``reject_expense_claim``  — reject with a MANDATORY reason (Comment-logged,
    the employee's ``remark`` is never overwritten)
  * ``mark_expense_paid``     — record payout: ``is_paid`` + mode + clearance
  * ``add_expense_comment``   — append a Comment row on the claim
  * ``export_expense_csv``    — manager-only CSV of the filtered list
  * ``upload_expense_attachment`` — fallback receipt upload when the native
    ``frappe.handler.upload_file`` denies an ``if_owner`` Employee

Lifecycle (hrms): Draft (docstatus 0) → Approved via approve → submit (docstatus
1, GL) → Paid via mark_paid; Cancelled (docstatus 2) → amend. Every mutation
publishes an ``expense_updated`` realtime ping and best-effort audit/notify rows.
"""

from __future__ import annotations

import csv
import io
import json

import frappe

from gege_hr.gege_hr.utils import notify, pagination

CLAIM_DOCTYPE = "Expense Claim"

# Realtime channel for open /hr/expense tabs (plan §2.12).
_REALTIME_EVENT = "expense_updated"

_EXPORT_MAX_ROWS = 10000  # plan §2.10 — CSV safety cap

# Row shape returned to the SPA list — kept stable/flat.
_CLAIM_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "posting_date",
    "total_claimed_amount",
    "total_sanctioned_amount",
    "total_amount_reimbursed",
    "approval_status",
    "status",
    "remark",
    "company",
    "expense_approver",
    "is_paid",
    "docstatus",
]

# Detail payload extras (plan §2.1) — everything the detail page renders.
_DETAIL_EXTRA = [
    "mode_of_payment",
    "clearance_date",
    "grand_total",
    "total_taxes_and_charges",
    "total_advance_amount",
    "amended_from",
    "owner",
    "modified_by",
    "creation",
    "modified",
]

# Child-table keys mirrored into the detail payload (read-only for the SPA).
_EXPENSE_ROW_KEYS = (
    "expense_type",
    "amount",
    "sanction_amount",
    "description",
)
_TAX_ROW_KEYS = ("account_head", "tax_amount", "description")
_ADVANCE_ROW_KEYS = ("employee_advance", "advance_amount", "allocated_amount")


def _num(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _today() -> str:
    try:
        return frappe.utils.today()
    except Exception:
        return ""


def _resolve(employee: str | None) -> str:
    if employee:
        return employee
    emp = frappe.db.get_value("Employee", {"user_id": frappe.session.user})
    if not emp:
        frappe.throw("Tài khoản chưa liên kết nhân viên.")
    return emp


def _is_manager() -> bool:
    roles = set(frappe.get_roles(frappe.session.user))
    return bool(roles & {"HR Manager", "HR User", "System Manager", "Expense Claim Approver"})


def _is_own(employee: str) -> bool:
    own = frappe.db.get_value("Employee", {"user_id": frappe.session.user})
    return employee == own


def _assert_own(employee: str) -> None:
    if _is_manager():
        return
    if not _is_own(employee):
        frappe.throw("Bạn chỉ xem được chi phí của chính mình.")


def normalize_expenses(expenses) -> list:
    """Coerce the FE ``expenses`` child payload into Expense Claim Detail rows."""
    out = []
    for e in expenses or []:
        amount = _num(e.get("amount") if isinstance(e, dict) else getattr(e, "amount", None))
        if amount <= 0:
            continue
        etype = (e.get("expense_type") if isinstance(e, dict) else getattr(e, "expense_type", None)) or ""
        out.append(
            {
                "expense_type": etype,
                "amount": amount,
                "sanction_amount": amount,
                "description": (
                    e.get("description") if isinstance(e, dict) else getattr(e, "description", "")
                )
                or "",
            }
        )
    return out


def claim_total(expenses) -> float:
    return round(sum(_num(e.get("amount")) for e in normalize_expenses(expenses)), 2)


def resolve_sanctions(rows, sanctions=None) -> list:
    """Per-line sanctioned amounts aligned with ``rows`` (pure, unit-tested).

    ``sanctions`` items may carry ``idx`` (0-based) to target a row; rows without
    an explicit sanction default to their full ``amount``. Raises via
    ``frappe.throw`` on out-of-range rows or 0 ≤ sanction ≤ amount violations.
    """
    amounts = [
        _num(r.get("amount") if isinstance(r, dict) else getattr(r, "amount", None))
        for r in rows or []
    ]
    out = list(amounts)
    by_idx: dict[int, float] = {}
    for i, item in enumerate(sanctions or []):
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("idx", i))
        except (TypeError, ValueError):
            idx = i
        by_idx[idx] = _num(item.get("sanction_amount"))
    for idx, s in by_idx.items():
        if idx < 0 or idx >= len(out):
            frappe.throw(f"Dòng chi phí {idx + 1} không tồn tại.")
        if s < 0 or s > amounts[idx] + 1e-9:
            frappe.throw(f"Số duyệt dòng {idx + 1} không hợp lệ (0 … {amounts[idx]}).")
        out[idx] = s
    return out


def _resolve_approver(employee: str, explicit: str | None = None) -> str | None:
    """Resolve the claim approver (plan §2.2): explicit param → Employee's
    ``expense_approver`` → any HR Manager user. NEVER the session user blindly —
    that violated ``prevent_self_expense_approval`` in the old code."""
    if (explicit or "").strip():
        return explicit.strip()
    try:
        approver = frappe.db.get_value("Employee", employee, "expense_approver")
    except Exception:
        approver = None
    if approver:
        return approver
    try:
        users = (
            frappe.get_all(
                "Has Role",
                filters={"role": "HR Manager", "parenttype": "User"},
                pluck="parent",
                limit_page_length=5,
            )
            or []
        )
        for u in users:
            if u not in ("Administrator", "Guest"):
                return u
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------- #
# Side-effect helpers (audit / notify / comment / realtime) — all best-effort
# --------------------------------------------------------------------------- #
def _audit(audit_type: str, doc, description=None, old_value=None, new_value=None) -> None:
    # Lazy import: api/audit.py pulls ``frappe.utils`` at import time which the
    # bench-free stub harness does not provide (utils/notify.py guards itself).
    try:
        from gege_hr.gege_hr.api import audit as audit_api
    except Exception:
        return
    try:
        d = doc.as_dict() if hasattr(doc, "as_dict") else dict(getattr(doc, "__dict__", {}) or {})
    except Exception:
        d = {}
    try:
        audit_api.log(
            audit_type,
            doc=d,
            work_date=getattr(doc, "posting_date", None),
            description=description,
            old_value=old_value,
            new_value=new_value,
        )
    except Exception:
        pass


def _add_comment(name, content) -> object | None:
    try:
        row = frappe.new_doc("Comment")
        row.update(
            {
                "comment_type": "Comment",
                "reference_doctype": CLAIM_DOCTYPE,
                "reference_name": name,
                "content": content,
            }
        )
        row.insert(ignore_permissions=True)
        return row
    except Exception:
        return None


def _notify(doc, outcome: str, state: str | None = None) -> None:
    try:
        notify.push_request_outcome(
            transaction_type=CLAIM_DOCTYPE,
            name=getattr(doc, "name", None),
            employee=getattr(doc, "employee", None),
            outcome=outcome,
            state=state,
            action_url=f"/hr/expense/{getattr(doc, 'name', '')}",
        )
    except Exception:
        pass


def _publish_expense(doc=None) -> None:
    """Realtime ping for open ``/hr/expense`` tabs (plan §2.12). Best-effort."""
    try:
        frappe.publish_realtime(
            _REALTIME_EVENT,
            {
                "doctype": CLAIM_DOCTYPE,
                "name": getattr(doc, "name", None),
                "employee": getattr(doc, "employee", None),
                "approval_status": getattr(doc, "approval_status", None),
                "docstatus": getattr(doc, "docstatus", None),
                "status": getattr(doc, "status", None),
            },
        )
    except Exception:
        pass


def _doc_set(doc, field: str, value) -> None:
    """Set a field on a possibly-submitted doc: prefer ``db_set`` (permlevel /
    allow_on_submit safe), fall back to attr + save (stub benches)."""
    try:
        doc.db_set(field, value, update_modified=True)
        return
    except Exception:
        pass
    try:
        setattr(doc, field, value)
        doc.save()
    except Exception:
        setattr(doc, field, value)


def _get_doc_or_throw(name):
    if not name:
        frappe.throw("Thiếu mã phiếu chi phí.")
    doc = frappe.get_doc(CLAIM_DOCTYPE, name)
    if not doc:
        frappe.throw("Không tìm thấy phiếu chi phí.")
    return doc


def _table_exists(table: str) -> bool:
    try:
        return bool(frappe.db.table_exists(table))
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# List endpoints
# --------------------------------------------------------------------------- #
def _claim_filters(
    status=None,
    search=None,
    employee_name=None,
    date_from=None,
    date_to=None,
    amount_min=None,
    amount_max=None,
):
    """Shared filter builder for the list + CSV export (plan §2.10)."""
    flt: list = []
    if status:
        flt.append(["approval_status", "=", status])
    ename = (employee_name or "").strip()
    if ename:
        flt.append(["employee_name", "like", f"%{ename}%"])
    if date_from:
        flt.append(["posting_date", ">=", date_from])
    if date_to:
        flt.append(["posting_date", "<=", date_to])
    amin = _num(amount_min, None) if amount_min not in (None, "") else None
    amax = _num(amount_max, None) if amount_max not in (None, "") else None
    if amin is not None:
        flt.append(["total_claimed_amount", ">=", amin])
    if amax is not None:
        flt.append(["total_claimed_amount", "<=", amax])
    or_filters = None
    q = (search or "").strip()
    if q:
        like = f"%{pagination.escape_like(q)}%"
        or_filters = [
            ["employee_name", "like", like],
            ["name", "like", like],
            ["remark", "like", like],
            ["total_claimed_amount", "like", like],
        ]
    return flt, or_filters


@frappe.whitelist()
def my_expense_claims(
    employee: str | None = None,
    status: str | None = None,
    search: str | None = None,
    employee_name: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    amount_min: float | None = None,
    amount_max: float | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    emp = _resolve(employee)
    _assert_own(emp)
    return _list(
        filters=[["employee", "=", emp]],
        status=status,
        search=search,
        employee_name=employee_name,
        date_from=date_from,
        date_to=date_to,
        amount_min=amount_min,
        amount_max=amount_max,
        page=page,
        page_size=page_size,
        with_summary=False,
    )


@frappe.whitelist()
def all_expense_claims(
    status: str | None = None,
    search: str | None = None,
    employee_name: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    amount_min: float | None = None,
    amount_max: float | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager xem tất cả chi phí.")
    return _list(
        filters=None,
        status=status,
        search=search,
        employee_name=employee_name,
        date_from=date_from,
        date_to=date_to,
        amount_min=amount_min,
        amount_max=amount_max,
        page=page,
        page_size=page_size,
        with_summary=True,
    )


def _list(
    filters,
    status,
    search,
    employee_name=None,
    date_from=None,
    date_to=None,
    amount_min=None,
    amount_max=None,
    page=1,
    page_size=20,
    with_summary=False,
) -> dict:
    flt, or_filters = _claim_filters(
        status=status,
        search=search,
        employee_name=employee_name,
        date_from=date_from,
        date_to=date_to,
        amount_min=amount_min,
        amount_max=amount_max,
    )
    flt = list(filters or []) + flt
    page = max(1, int(page or 1))
    page_size = max(1, int(page_size or 20))
    try:
        rows = (
            frappe.get_all(
                CLAIM_DOCTYPE,
                filters=flt or None,
                or_filters=or_filters,
                fields=_CLAIM_FIELDS,
                order_by="posting_date desc",
                limit_start=(page - 1) * page_size,
                limit_page_length=page_size,
            )
            or []
        )
        total = len(
            frappe.get_all(
                CLAIM_DOCTYPE,
                filters=flt or None,
                or_filters=or_filters,
                fields=["name"],
                limit_page_length=0,
            )
            or [],
        )
    except Exception:
        frappe.log_error(title="expense.list failed")
        rows, total = [], 0

    summary = None
    if with_summary and page_size:
        # Aggregate over the FULL filtered set so the panel tiles stay correct
        # under paging (mirrors all_advance_requests, plan §2.10).
        try:
            agg = (
                frappe.get_all(
                    CLAIM_DOCTYPE,
                    filters=flt or None,
                    or_filters=or_filters,
                    fields=[
                        "total_claimed_amount",
                        "total_sanctioned_amount",
                        "total_amount_reimbursed",
                        "status",
                    ],
                    limit_page_length=0,
                )
                or []
            )
            summary = {
                "count": len(agg),
                "total_claimed": round(sum(_num(r.get("total_claimed_amount")) for r in agg), 2),
                "total_sanctioned": round(
                    sum(_num(r.get("total_sanctioned_amount")) for r in agg), 2
                ),
                "total_reimbursed": round(
                    sum(_num(r.get("total_amount_reimbursed")) for r in agg), 2
                ),
                "unpaid_count": sum(1 for r in agg if r.get("status") == "Unpaid"),
            }
        except Exception:
            summary = None
    return {"data": rows, "total": total, "summary": summary}


@frappe.whitelist()
def expense_claim_options() -> dict:
    """Active Expense Claim Types + the resolved approver for the caller (v2).

    ``expense_approver`` is resolved from the caller's Employee row (never the
    session user blindly) so the create form can hint "Người duyệt: X" and the
    HR Settings mandatory/self-approval flags behave as configured.
    """
    try:
        types = frappe.get_all("Expense Claim Type", pluck="name", order_by="name asc") or []
    except Exception:
        types = []
    approver = None
    try:
        emp = frappe.db.get_value("Employee", {"user_id": frappe.session.user})
    except Exception:
        emp = None
    if emp:
        approver = _resolve_approver(emp)
    return {
        "expense_types": types,
        "expense_approver": approver,
        "can_approve": _is_manager(),
    }


# --------------------------------------------------------------------------- #
# Create (draft-first) — plans/plan-expense-desk-free.md §2.2
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def submit_expense_claim(
    employee: str | None = None,
    expenses: list | None = None,
    posting_date: str | None = None,
    remark: str | None = None,
    expense_approver: str | None = None,
) -> dict:
    """Create a Draft claim chờ duyệt (v2 — fixes the "zombie draft" bug).

    The old contract inserted + ``doc.submit()`` in one go, but hrms
    ``on_submit`` throws while ``approval_status == Draft`` so the submit ALWAYS
    failed silently. Now every claim starts as a Draft; HR approves (which
    submits) via :func:`approve_expense_claim`.
    """
    emp = _resolve(employee)
    rows = normalize_expenses(expenses)
    if not rows:
        frappe.throw("Cần ít nhất 1 khoản chi phí hợp lệ (loại + số tiền > 0).")
    total = claim_total(expenses)
    company = frappe.db.get_value("Employee", emp, "company")
    approver = _resolve_approver(emp, explicit=expense_approver)
    doc = frappe.new_doc(CLAIM_DOCTYPE)
    doc.employee = emp
    doc.company = company
    doc.posting_date = posting_date or _today()
    doc.expense_approver = approver or ""
    doc.remark = remark or ""
    doc.total_claimed_amount = total
    doc.total_sanctioned_amount = 0  # sanctioned by the approver, not the submitter
    for r in rows:
        doc.append("expenses", r)
    doc.insert(ignore_permissions=True)
    _audit(
        "Expense Submit",
        doc,
        description=f"Tạo phiếu chi phí {doc.name} (nháp chờ duyệt)",
        new_value={"total_claimed_amount": total, "expense_approver": approver},
    )
    _publish_expense(doc)
    return {
        "name": doc.name,
        "total": total,
        "status": doc.status,
        "approval_status": doc.approval_status,
        "docstatus": getattr(doc, "docstatus", 0) or 0,
        "approver": doc.expense_approver,
        "message": f"Đã gửi phiếu chi phí {doc.name} chờ duyệt.",
    }


# --------------------------------------------------------------------------- #
# Detail / timeline / comments / attachments — plan §2.1
# --------------------------------------------------------------------------- #
def _versions(name: str) -> list:
    """Track-changes (Version) rows summarised for the timeline (best-effort)."""
    rows: list = []
    try:
        for v in (
            frappe.get_all(
                "Version",
                filters={"ref_doctype": CLAIM_DOCTYPE, "docname": name},
                fields=["name", "owner", "creation", "data"],
                order_by="creation desc",
                limit_page_length=100,
            )
            or []
        ):
            changed = []
            try:
                data = json.loads(v.get("data") or "{}")
                changed = [c[0] for c in (data.get("changed") or []) if isinstance(c, (list, tuple))]
            except Exception:
                changed = []
            rows.append(
                {
                    "at": v.get("creation"),
                    "kind": "version",
                    "actor": v.get("owner"),
                    "title": "Cập nhật phiếu",
                    "detail": ", ".join(changed),
                }
            )
    except Exception:
        rows = []
    return rows


def _comments(name: str) -> list:
    try:
        return (
            frappe.get_all(
                "Comment",
                filters={
                    "comment_type": "Comment",
                    "reference_doctype": CLAIM_DOCTYPE,
                    "reference_name": name,
                },
                fields=["name", "owner", "content", "creation"],
                order_by="creation asc",
                limit_page_length=200,
            )
            or []
        )
    except Exception:
        return []


def _attachments(name: str) -> list:
    try:
        return (
            frappe.get_all(
                "File",
                filters={"attached_to_doctype": CLAIM_DOCTYPE, "attached_to_name": name},
                fields=["name", "file_name", "file_url", "file_size", "is_private", "owner", "creation"],
                order_by="creation asc",
                limit_page_length=100,
            )
            or []
        )
    except Exception:
        return []


def _timeline(name: str) -> list:
    """Merged Version + Comment + VN Audit Event feed (creation desc)."""
    items = list(_versions(name))
    for c in _comments(name):
        items.append(
            {
                "at": c.get("creation"),
                "kind": "comment",
                "actor": c.get("owner"),
                "title": "Bình luận",
                "detail": c.get("content"),
            }
        )
    if _table_exists("VN Audit Event"):
        try:
            for a in (
                frappe.get_all(
                    "VN Audit Event",
                    filters={"reference_doctype": CLAIM_DOCTYPE, "reference_name": name},
                    fields=["actor", "description", "created_at", "old_value", "new_value"],
                    order_by="created_at desc",
                    limit_page_length=100,
                )
                or []
            ):
                items.append(
                    {
                        "at": a.get("created_at"),
                        "kind": "audit",
                        "actor": a.get("actor"),
                        "title": "Audit",
                        "detail": a.get("description"),
                    }
                )
        except Exception:
            pass
    items.sort(key=lambda x: str(x.get("at") or ""), reverse=True)
    return items


def _child_rows(doc, field: str, keys) -> list:
    out = []
    for r in getattr(doc, field, None) or []:
        row = {}
        for k in keys:
            try:
                row[k] = r.get(k) if hasattr(r, "get") else getattr(r, k, None)
            except Exception:
                row[k] = None
        out.append(row)
    return out


def _linked_meta(doc) -> dict:
    """Best-effort accounting linkage: GL entries booked against the claim."""
    out = {"gl_entries": 0}
    try:
        rows = frappe.get_all(
            "GL Entry",
            filters={"voucher_type": CLAIM_DOCTYPE, "voucher_no": getattr(doc, "name", None)},
            fields=["name"],
            limit_page_length=50,
        )
        out["gl_entries"] = len(rows or [])
    except Exception:
        out["gl_entries"] = 0
    return out


def _can_flags(doc) -> dict:
    """Server-computed action matrix for the detail view (plan §2.1)."""
    manager = _is_manager()
    own = _is_own(getattr(doc, "employee", None) or "")
    ds = int(getattr(doc, "docstatus", 0) or 0)
    st = getattr(doc, "approval_status", None) or "Draft"
    is_paid = bool(int(getattr(doc, "is_paid", 0) or 0))
    return {
        "edit": ds == 0 and own and st in ("Draft", "Rejected"),
        "cancel": (ds == 0 and own) or (ds == 1 and manager),
        "approve": manager and ds == 0 and st == "Draft",
        "reject": manager and ds == 0 and st in ("Draft", "Approved"),
        "mark_paid": manager and ds == 1 and not is_paid,
        "amend": ds == 2 and (own or manager),
    }


@frappe.whitelist()
def get_expense_claim(name: str | None = None) -> dict:
    """Full detail payload for the SPA detail page (plan §2.1).

    Returns ``{ doc, timeline, comments, attachments, linked, can }``. The
    caller must be the owner or an HR/Manager.
    """
    doc = _get_doc_or_throw(name)
    _assert_own(getattr(doc, "employee", None) or "")
    payload = {f: getattr(doc, f, None) for f in _CLAIM_FIELDS + _DETAIL_EXTRA}
    payload["expenses"] = _child_rows(doc, "expenses", _EXPENSE_ROW_KEYS)
    payload["taxes"] = _child_rows(doc, "taxes", _TAX_ROW_KEYS)
    payload["advances"] = _child_rows(doc, "advances", _ADVANCE_ROW_KEYS)
    return {
        "doc": payload,
        "timeline": _timeline(name),
        "comments": _comments(name),
        "attachments": _attachments(name),
        "linked": _linked_meta(doc),
        "can": _can_flags(doc),
    }


# --------------------------------------------------------------------------- #
# Update / cancel / amend — plan §2.3–§2.5
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def update_expense_claim(
    name: str | None = None,
    expenses: list | None = None,
    posting_date: str | None = None,
    remark: str | None = None,
) -> dict:
    """Edit an own Draft/Rejected claim (plan §2.3).

    Only ``expenses`` / ``posting_date`` / ``remark`` are editable. A Rejected
    draft resets to ``Draft`` (via ``db_set`` — ``approval_status`` is permlevel
    1) so the approver can act on it again. ``doc.save()`` re-runs validate.
    """
    doc = _get_doc_or_throw(name)
    _assert_own(getattr(doc, "employee", None) or "")
    if (getattr(doc, "docstatus", 0) or 0) != 0 or (getattr(doc, "approval_status", None) or "Draft") not in (
        "Draft",
        "Rejected",
    ):
        frappe.throw("Chỉ phiếu nháp hoặc bị từ chối mới sửa được.")

    was_rejected = (getattr(doc, "approval_status", None) or "") == "Rejected"
    old_total = getattr(doc, "total_claimed_amount", None)
    changed = False
    if expenses is not None:
        rows = normalize_expenses(expenses)
        if not rows:
            frappe.throw("Cần ít nhất 1 khoản chi phí hợp lệ (loại + số tiền > 0).")
        try:
            doc.set("expenses", [])
        except Exception:
            setattr(doc, "expenses", [])
        for r in rows:
            doc.append("expenses", r)
        doc.total_claimed_amount = claim_total(expenses)
        changed = True
    if posting_date not in (None, ""):
        doc.posting_date = posting_date
        changed = True
    if remark is not None:
        doc.remark = remark or ""
        changed = True
    if not changed:
        frappe.throw("Không có thay đổi để cập nhật.")

    try:
        doc.save(ignore_permissions=True)
    except TypeError:
        doc.save()
    if was_rejected:
        _doc_set(doc, "approval_status", "Draft")
        doc.approval_status = "Draft"
    _audit(
        "Expense Update",
        doc,
        description=f"Cập nhật phiếu chi phí {name}",
        old_value={"total_claimed_amount": old_total},
        new_value={"total_claimed_amount": getattr(doc, "total_claimed_amount", None)},
    )
    _publish_expense(doc)
    return {
        "name": name,
        "total": getattr(doc, "total_claimed_amount", None),
        "approval_status": getattr(doc, "approval_status", None),
        "message": f"Đã cập nhật phiếu chi phí {name}.",
    }


@frappe.whitelist()
def cancel_expense_claim(name: str | None = None, reason: str | None = None) -> dict:
    """Cancel a claim (plan §2.4): Draft → delete (trash); submitted →
    ``doc.cancel()`` (manager — the hrms hook reverses GL + advance sync)."""
    doc = _get_doc_or_throw(name)
    _assert_own(getattr(doc, "employee", None) or "")
    ds = int(getattr(doc, "docstatus", 0) or 0)
    if ds == 2:
        frappe.throw("Phiếu chi phí này đã bị hủy.")
    if ds == 1:
        if not _is_manager():
            frappe.throw("Chỉ HR/Manager hủy phiếu chi phí đã duyệt.")
        doc.cancel()
        _audit(
            "Expense Cancel",
            doc,
            description=f"Hủy phiếu đã duyệt {name}" + (f" — {reason}" if reason else ""),
            old_value="Submitted",
            new_value="Cancelled",
        )
        _notify(doc, "cancelled", "Cancelled")
        _publish_expense(doc)
        return {"name": name, "docstatus": 2, "message": f"Đã hủy phiếu chi phí {name}."}

    # Draft / Rejected (docstatus 0) → delete with the reason trail in audit.
    frappe.delete_doc(CLAIM_DOCTYPE, name, ignore_permissions=True)
    _audit(
        "Expense Cancel",
        doc,
        description=f"Xóa phiếu nháp {name}" + (f" — {reason}" if reason else ""),
        old_value="Draft",
        new_value="Deleted",
    )
    _publish_expense(doc)
    return {"name": name, "docstatus": 0, "message": f"Đã xóa phiếu nháp {name}."}


@frappe.whitelist()
def amend_expense_claim(name: str | None = None) -> dict:
    """Clone a Cancelled claim into a fresh Draft via ``amended_from`` (§2.5)."""
    doc = _get_doc_or_throw(name)
    _assert_own(getattr(doc, "employee", None) or "")
    if int(getattr(doc, "docstatus", 0) or 0) != 2:
        frappe.throw("Chỉ phiếu đã hủy mới tạo lại được (amend).")
    new_doc = frappe.copy_doc(doc)
    new_doc.posting_date = _today() or getattr(doc, "posting_date", None)
    new_doc.amended_from = name
    new_doc.is_paid = 0
    new_doc.total_amount_reimbursed = 0
    try:
        new_doc.insert(ignore_permissions=True)
    except Exception:
        # copy_doc on real benches resets docstatus; stubs may not — force it.
        new_doc.docstatus = 0
        new_doc.insert(ignore_permissions=True)
    # State fields are no_copy on the DocType; db_set makes that certain.
    for field, value in (("approval_status", "Draft"), ("status", "Draft")):
        try:
            new_doc.db_set(field, value, update_modified=True)
        except Exception:
            setattr(new_doc, field, value)
    _audit(
        "Expense Amend",
        new_doc,
        description=f"Tạo lại từ phiếu đã hủy {name}",
        old_value=name,
        new_value=new_doc.name,
    )
    _publish_expense(new_doc)
    return {
        "name": new_doc.name,
        "original": name,
        "message": f"Đã tạo lại phiếu chi phí {new_doc.name} từ {name}.",
    }


# --------------------------------------------------------------------------- #
# Approve / reject — plan §2.6–§2.7
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def approve_expense_claim(
    name: str | None = None,
    sanctions: list | None = None,
    comment: str | None = None,
) -> dict:
    """Approve with per-line sanctions, then submit (plan §2.6).

    ``sanctions`` is a list aligned with the expenses rows (each item may carry
    ``idx`` + ``sanction_amount``; default = the full claimed amount). After
    stamping ``approval_status = Approved`` the claim is submitted — GL failures
    (accounts not configured) degrade gracefully with ``submit_note`` instead of
    leaving a silent zombie like the old code.
    """
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager duyệt chi phí.")
    doc = _get_doc_or_throw(name)
    ds = int(getattr(doc, "docstatus", 0) or 0)
    st = getattr(doc, "approval_status", None) or "Draft"
    if ds == 2:
        frappe.throw("Phiếu chi phí đã hủy.")
    if ds == 1:
        frappe.throw("Phiếu chi phí đã vào sổ.")
    if st in ("Approved", "Rejected"):
        frappe.throw(f"Đơn chi phí đã ở trạng thái {st} — không duyệt lại.")
    rows = list(getattr(doc, "expenses", None) or [])
    if not rows:
        frappe.throw("Phiếu không có dòng chi phí.")

    sanctioned = resolve_sanctions(rows, sanctions)
    for row, s in zip(rows, sanctioned):
        try:
            row.sanction_amount = s
        except Exception:
            row["sanction_amount"] = s
    total_sanctioned = round(sum(sanctioned), 2)
    taxes = _num(getattr(doc, "total_taxes_and_charges", None))
    doc.total_sanctioned_amount = total_sanctioned
    doc.grand_total = round(total_sanctioned + taxes, 2)
    doc.approval_status = "Approved"
    doc.save()

    submit_note = ""
    try:
        doc.submit()
    except Exception:
        try:
            frappe.log_error(title="expense_claim.submit skipped (accounts not ready)")
        except Exception:
            pass
        submit_note = "Đã duyệt — chưa vào sổ (thiếu cấu hình tài khoản)."

    if (comment or "").strip():
        _add_comment(name, comment.strip())
    _audit(
        "Expense Approve",
        doc,
        description=f"Draft → Approved (duyệt {total_sanctioned})",
        old_value="Draft",
        new_value="Approved",
    )
    _notify(doc, "approved", "Approved")
    _publish_expense(doc)
    return {
        "name": name,
        "approval_status": getattr(doc, "approval_status", None),
        "docstatus": int(getattr(doc, "docstatus", 0) or 0),
        "total_sanctioned": total_sanctioned,
        "submit_note": submit_note,
    }


@frappe.whitelist()
def reject_expense_claim(name: str | None = None, reason: str | None = None) -> dict:
    """Reject with a MANDATORY reason (plan §2.7).

    The reason lands in a Comment (visible on the detail timeline) — the
    employee's ``remark`` is never overwritten (fixes the old clobber bug).
    """
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager từ chối chi phí.")
    if not (reason or "").strip():
        frappe.throw("Cần lý do từ chối.")
    doc = _get_doc_or_throw(name)
    ds = int(getattr(doc, "docstatus", 0) or 0)
    st = getattr(doc, "approval_status", None) or "Draft"
    if ds == 2:
        frappe.throw("Phiếu chi phí đã hủy.")
    if ds == 1:
        frappe.throw("Phiếu đã vào sổ — không từ chối được.")
    if st == "Rejected":
        frappe.throw("Đơn chi phí đã ở trạng thái Rejected — không đổi được.")
    doc.approval_status = "Rejected"
    doc.save()
    _add_comment(name, f"Từ chối: {reason.strip()}")
    _audit(
        "Expense Reject",
        doc,
        description=f"Draft → Rejected — {reason.strip()}",
        old_value=st,
        new_value="Rejected",
    )
    _notify(doc, "rejected", "Rejected")
    _publish_expense(doc)
    return {
        "name": name,
        "approval_status": "Rejected",
        "message": f"Đã từ chối phiếu chi phí {name}.",
    }


# --------------------------------------------------------------------------- #
# Payment / comments / export / upload — plan §2.8–§2.10, §3.2 fallback
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def mark_expense_paid(
    name: str | None = None,
    mode_of_payment: str | None = None,
    clearance_date: str | None = None,
) -> dict:
    """Record payout of a submitted claim (plan §2.8): ``is_paid`` +
    ``mode_of_payment`` + ``clearance_date`` (Desk-parity light path — no
    Payment Entry is created; JE/PE hooks keep ``total_amount_reimbursed``)."""
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager ghi nhận thanh toán chi phí.")
    doc = _get_doc_or_throw(name)
    if int(getattr(doc, "docstatus", 0) or 0) != 1:
        frappe.throw("Chỉ phiếu đã duyệt vào sổ mới ghi nhận thanh toán.")
    if int(getattr(doc, "is_paid", 0) or 0):
        return {
            "name": name,
            "status": getattr(doc, "status", None),
            "is_paid": 1,
            "message": "Phiếu chi phí đã được ghi nhận thanh toán trước đó.",
        }
    mode = (mode_of_payment or getattr(doc, "mode_of_payment", None) or "Cash").strip()
    clearance = clearance_date or _today()
    _doc_set(doc, "is_paid", 1)
    _doc_set(doc, "mode_of_payment", mode)
    _doc_set(doc, "clearance_date", clearance)
    _doc_set(doc, "status", "Paid")
    doc.is_paid = 1
    doc.status = "Paid"
    _audit(
        "Expense Payment",
        doc,
        description=f"Ghi nhận đã trả tiền ({mode}, {clearance})",
        old_value="Unpaid",
        new_value="Paid",
    )
    _notify(doc, "paid", "Paid")
    _publish_expense(doc)
    return {
        "name": name,
        "status": "Paid",
        "is_paid": 1,
        "message": f"Đã ghi nhận thanh toán phiếu chi phí {name}.",
    }


@frappe.whitelist()
def add_expense_comment(name: str | None = None, comment: str | None = None) -> dict:
    """Append a Comment row on the claim (plan §2.9). Owner or HR/Manager."""
    if not name:
        frappe.throw("Thiếu mã phiếu chi phí.")
    if not (comment or "").strip():
        frappe.throw("Nội dung bình luận không được để trống.")
    doc = _get_doc_or_throw(name)
    _assert_own(getattr(doc, "employee", None) or "")
    row = _add_comment(name, comment.strip())
    if row is None:
        frappe.throw("Không gửi được bình luận.")
    _publish_expense(doc)
    return {
        "name": getattr(row, "name", None),
        "owner": getattr(row, "owner", None),
        "content": getattr(row, "content", None),
        "message": "Đã gửi bình luận.",
    }


def _build_expense_csv(rows: list) -> str:
    """Excel-safe CSV (UTF-8 BOM) of the filtered claim list (plan §2.10)."""
    header = [
        "Mã phiếu",
        "Mã nhân viên",
        "Tên nhân viên",
        "Ngày phiếu",
        "Tổng khai báo",
        "Tổng duyệt",
        "Đã hoàn trả",
        "Trạng thái duyệt",
        "Trạng thái",
        "Người duyệt",
        "Ghi chú",
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
                r.get("total_claimed_amount"),
                r.get("total_sanctioned_amount"),
                r.get("total_amount_reimbursed"),
                r.get("approval_status"),
                r.get("status"),
                r.get("expense_approver"),
                (r.get("remark") or "").replace("\n", " "),
            ]
        )
    return "\ufeff" + buf.getvalue()


@frappe.whitelist()
def export_expense_csv(
    status: str | None = None,
    search: str | None = None,
    employee_name: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    amount_min: float | None = None,
    amount_max: float | None = None,
    download: int = 0,
) -> dict:
    """Manager-only CSV export of the filtered claim list (plan §2.10).

    Same filter contract as :func:`all_expense_claims` (minus pagination).
    Returns ``{filename, content, rows, truncated}``; with ``download=1`` the
    response switches to a binary file download.
    """
    if not _is_manager():
        frappe.throw("Bạn không có quyền xuất danh sách chi phí.")
    flt, or_filters = _claim_filters(
        status=status,
        search=search,
        employee_name=employee_name,
        date_from=date_from,
        date_to=date_to,
        amount_min=amount_min,
        amount_max=amount_max,
    )
    try:
        rows = (
            frappe.get_all(
                CLAIM_DOCTYPE,
                filters=flt or None,
                or_filters=or_filters,
                fields=_CLAIM_FIELDS,
                order_by="posting_date desc",
                limit_page_length=_EXPORT_MAX_ROWS + 1,  # +1 detects truncation
            )
            or []
        )
    except Exception:
        frappe.log_error(title="expense.export failed")
        rows = []
    truncated = len(rows) > _EXPORT_MAX_ROWS
    rows = rows[:_EXPORT_MAX_ROWS]

    try:
        from gege_hr.gege_hr.api import audit as audit_api

        audit_api.log(
            "Manual Override",
            company=None,
            description=f"Xuất CSV chi phí: {len(rows)} dòng",
            new_value={"filters": {"status": status, "date_from": date_from, "date_to": date_to}, "rows": len(rows)},
        )
    except Exception:
        pass

    csv_text = _build_expense_csv(rows)
    filename = f"expense_export_{_today() or 'today'}.csv"
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


@frappe.whitelist()
def upload_expense_attachment(name: str | None = None, is_private: int = 1) -> dict:
    """Fallback receipt upload (plan §3.2): employees whose ``if_owner`` perms
    block the native ``frappe.handler.upload_file`` land here instead."""
    doc = _get_doc_or_throw(name)
    _assert_own(getattr(doc, "employee", None) or "")
    try:
        from frappe.utils.file_manager import save_file
    except Exception:
        frappe.throw("Không tải được module xử lý tệp.")
    fs = None
    try:
        fs = (getattr(frappe.request, "files", None) or {}).get("file")
    except Exception:
        fs = None
    if fs is None:
        frappe.throw("Thiếu tệp đính kèm.")
    filename = getattr(fs, "filename", None) or "receipt"
    try:
        content = fs.read()
    except Exception:
        frappe.throw("Không đọc được tệp.")
    try:
        out = save_file(
            filename,
            content,
            CLAIM_DOCTYPE,
            name,
            is_private=int(bool(is_private)),
            ignore_permissions=True,
        )
    except Exception:
        frappe.throw("Không lưu được tệp đính kèm.")
    _publish_expense(doc)
    return {
        "name": getattr(out, "name", None),
        "file_url": getattr(out, "file_url", None),
        "message": "Đã tải lên tệp đính kèm.",
    }
