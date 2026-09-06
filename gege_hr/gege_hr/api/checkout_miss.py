"""Checkout-miss API — HR admin list/resolve + employee explain endpoints.

Fronts the VN Checkout Miss doctype created by the auto-close engine
(``gege_hr.gege_hr.utils.checkout_miss``). HR can list + resolve tickets
(waive/penalise/close); employees can view their own tickets and submit an
explanation (optionally opening a Correction Request for a real late checkout).

Desk-free parity (plans/plan-checkout-miss-admin-parity.md) adds:
  * ``list_checkout_misses``      — employee/date-window/shift/repeat-offender
    filters + opt-in pagination envelope ``{"data","total","summary"}`` (§6.6 A)
  * ``get_checkout_miss``         — detail + payroll impact + audit/version timeline
  * ``resolve_checkout_miss``     — optional HR ``penalty_amount`` override
  * ``bulk_resolve_checkout_misses`` — partial-safe bulk resolve (cap 100)
  * ``export_checkout_misses_csv``   — filtered CSV export (cap 10k)
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, timedelta

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime

from gege_hr.gege_hr.api import audit as audit_api
from gege_hr.gege_hr.utils import employee as emp_utils, pagination
from gege_hr.gege_hr.utils.tz import wall as tz_wall

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
    # C6 — one-shot employee appeal against a Penalised ticket.
    "appeal_count",
    "appealed_on",
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


# Summary buckets for the admin tiles — every Select option of the status field.
_SUMMARY_STATUSES = ("Pending", "Explained", "Waived", "Penalised", "Closed")


def _list_filters(
    status: str | None,
    employee: str | None,
    from_date: str | None,
    to_date: str | None,
    shift_type: str | None,
    min_occurrence,
) -> list:
    """Shared WHERE (list form) for the admin list + CSV export."""
    filters: list = [["docstatus", "<", 2]]
    if status:
        filters.append(["status", "=", status])
    if employee:
        filters.append(["employee", "=", emp_utils.emp_name(employee)])
    if from_date:
        filters.append(["work_date", ">=", from_date])
    if to_date:
        filters.append(["work_date", "<=", to_date])
    if shift_type:
        filters.append(["shift_type", "=", shift_type])
    min_occ = pagination.as_int(min_occurrence, 0)
    if min_occ > 1:
        filters.append(["occurrence_no", ">=", min_occ])
    return filters


def _search_or_filters(search: str | None) -> list | None:
    """Frappe ``or_filters`` (list form) for a free-text ticket search."""
    q = (search or "").strip()
    if not q:
        return None
    like = f"%{pagination.escape_like(q)}%"
    return [
        ["employee_name", "like", like],
        ["employee", "like", like],
        ["shift_type", "like", like],
    ]


def _status_summary(base_filters: list, or_filters: list | None) -> dict:
    """Per-status counts over the full filtered set (DNA §6.6 A).

    ``base_filters`` excludes the status filter on purpose — the tiles stay
    meaningful when HR is viewing one status (the pending count must not
    collapse to "everything is the filtered status").
    """
    light = pagination.all_rows(MISS_DOCTYPE, fields=["status"], filters=base_filters, or_filters=or_filters)
    buckets = pagination.bucket_counts(light, "status")
    out: dict = {"total": len(light)}
    for st in _SUMMARY_STATUSES:
        out[st.lower()] = int(buckets.get(st) or 0)
    return out


# Client sort keys → real columns (P2 §8 UX round). ``order_by`` is SQL, so
# the mapping is a strict whitelist — never interpolate raw client input.
_SORTABLE = {
    "date": "work_date",
    "status": "status",
    "occurrence": "occurrence_no",
    "penalty": "penalty_amount",
    "created": "creation",
}
_DEFAULT_ORDER = "work_date desc, creation desc"


def _order_by_clause(sort: str | None) -> str:
    """Whitelist-mapped ORDER BY from a ``<key> <asc|desc>`` client token."""
    parts = (sort or "").strip().lower().split()
    if not parts:
        return _DEFAULT_ORDER
    key, direction = parts[0], (parts[1] if len(parts) > 1 else "desc")
    if key not in _SORTABLE or direction not in ("asc", "desc"):
        frappe.throw(_("Thứ tự sắp xếp không hợp lệ."))
    return f"{_SORTABLE[key]} {direction}, name {direction}"


@frappe.whitelist()
def list_checkout_misses(
    status: str | None = None,
    employee: str | None = None,
    search: str | None = None,
    limit: int = 100,
    from_date: str | None = None,
    to_date: str | None = None,
    shift_type: str | None = None,
    min_occurrence: int = 0,
    page: int = 1,
    page_size: int = 0,
    sort: str | None = None,
) -> list[dict] | dict:
    """HR — checkout-miss tickets with filters.

    Filters: ``status`` (exact), ``employee``, free-text ``search`` (LIKE across
    employee_name/employee/shift_type — wildcards escaped), a ``from_date``/
    ``to_date`` window on ``work_date`` (inclusive), ``shift_type`` and
    ``min_occurrence`` (>= 2 = repeat offenders).

    Pagination is opt-in (DNA §6.6 A — parity ``audit_events``): with a
    positive ``page_size`` returns ``{"data", "total", "summary"}`` where
    ``total`` counts the full filtered set and ``summary`` buckets per-status
    over the filtered set *minus the status filter*. Legacy callers (and the
    bench tests) keep the bare-list return.
    """
    _require_hr()
    filters = _list_filters(status, employee, from_date, to_date, shift_type, min_occurrence)
    or_filters = _search_or_filters(search)
    order_by = _order_by_clause(sort)
    if page_size:
        base = [f for f in filters if f[0] != "status"]
        summary = _status_summary(base, or_filters)
        out = pagination.page_slice(
            MISS_DOCTYPE,
            fields=_MISS_FIELDS,
            filters=filters,
            or_filters=or_filters,
            order_by=order_by,
            page=page,
            page_size=page_size,
        )
        out["summary"] = summary
        return out
    try:
        return (
            frappe.db.get_all(
                MISS_DOCTYPE,
                filters=filters,
                or_filters=or_filters,
                fields=_MISS_FIELDS,
                order_by=order_by,
                limit_page_length=pagination.clamp_limit(limit, default=100, maximum=500),
            )
            or []
        )
    except Exception:
        frappe.log_error(title="checkout_miss.list failed")
        return []


def _parse_penalty_override(value) -> float | None:
    """Optional HR override for the penalty amount (penalise only).

    ``None``/"" → no override (legacy default-stamp path). Negative or
    non-numeric values are rejected up front (parity payroll settings B5/B6 —
    a legit ``0`` stays ``0``).
    """
    if value is None or value == "":
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        frappe.throw(_("Mức phạt phải là số."))
    if v < 0:
        frappe.throw(_("Mức phạt không được âm."))
    return v


def _publish(event: str, payload: dict) -> None:
    """Best-effort realtime notify (stub-safe): never fails the caller."""
    pub = getattr(frappe, "publish_realtime", None)
    if not pub:
        return
    try:
        pub(event, payload)
    except Exception:
        pass


def _rate_limited(func):
    """Employee-facing spam guard (B7) — ``frappe.rate_limit`` when available.

    The bench-free stub has no ``rate_limit`` symbol, so absence degrades to a
    passthrough (the guard is defence-in-depth, not the only protection).
    """
    rl = getattr(frappe, "rate_limit", None)
    if rl is None:
        return func
    try:
        return rl(key="user", limit=10, seconds=3600)(func)
    except Exception:
        return func


def _resolve_one(
    name: str,
    action: str,
    note: str | None,
    penalty_override: float | None,
) -> str | None:
    """Shared resolve core (single endpoint + bulk). Returns the ticket's
    payroll state so the single endpoint can flag ``payroll_recalc_required``.

    Raises via ``frappe.throw`` on any guard failure — the bulk wrapper turns
    that into a per-row ``failed`` entry instead of aborting the batch.
    """
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
        if penalty_override is not None:
            doc.penalty_amount = penalty_override
        elif not float(doc.penalty_amount or 0):
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
    _publish("checkout_miss_updated", {"ticket": name, "status": _STATUS_FOR_ACTION[action]})
    # B1 — best-effort result email to the employee (action → template kind:
    # waive/penalise/close map onto the waived/penalised/closed templates).
    _email_notify(name, _STATUS_FOR_ACTION[action].lower())
    return payroll_state


@frappe.whitelist()
def resolve_checkout_miss(
    name: str,
    action: str,
    note: str | None = None,
    waive_penalty: int = 0,
    penalty_amount=None,
) -> dict:
    """HR — resolve a ticket. ``action`` ∈ waive / penalise / close.

    BUG-7 fix: state machine — ``Closed`` is terminal and a no-op re-resolve
    is rejected; updates go through ``doc.save()`` so the doctype's own
    validate/on_update hooks fire (no raw ``db.set_value`` bypass).
    BUG-4 fix: penalising a first-N (amount=0) ticket stamps the configured
    penalty so the action actually deducts; an explicit ``penalty_amount``
    overrides both (validated — never negative/non-numeric).
    BUG-2 fix: refuses to mutate a ticket whose payroll period is Approved
    with generated slips; flags ``payroll_recalc_required`` when the period
    is only Calculated so HR recalculates before approving.
    """
    _require_hr()
    name = (name or "").strip()
    action = (action or "").strip().lower()
    if action not in ("waive", "penalise", "close"):
        frappe.throw(_("Hành động không hợp lệ."))
    penalty_override = _parse_penalty_override(penalty_amount)
    payroll_state = _resolve_one(name, action, note, penalty_override)
    result = frappe.db.get_value(MISS_DOCTYPE, name, _MISS_FIELDS, as_dict=True) or {}
    result["payroll_recalc_required"] = payroll_state == "calculated"
    return result


# Timeline rows returned per source (audit + version) — enough for the modal
# without dumping the whole history.
_TIMELINE_LIMIT = 50


def _version_summary(data) -> str:
    """Human line for a Version row's ``data`` JSON (best-effort)."""
    try:
        payload = json.loads(data) if isinstance(data, str) else (data or {})
        parts: list[str] = []
        for c in (payload.get("changed") or [])[:6]:
            if isinstance(c, (list, tuple)) and len(c) >= 2:
                pair = c[1] if isinstance(c[1], (list, tuple)) else []
                old = pair[0] if len(pair) > 0 else ""
                new = pair[1] if len(pair) > 1 else ""
                parts.append(f"{c[0]}: {old} → {new}")
        return "; ".join(parts) or "Cập nhật bản ghi"
    except Exception:
        return "Cập nhật bản ghi"


def _ticket_timeline(name: str) -> list[dict]:
    """Merged audit + version trail for one ticket (newest first)."""
    rows: list[dict] = []
    try:
        for r in (
            frappe.db.get_all(
                "VN Audit Event",
                filters=[["reference_doctype", "=", MISS_DOCTYPE], ["reference_name", "=", name]],
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
                limit_page_length=_TIMELINE_LIMIT,
            )
            or []
        ):
            rows.append(
                {
                    "source": "audit",
                    "at": r.get("created_at"),
                    "actor": r.get("actor"),
                    "description": r.get("description") or r.get("audit_type") or "",
                    "old_value": r.get("old_value"),
                    "new_value": r.get("new_value"),
                }
            )
    except Exception:
        frappe.log_error(title="checkout_miss.timeline audit failed")
    try:
        for v in (
            frappe.db.get_all(
                "Version",
                filters=[["ref_doctype", "=", MISS_DOCTYPE], ["docname", "=", name]],
                fields=["name", "owner", "creation", "data"],
                order_by="creation desc",
                limit_page_length=_TIMELINE_LIMIT,
            )
            or []
        ):
            rows.append(
                {
                    "source": "version",
                    "at": v.get("creation"),
                    "actor": v.get("owner"),
                    "description": _version_summary(v.get("data")),
                    "old_value": None,
                    "new_value": None,
                }
            )
    except Exception:
        frappe.log_error(title="checkout_miss.timeline versions failed")
    try:
        for c in (
            frappe.db.get_all(
                "Comment",
                filters={
                    "comment_type": "Comment",
                    "reference_doctype": MISS_DOCTYPE,
                    "reference_name": name,
                },
                fields=["name", "owner", "creation", "content"],
                order_by="creation desc",
                limit_page_length=_TIMELINE_LIMIT,
            )
            or []
        ):
            rows.append(
                {
                    "source": "comment",
                    "at": c.get("creation"),
                    "actor": c.get("owner"),
                    "description": c.get("content") or "",
                    "old_value": None,
                    "new_value": None,
                }
            )
    except Exception:
        frappe.log_error(title="checkout_miss.timeline comments failed")
    rows.sort(key=lambda r: str(r.get("at") or ""), reverse=True)
    return rows


@frappe.whitelist()
def get_checkout_miss(name: str) -> dict:
    """HR — one ticket + payroll impact + activity timeline (desk-free detail).

    ``payroll_state`` mirrors the resolve guard: ``None`` (safe to act),
    ``"calculated"`` (recalc needed after resolving — show an amber banner),
    ``"locked"`` (Approved period with slips — the UI must disable actions and
    point at VN Payroll Adjustment).
    """
    _require_hr()
    name = (name or "").strip()
    if not name or not frappe.db.exists(MISS_DOCTYPE, name):
        frappe.throw(_("Ticket không tồn tại."))
    ticket = frappe.db.get_value(MISS_DOCTYPE, name, _MISS_FIELDS, as_dict=True) or {}
    return {
        "ticket": ticket,
        "payroll_state": _payroll_state_for(
            {
                "work_date": ticket.get("work_date"),
                "employee": ticket.get("employee"),
                "company": ticket.get("company"),
            }
        ),
        "timeline": _ticket_timeline(name),
        # Desk-free COMPLETE §3 A2/A3 — collaboration context on the detail.
        "assignees": _ticket_assignees(name),
        "attachments": _ticket_attachments(name),
    }


@frappe.whitelist()
def my_checkout_misses(status: str | None = None, search: str | None = None) -> list[dict]:
    """Employee — their own checkout-miss tickets (Pending ones needing action).

    ``search`` (P2 §8 — clears backlog HR-BL-checkout-miss) is a free-text
    post-filter across name/shift/work_date/employee_name, applied AFTER the
    50-row window (an employee's ticket volume is small by construction).
    """
    emp = emp_utils.get_employee_for_user()
    if not emp:
        return []
    filters = [["employee", "=", emp], ["docstatus", "<", 2]]
    if status:
        filters.append(["status", "=", status])
    try:
        rows = (
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
    q = (search or "").strip().lower()
    if q:
        rows = [
            r
            for r in rows
            if any(
                q in str(r.get(f) or "").lower() for f in ("name", "shift_type", "work_date", "employee_name")
            )
        ]
    return rows


@frappe.whitelist()
@_rate_limited
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
    ticket = frappe.db.get_value(MISS_DOCTYPE, name, ["employee", "status"], as_dict=True) or {}
    ticket_emp = ticket.get("employee")
    emp = emp_utils.get_employee_for_user()
    # BUG-3 fix: a user with no Employee record must not explain anyone's ticket
    # (the old `if emp and ...` check silently passed when emp was None).
    if not emp or (ticket_emp and emp != ticket_emp):
        frappe.throw(_("Bạn chỉ được giải trình ticket của mình."), frappe.PermissionError)
    # BUG-1 fix: only Pending/Explained tickets accept an explanation — flipping
    # a Penalised ticket back to Explained would silently drop the payroll
    # penalty (load_checkout_miss_penalty only counts Penalised non-waived).
    status = (ticket.get("status") or "").strip()
    if status not in ("Pending", "Explained"):
        frappe.throw(
            _("Ticket ở trạng thái {0} — không thể giải trình nữa.").format(status or "?"),
            frappe.ValidationError,
        )
    # Grace-deadline hard-stop: even while the status is still Pending (the
    # hourly flip may not have run yet), a ticket past its grace_deadline no
    # longer accepts explanations — the card is closed for the employee.
    deadline = frappe.db.get_value(MISS_DOCTYPE, name, "grace_deadline")
    # PHASE-1 FRAME: deadline is naive PORTAL WALL — compare in the same frame.
    if deadline and tz_wall(now_datetime()) > tz_wall(get_datetime(deadline)):
        frappe.throw(
            _("Đã quá hạn giải trình — phiếu {0} đã bị khoá.").format(name),
            frappe.ValidationError,
        )
    # B6 — evidence caps (count in the joined string + attached File sizes).
    _validate_evidence_caps(name, evidence_ref)
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
            # D-FLOW FIX: a CR opened from an explanation must enter the
            # approval pipeline immediately (Draft → Pending Manager) — exactly
            # like submit_correction_request does. Otherwise the CR sits in
            # Draft forever and HR's approve_request rejects it ("không ở
            # trạng thái chờ duyệt"), so the real checkout never replaces the
            # fake OUT (E2E Group D finding).
            # Move the CR into the approval pipeline. doc.save() is unusable
            # here: the employee session lacks READ on the CR doctype (the
            # insert above ran ignore_permissions), so send_for_approval's
            # save() fails on has_permission and the CR silently stays Draft.
            # A direct state write is safe — explain already verified ticket
            # ownership, and the approver's approve_request performs the full
            # doc.save() with hooks when acting on it.
            frappe.db.set_value(
                "VN Attendance Correction Request",
                cr_name,
                {"workflow_state": "Pending Manager"},
                update_modified=False,
            )
            frappe.db.commit()
        except Exception:
            frappe.log_error(title="checkout_miss.explain correction create failed")
    frappe.db.set_value(MISS_DOCTYPE, name, updates)
    # Audit the employee's explanation (best-effort — same contract as resolve).
    try:
        ctx = frappe.db.get_value(MISS_DOCTYPE, name, ["work_date", "company"], as_dict=True) or {}
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
    _email_notify(name, "explain_ack")  # B1 — best-effort acknowledgement
    return frappe.db.get_value(MISS_DOCTYPE, name, _MISS_FIELDS, as_dict=True) or {}


# --------------------------------------------------------------------------- #
# Desk-free parity — bulk resolve + CSV export (plan §3 B4/B5)
# --------------------------------------------------------------------------- #
BULK_MAX = 100


@frappe.whitelist()
def bulk_resolve_checkout_misses(
    names,
    action: str,
    note: str | None = None,
    penalty_amount=None,
) -> dict:
    """HR — resolve many tickets with one action (partial-safe, cap 100).

    Every per-ticket guard (state machine, payroll lock) keeps firing: a row
    that fails lands in ``failed`` with its Vietnamese message while the rest
    still resolve (parity ``approval.bulk_approve`` — never roll back the
    batch). Returns ``{"updated": [...], "failed": [{name, error}],
    "counts": {"waived", "penalised", "closed"}}``.
    """
    _require_hr()
    names = [str(n or "").strip() for n in (names or []) if str(n or "").strip()]
    if not names:
        frappe.throw(_("Chọn ít nhất một ticket."))
    if len(names) > BULK_MAX:
        frappe.throw(_("Tối đa {0} ticket mỗi lần.").format(BULK_MAX))
    action = (action or "").strip().lower()
    if action not in ("waive", "penalise", "close"):
        frappe.throw(_("Hành động không hợp lệ."))
    penalty_override = _parse_penalty_override(penalty_amount)

    updated: list[str] = []
    failed: list[dict] = []
    for n in names:
        try:
            _resolve_one(n, action, note, penalty_override)
            updated.append(n)
        except Exception as e:
            failed.append({"name": n, "error": str(e).strip() or "Không xử lý được."})
    counts = {"waived": 0, "penalised": 0, "closed": 0}
    counts[_STATUS_FOR_ACTION[action].lower()] = len(updated)
    _publish(
        "checkout_miss_updated",
        {"action": "bulk", "kind": action, "updated": len(updated), "failed": len(failed)},
    )
    return {"updated": updated, "failed": failed, "counts": counts}


# WP11 parity — CSV export ceiling keeps the response bounded (same cap as the
# audit export); a bigger window can be re-run per month.
EXPORT_MAX_ROWS = 10000

_EXPORT_HEADERS = [
    "Mã ticket",
    "Mã NV",
    "Tên nhân viên",
    "Ngày",
    "Ca",
    "Trạng thái",
    "Lần thứ",
    "Tự đóng lúc",
    "Hạn giải trình",
    "Mức phạt",
    "Đã miễn phạt",
    "Xử lý bởi",
    "Xử lý lúc",
    "Ghi chú HR",
    "Yêu cầu điều chỉnh",
]


def _export_matrix(rows: list[dict]) -> list[list]:
    """Header + data rows as a plain matrix (shared by CSV and XLSX, B2)."""
    out = [list(_EXPORT_HEADERS)]
    for r in rows or []:
        out.append(
            [
                r.get("name"),
                r.get("employee"),
                r.get("employee_name"),
                r.get("work_date"),
                r.get("shift_type"),
                r.get("status"),
                r.get("occurrence_no"),
                r.get("auto_checkout_at"),
                r.get("grace_deadline"),
                r.get("penalty_amount"),
                "Có" if r.get("penalty_waived") else "",
                r.get("resolved_by"),
                r.get("resolved_on"),
                r.get("note"),
                r.get("correction_request"),
            ]
        )
    return out


def _build_csv(rows: list[dict]) -> str:
    """Serialise tickets to CSV with a UTF-8 BOM (Excel-safe, parity audit)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerows(_export_matrix(rows))
    return "\ufeff" + buf.getvalue()


@frappe.whitelist()
def export_checkout_misses_csv(
    status: str | None = None,
    employee: str | None = None,
    search: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    shift_type: str | None = None,
    min_occurrence: int = 0,
    download: int = 0,
    sort: str | None = None,
    fmt: str | None = None,
) -> dict:
    """HR — CSV/XLSX export honouring the active list filters (parity WP11).

    Same filter contract as :func:`list_checkout_misses`. ``fmt`` ∈ csv (default)
    | xlsx (B2 — standard ``make_xlsx``). Returns ``{filename, content, rows,
    truncated}``; with ``download=1`` the response is switched to a binary file
    download. Capped at :data:`EXPORT_MAX_ROWS` (``truncated`` flags the cut).
    """
    _require_hr()
    fmt = (fmt or "csv").strip().lower()
    if fmt not in ("csv", "xlsx"):
        frappe.throw(_("Định dạng xuất không hợp lệ."))
    filters = _list_filters(status, employee, from_date, to_date, shift_type, min_occurrence)
    or_filters = _search_or_filters(search)
    try:
        rows = (
            frappe.db.get_all(
                MISS_DOCTYPE,
                filters=filters,
                or_filters=or_filters,
                fields=_MISS_FIELDS,
                order_by=_order_by_clause(sort),
                limit_page_length=EXPORT_MAX_ROWS + 1,  # +1 detects the truncation
            )
            or []
        )
    except Exception:
        frappe.log_error(title="checkout_miss.export failed")
        rows = []
    truncated = len(rows) > EXPORT_MAX_ROWS
    rows = rows[:EXPORT_MAX_ROWS]
    if fmt == "xlsx":
        from frappe.utils.xlsxutils import make_xlsx  # lazy: stub-safe

        workbook = make_xlsx(_export_matrix(rows), _("Quên checkout"))
        xlsx_bytes = workbook.getvalue() if hasattr(workbook, "getvalue") else bytes(workbook or b"")
        filename = f"quen-checkout_{date.today().isoformat()}.xlsx"
        if download:
            frappe.response.filename = filename
            frappe.response.filecontent = xlsx_bytes
            frappe.response.type = "binary"
            return {"rows": len(rows), "truncated": truncated}
        return {
            "filename": filename,
            "content": xlsx_bytes,
            "rows": len(rows),
            "truncated": truncated,
        }
    csv_text = _build_csv(rows)
    filename = f"quen-checkout_{date.today().isoformat()}.csv"
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


# --------------------------------------------------------------------------- #
# P2 desk-free round (plan §8) — grace extend / manual create / delete /
# reopen / remind. All HR-gated, audited ("Manual Override" for the three
# desk-parity mutations — the vocabulary stays untouched), realtime-published.
# --------------------------------------------------------------------------- #
def _require_hr_manager() -> None:
    """Destructive ops — HR Manager / Payroll Manager only (not plain HR User)."""
    roles = set(emp_utils.get_user_roles() or [])
    if not (roles & {"HR Manager", "Payroll Manager"}):
        frappe.throw(_("Chỉ HR Manager mới được thao tác này."), frappe.PermissionError)


def _load_ticket(name: str):
    """Exists-check + load in one step (throws on a dead link)."""
    name = (name or "").strip()
    if not name or not frappe.db.exists(MISS_DOCTYPE, name):
        frappe.throw(_("Ticket không tồn tại."))
    return name, frappe.get_doc(MISS_DOCTYPE, name)


def _audit_override(doc, name: str, description: str, old_value=None, new_value=None) -> None:
    """Best-effort audit row (type "Manual Override" — no vocab change)."""
    try:
        audit_api.log(
            "Manual Override",
            company=doc.company,
            employee=doc.employee,
            work_date=doc.work_date,
            reference_doctype=MISS_DOCTYPE,
            reference_name=name,
            description=description,
            old_value=old_value,
            new_value=new_value,
        )
    except Exception:
        pass


def _extend_grace_one(name: str, new_deadline: str, reason: str) -> str:
    """Shared grace-extension core (single endpoint + bulk, parity ``_resolve_one``).

    Only Pending/Explained tickets; ``new_deadline`` must be a future portal
    wall datetime. Raises via ``frappe.throw`` on any guard failure — the bulk
    wrapper turns that into a per-row ``failed`` entry. Returns the old
    deadline string (for the audit row / response).
    """
    name, doc = _load_ticket(name)
    if (doc.status or "").strip() not in ("Pending", "Explained"):
        frappe.throw(_("Chỉ ticket đang chờ giải trình mới được gia hạn."))
    try:
        new_dt = tz_wall(get_datetime(new_deadline))
    except Exception:
        frappe.throw(_("Thời hạn mới không hợp lệ."))
    if new_dt <= tz_wall(now_datetime()):
        frappe.throw(_("Thời hạn mới phải ở tương lai."))
    # getattr: a doc payload without the key (bench stub) must not crash the
    # shared core — a real DB row always carries the column.
    old = getattr(doc, "grace_deadline", None)
    doc.grace_deadline = new_dt.strftime("%Y-%m-%d %H:%M:%S")
    doc.save(ignore_permissions=True)
    _audit_override(
        doc,
        name,
        f"extend grace {old} → {doc.grace_deadline} — {reason}",
        old_value=str(old or ""),
        new_value=str(doc.grace_deadline),
    )
    _publish("checkout_miss_updated", {"ticket": name, "grace_extended": 1})
    _email_notify(name, "grace_extended")  # B1 — new deadline to the employee
    return str(old or "")


@frappe.whitelist()
def extend_checkout_miss_grace(name: str, new_deadline: str, reason: str | None = None) -> dict:
    """HR — extend a ticket's explanation deadline (P2 §8)."""
    _require_hr()
    reason = (reason or "").strip()
    if not reason:
        frappe.throw(_("Lý do gia hạn là bắt buộc."))
    _extend_grace_one(name, new_deadline, reason)
    return frappe.db.get_value(MISS_DOCTYPE, name, _MISS_FIELDS, as_dict=True) or {}


def _create_one(
    employee: str,
    work_date: str,
    shift_type: str | None = None,
    note: str | None = None,
    penalty_amount=None,
) -> dict:
    """Shared single-create core (C5) — every guard of the public endpoint.

    Guards: employee must exist; ONE open ticket per employee+day; occurrence
    counted from prior tickets; grace deadline starts now + ``grace_hours``
    (engine default); penalty follows first-N freebies unless ``penalty_amount``
    overrides.
    """
    employee = (employee or "").strip()
    work_date = str(work_date or "").strip()
    if not employee or not frappe.db.exists("Employee", employee):
        frappe.throw(_("Nhân viên không tồn tại."))
    try:
        date.fromisoformat(work_date)
    except ValueError:
        frappe.throw(_("Ngày làm việc không hợp lệ (YYYY-MM-DD)."))
    if frappe.db.exists(MISS_DOCTYPE, {"employee": employee, "work_date": work_date, "docstatus": ["<", 2]}):
        frappe.throw(_("Đã tồn tại ticket cho nhân viên này trong ngày {0}.").format(work_date))

    from gege_hr.gege_hr.utils.checkout_miss import DEFAULTS as CM_DEFAULTS

    occ = pagination.count_all(MISS_DOCTYPE, filters=[["employee", "=", employee], ["docstatus", "<", 2]]) + 1
    amount = _parse_penalty_override(penalty_amount)
    if amount is None:
        free_first_n = int(CM_DEFAULTS.get("free_first_n", 2))
        amount = 0.0 if occ <= free_first_n else float(_default_penalty_amount())
    grace_hours = int(CM_DEFAULTS.get("grace_hours", 24))
    grace_deadline = (tz_wall(now_datetime()) + timedelta(hours=grace_hours)).strftime("%Y-%m-%d %H:%M:%S")

    doc = frappe.get_doc(
        {
            "doctype": MISS_DOCTYPE,
            "employee": employee,
            "employee_name": frappe.db.get_value("Employee", employee, "employee_name") or "",
            "work_date": work_date,
            "shift_type": (shift_type or "").strip() or None,
            "company": frappe.db.get_value("Employee", employee, "company") or "",
            "status": "Pending",
            "occurrence_no": occ,
            "penalty_amount": amount,
            "penalty_waived": 0,
            "grace_deadline": grace_deadline,
            "note": (note or "").strip(),
        }
    )
    doc.insert(ignore_permissions=True)
    _audit_override(doc, doc.name, f"manual create ticket {doc.name} — {note or ''}", new_value="Pending")
    _publish("checkout_miss_created", {"ticket": doc.name, "work_date": work_date})
    return frappe.db.get_value(MISS_DOCTYPE, doc.name, _MISS_FIELDS, as_dict=True) or {}


@frappe.whitelist()
def create_checkout_miss(
    employee: str,
    work_date: str,
    shift_type: str | None = None,
    note: str | None = None,
    penalty_amount=None,
) -> dict:
    """HR — record a historical missed checkout the engine did not cover."""
    _require_hr()
    return _create_one(employee, work_date, shift_type, note, penalty_amount)


@frappe.whitelist()
def delete_checkout_miss(name: str, reason: str | None = None) -> dict:
    """HR Manager — remove a wrong engine artifact (P2 §8).

    Destructive: HR Manager/Payroll Manager only, mandatory reason, refused
    while the covering payroll period is locked. The audit row lands BEFORE
    the doc is gone (append-only trail keeps the reference).
    """
    _require_hr_manager()
    reason = (reason or "").strip()
    if not reason:
        frappe.throw(_("Lý do xoá là bắt buộc."))
    name, doc = _load_ticket(name)
    if (
        _payroll_state_for({"work_date": doc.work_date, "employee": doc.employee, "company": doc.company})
        == "locked"
    ):
        frappe.throw(
            _("Kỳ lương chứa ngày {0} đã phê duyệt và sinh phiếu lương — không thể xoá ticket.").format(
                doc.work_date
            ),
            frappe.ValidationError,
        )
    _audit_override(
        doc,
        name,
        f"delete ticket {name} (status {doc.status}) — {reason}",
        old_value=(doc.status or ""),
        new_value=None,
    )
    frappe.delete_doc(MISS_DOCTYPE, name, ignore_permissions=True)
    _publish("checkout_miss_updated", {"ticket": name, "deleted": 1})
    return {"deleted": name}


@frappe.whitelist()
def reopen_checkout_miss(name: str, note: str | None = None) -> dict:
    """HR — undo a close (Closed → Pending, P2 §8).

    Same payroll guards as resolve; the explanation/grace state is preserved
    as-is so the employee can still act on it.
    """
    _require_hr()
    name, doc = _load_ticket(name)
    if (doc.status or "").strip() != "Closed":
        frappe.throw(_("Chỉ ticket đã đóng mới có thể mở lại."))
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
    doc.status = "Pending"
    doc.resolved_by = None
    doc.resolved_on = None
    if (note or "").strip():
        doc.note = note.strip()
    doc.save(ignore_permissions=True)
    try:
        audit_api.log(
            "Checkout Miss Resolve",
            company=doc.company,
            employee=doc.employee,
            work_date=doc.work_date,
            reference_doctype=MISS_DOCTYPE,
            reference_name=name,
            description=f"reopen ticket {name}" + (f" — {note}" if note else ""),
            old_value="Closed",
            new_value="Pending",
        )
    except Exception:
        pass
    _publish("checkout_miss_updated", {"ticket": name, "reopened": 1})
    result = frappe.db.get_value(MISS_DOCTYPE, name, _MISS_FIELDS, as_dict=True) or {}
    result["payroll_recalc_required"] = payroll_state == "calculated"
    return result


@frappe.whitelist()
def remind_pending_checkout_misses(names=None, within_hours: int = 24) -> dict:
    """HR — nudge employees whose explanation window is closing (P2 §8).

    Scans Pending tickets whose ``grace_deadline`` is still ahead but within
    ``within_hours``; writes ONE core Notification Log per user (portal bell)
    + realtime ping. A single notify failure never fails the whole call.
    """
    _require_hr()
    try:
        rows = (
            frappe.db.get_all(
                MISS_DOCTYPE,
                filters=[["docstatus", "<", 2], ["status", "=", "Pending"]],
                fields=["name", "employee", "employee_name", "grace_deadline"],
            )
            or []
        )
    except Exception:
        frappe.log_error(title="checkout_miss.remind list failed")
        rows = []
    wanted = {str(n or "").strip() for n in (names or []) if str(n or "").strip()} or None
    now = tz_wall(now_datetime())
    window = timedelta(hours=max(1, pagination.as_int(within_hours, 24)))
    due = []
    for r in rows:
        if wanted and r.get("name") not in wanted:
            continue
        dl = r.get("grace_deadline")
        if not dl:
            continue
        try:
            remaining = tz_wall(get_datetime(dl)) - now
        except Exception:
            continue
        if timedelta(0) < remaining <= window:
            due.append(r)
    users: dict = {}
    for r in due:
        uid = frappe.db.get_value("Employee", r.get("employee"), "user_id")
        if uid:
            users.setdefault(uid, []).append(r)
    sent = 0
    for uid, tickets in users.items():
        try:
            frappe.get_doc(
                {
                    "doctype": "Notification Log",
                    "for_user": uid,
                    "type": "Alert",
                    "subject": _("Sắp hết hạn giải trình quên checkout"),
                    "email_content": _("{0} ticket quên checkout sắp hết hạn giải trình: {1}").format(
                        len(tickets), ", ".join(str(t.get("name")) for t in tickets)
                    ),
                }
            ).insert(ignore_permissions=True)
            sent += 1
        except Exception:
            frappe.log_error(title="checkout_miss.remind notify failed")
    _publish("checkout_miss_reminder", {"users": sent, "tickets": len(due)})
    return {"reminded": sent, "tickets": len(due)}


# --------------------------------------------------------------------------- #
# P2 §8 UX round — monthly mini-chart buckets (sort whitelist lives above).
# --------------------------------------------------------------------------- #
def _shift_month(base: date, offset: int) -> date:
    """``base``'s month shifted by ``offset`` months (day=1, overflow-safe)."""
    total = base.year * 12 + (base.month - 1) + offset
    return date(total // 12, total % 12 + 1, 1)


@frappe.whitelist()
def checkout_miss_stats(months: int = 6) -> dict:
    """HR — monthly buckets for the admin mini-chart (P2 §8).

    Returns ``{"months": [{"month": "YYYY-MM", "tickets", "pending",
    "explained", "waived", "penalised", "closed", "penalty_total"}]}``,
    oldest → newest, covering the last ``months`` calendar months (clamped
    1..24). ``penalty_total`` sums Penalised non-waived tickets only (parity
    ``load_checkout_miss_penalty``).
    """
    _require_hr()
    n = max(1, min(pagination.as_int(months, 6), 24))
    today = date.today()
    keys = [_shift_month(today, -i).strftime("%Y-%m") for i in range(n - 1, -1, -1)]
    first = f"{keys[0]}-01"
    rows = pagination.all_rows(
        MISS_DOCTYPE,
        fields=["work_date", "status", "penalty_amount", "penalty_waived"],
        filters=[["docstatus", "<", 2], ["work_date", ">=", first]],
    )
    buckets = {
        k: {
            "month": k,
            "tickets": 0,
            "pending": 0,
            "explained": 0,
            "waived": 0,
            "penalised": 0,
            "closed": 0,
            "penalty_total": 0.0,
        }
        for k in keys
    }
    for r in rows or []:
        k = str(r.get("work_date") or "")[:7]
        bucket = buckets.get(k)
        if not bucket:
            continue
        bucket["tickets"] += 1
        status = (r.get("status") or "").strip().lower()
        if status in bucket:
            bucket[status] += 1
        if status == "penalised" and not r.get("penalty_waived"):
            bucket["penalty_total"] += float(r.get("penalty_amount") or 0)
    return {"months": [buckets[k] for k in keys]}


# --------------------------------------------------------------------------- #
# Desk-free COMPLETE — group A (plans/plan-checkout-miss-deskfree-complete.md
# §3 A1-A6): collaboration layer (comment thread, Assign To/ToDo, real File
# attachments, print PDF), bulk grace extension and run-engine-now. All
# stub-safe: optional Frappe machinery is imported lazily and every failure is
# logged, never propagated into the primary flow.
# --------------------------------------------------------------------------- #
_COMMENT_MAX = 2000
ASSIGN_MAX = 5

# Seeded by setup_checkout_miss_deskfree.seed() (S4) — referenced here so the
# endpoint and the fixture can never drift apart.
PRINT_FORMAT = "Biên bản giải trình quên checkout"


def _require_hr_or_owner(ticket_emp: str | None) -> None:
    """Collaboration endpoints: an HR user OR the ticket's own employee."""
    roles = set(emp_utils.get_user_roles() or [])
    if roles & emp_utils.HR_MANAGER_ROLES:
        return
    emp = emp_utils.get_employee_for_user()
    if not emp or not ticket_emp or emp != ticket_emp:
        frappe.throw(_("Bạn không có quyền thao tác ticket này."), frappe.PermissionError)


def _ticket_assignees(name: str) -> list[dict]:
    """Open ToDo rows assigned on this ticket (best-effort, stub-safe).

    The assignee is ``allocated_to`` — NOT ``owner`` (which Frappe stamps with
    the user who CREATED the ToDo, i.e. the assigner).
    """
    try:
        return [
            {
                "user": r.get("allocated_to") or r.get("owner"),
                "assigned_by": r.get("assigned_by") or r.get("owner"),
                "creation": r.get("creation"),
            }
            for r in (
                frappe.db.get_all(
                    "ToDo",
                    filters=[
                        ["reference_type", "=", MISS_DOCTYPE],
                        ["reference_name", "=", name],
                        ["status", "=", "Open"],
                    ],
                    fields=["allocated_to", "owner", "assigned_by", "creation"],
                    order_by="creation desc",
                    limit_page_length=ASSIGN_MAX,
                )
                or []
            )
        ]
    except Exception:
        frappe.log_error(title="checkout_miss.assignees list failed")
        return []


def _ticket_attachments(name: str) -> list[dict]:
    """Standard File attachments on the ticket (best-effort, stub-safe)."""
    try:
        return [
            {
                "name": r.get("name"),
                "file_name": r.get("file_name"),
                "file_url": r.get("file_url"),
                "file_size": r.get("file_size"),
            }
            for r in (
                frappe.db.get_all(
                    "File",
                    filters={"attached_to_doctype": MISS_DOCTYPE, "attached_to_name": name},
                    fields=["name", "file_name", "file_url", "file_size"],
                    order_by="creation desc",
                    limit_page_length=50,
                )
                or []
            )
        ]
    except Exception:
        frappe.log_error(title="checkout_miss.attachments list failed")
        return []


def _ticket_comments(name: str, limit: int = 50) -> list[dict]:
    """Comment thread (newest first, best-effort, stub-safe)."""
    try:
        return [
            {
                "name": r.get("name"),
                "actor": r.get("owner"),
                "at": r.get("creation"),
                "content": r.get("content") or "",
            }
            for r in (
                frappe.db.get_all(
                    "Comment",
                    filters={
                        "comment_type": "Comment",
                        "reference_doctype": MISS_DOCTYPE,
                        "reference_name": name,
                    },
                    fields=["name", "owner", "creation", "content"],
                    order_by="creation desc",
                    limit_page_length=limit,
                )
                or []
            )
        ]
    except Exception:
        frappe.log_error(title="checkout_miss.comments list failed")
        return []


@frappe.whitelist()
def comment_checkout_miss(name: str, text: str) -> dict:
    """HR or the ticket's employee — add a comment to the thread (A1).

    Written as a plain ``Comment`` doc (uniform stub/bench behaviour — the
    Desk sidebar's own storage), then realtime-published so both views sync.
    """
    name = (name or "").strip()
    text = (text or "").strip()
    if not text:
        frappe.throw(_("Nội dung bình luận là bắt buộc."))
    if len(text) > _COMMENT_MAX:
        frappe.throw(_("Bình luận tối đa {0} ký tự.").format(_COMMENT_MAX))
    if not name or not frappe.db.exists(MISS_DOCTYPE, name):
        frappe.throw(_("Ticket không tồn tại."))
    ticket_emp = frappe.db.get_value(MISS_DOCTYPE, name, "employee")
    _require_hr_or_owner(ticket_emp)
    frappe.get_doc(
        {
            "doctype": "Comment",
            "comment_type": "Comment",
            "reference_doctype": MISS_DOCTYPE,
            "reference_name": name,
            "content": text,
        }
    ).insert(ignore_permissions=True)
    _publish("checkout_miss_updated", {"ticket": name, "comment": 1})
    return {"ticket": name, "comments": _ticket_comments(name)}


@frappe.whitelist()
def get_checkout_miss_comments(name: str) -> list[dict]:
    """HR or the ticket's employee — read the comment thread (A1).

    A separate light reader so the employee view never needs the HR-gated
    ``get_checkout_miss`` detail endpoint.
    """
    name = (name or "").strip()
    if not name or not frappe.db.exists(MISS_DOCTYPE, name):
        frappe.throw(_("Ticket không tồn tại."))
    ticket_emp = frappe.db.get_value(MISS_DOCTYPE, name, "employee")
    _require_hr_or_owner(ticket_emp)
    return _ticket_comments(name)


@frappe.whitelist()
def assign_checkout_miss(name: str, assignees) -> dict:
    """HR — sync the ticket's assignee set (standard ToDo, parity onboarding).

    ``assignees`` is the FULL desired set (add missing, remove extra); capped
    at :data:`ASSIGN_MAX`. Assign machinery failures are logged per user —
    they never abort the sync (parity ``_sync_todo``).
    """
    _require_hr()
    name, _doc = _load_ticket(name)
    if isinstance(assignees, str):
        try:
            parsed = json.loads(assignees)
        except Exception:
            parsed = [assignees]
    else:
        parsed = assignees or []
    users: list[str] = []
    for u in parsed:
        s = str(u or "").strip()
        if s and s not in users:
            users.append(s)
    if len(users) > ASSIGN_MAX:
        frappe.throw(_("Tối đa {0} người được phân công.").format(ASSIGN_MAX))
    current = [a.get("user") for a in _ticket_assignees(name)]
    # NOTE: desk assign_to.add() also calls frappe.share.add(), whose permission
    # gate rejects non-Administrator sessions even with share=1 on the DocPerm.
    # A direct ToDo insert is the same artefact (bell/Desk ToDo) minus that
    # side-effect — deliberately NOT sharing the doc with the assignee.
    for u in users:
        if u in current:
            continue
        try:
            frappe.get_doc(
                {
                    "doctype": "ToDo",
                    "allocated_to": u,
                    "reference_type": MISS_DOCTYPE,
                    "reference_name": name,
                    "description": _("Xử lý ticket quên checkout {0}").format(name),
                    "priority": "Medium",
                    "status": "Open",
                    "assigned_by": frappe.session.user,
                }
            ).insert(ignore_permissions=True)
        except Exception:
            frappe.log_error(title="checkout_miss.assign add failed")
    for u in current:
        if u in users:
            continue
        try:
            for td in frappe.db.get_all(
                "ToDo",
                filters=[
                    ["reference_type", "=", MISS_DOCTYPE],
                    ["reference_name", "=", name],
                    ["allocated_to", "=", u],
                    ["status", "=", "Open"],
                ],
                fields=["name"],
            ):
                frappe.delete_doc("ToDo", td.get("name"), ignore_permissions=True)
        except Exception:
            frappe.log_error(title="checkout_miss.assign remove failed")
    _publish("checkout_miss_updated", {"ticket": name, "assigned": users})
    return {"ticket": name, "assignees": _ticket_assignees(name)}


@frappe.whitelist()
def remove_checkout_miss_evidence(name: str, file_url: str) -> dict:
    """HR — delete one evidence file (attached File doc + joined URL, A3).

    Refused while the covering payroll period is locked (parity delete). The
    legacy ``evidence_ref`` string keeps its other URLs so old rows still
    render something.
    """
    _require_hr()
    name, doc = _load_ticket(name)
    if (
        _payroll_state_for(
            {"work_date": doc.work_date, "employee": doc.employee, "company": doc.company}
        )
        == "locked"
    ):
        frappe.throw(
            _("Kỳ lương đã phê duyệt — không thể sửa minh chứng ticket này."),
            frappe.ValidationError,
        )
    file_url = (file_url or "").strip()
    if not file_url:
        frappe.throw(_("Đường dẫn tệp là bắt buộc."))
    try:
        for f in frappe.db.get_all(
            "File",
            filters={
                "attached_to_doctype": MISS_DOCTYPE,
                "attached_to_name": name,
                "file_url": file_url,
            },
            fields=["name"],
        ):
            frappe.delete_doc("File", f.get("name"), ignore_permissions=True)
    except Exception:
        frappe.log_error(title="checkout_miss.evidence file delete failed")
    if getattr(doc, "evidence_ref", None):
        kept = [s for s in re.split(r"[,\s]+", str(doc.evidence_ref)) if s and s != file_url]
        doc.evidence_ref = ", ".join(kept)
        doc.save(ignore_permissions=True)
    _audit_override(doc, name, f"remove evidence {file_url}", old_value=str(file_url))
    _publish("checkout_miss_updated", {"ticket": name, "evidence_removed": file_url})
    return {
        "ticket": name,
        "evidence_ref": getattr(doc, "evidence_ref", None) or "",
        "attachments": _ticket_attachments(name),
    }


@frappe.whitelist()
def download_checkout_miss_pdf(name: str) -> dict:
    """HR or the ticket's employee — print the resolution memo (A4).

    Delegates to Frappe's standard ``frappe.utils.print_format.download_pdf``
    which fills ``frappe.response`` with the PDF bytes; the seeded format is
    :data:`PRINT_FORMAT` (S4 fixture).
    """
    name = (name or "").strip()
    if not name or not frappe.db.exists(MISS_DOCTYPE, name):
        frappe.throw(_("Ticket không tồn tại."))
    ticket_emp = frappe.db.get_value(MISS_DOCTYPE, name, "employee")
    _require_hr_or_owner(ticket_emp)
    try:
        from frappe.utils.print_format import download_pdf  # lazy: stub-safe

        download_pdf(MISS_DOCTYPE, name, format=PRINT_FORMAT)
    except Exception as e:
        # Pass the underlying cause through — a Jinja/format error must stay
        # visible at the API, not swallowed by the Vietnamese wrapper.
        frappe.throw(
            _("Không tạo được PDF — kiểm tra Print Format '{0}'. {1}").format(PRINT_FORMAT, str(e)),
            frappe.ValidationError,
        )
    return {"ticket": name, "format": PRINT_FORMAT}


@frappe.whitelist()
def bulk_extend_checkout_miss_grace(names, new_deadline: str, reason: str | None = None) -> dict:
    """HR — extend many deadlines at once (partial-safe, cap 100, A5).

    Every per-ticket guard (status machine, future-deadline, payroll lock)
    keeps firing inside :func:`_extend_grace_one`; a failing row lands in
    ``failed`` while the rest still extend. Returns ``{updated, failed,
    counts}`` (parity :func:`bulk_resolve_checkout_misses`).
    """
    _require_hr()
    reason = (reason or "").strip()
    if not reason:
        frappe.throw(_("Lý do gia hạn là bắt buộc."))
    names = [str(n or "").strip() for n in (names or []) if str(n or "").strip()]
    if not names:
        frappe.throw(_("Chọn ít nhất một ticket."))
    if len(names) > BULK_MAX:
        frappe.throw(_("Tối đa {0} ticket mỗi lần.").format(BULK_MAX))
    if not (new_deadline or "").strip():
        frappe.throw(_("Thời hạn mới là bắt buộc."))
    updated: list[str] = []
    failed: list[dict] = []
    for n in names:
        try:
            _extend_grace_one(n, new_deadline, reason)
            updated.append(n)
        except Exception as e:
            failed.append({"name": n, "error": str(e).strip() or "Không xử lý được."})
    _publish(
        "checkout_miss_updated",
        {"action": "bulk", "kind": "extend_grace", "updated": len(updated), "failed": len(failed)},
    )
    return {"updated": updated, "failed": failed, "counts": {"extended": len(updated)}}


def _require_engine_runner() -> None:
    """Engine trigger — HR Manager / Payroll Manager / System Manager only."""
    roles = set(emp_utils.get_user_roles() or [])
    if not (roles & {"HR Manager", "Payroll Manager", "System Manager"}):
        frappe.throw(_("Chỉ HR Manager mới được chạy engine."), frappe.PermissionError)


@frappe.whitelist()
def run_checkout_miss_engine_now() -> dict:
    """HR Manager — run the hourly auto-close engine on demand (A6).

    ``run_hourly`` already commits per employee and stamps its own heartbeat
    (a dead engine must be VISIBLE), so this is a thin guarded trigger. The
    returned summary mirrors the heartbeat payload.
    """
    _require_engine_runner()
    from gege_hr.gege_hr.utils import checkout_miss as engine

    summary = engine.run_hourly() or {}
    out = {
        "closed": int(summary.get("closed") or 0),
        "penalised": int(summary.get("penalised") or 0),
    }
    _publish("checkout_miss_updated", {"action": "engine", **out})
    return out


# --------------------------------------------------------------------------- #
# Desk-free COMPLETE — group B (plan §3 B1/B6): email channel + evidence caps.
# --------------------------------------------------------------------------- #
# Email Template names — seeded by setup_checkout_miss_deskfree.seed() (S4);
# a missing template makes sendmail raise, which _email_notify swallows.
_EMAIL_TEMPLATES = {
    "created": "Checkout Miss — Ticket mới",
    "explain_ack": "Checkout Miss — Đã nhận giải trình",
    "waived": "Checkout Miss — Miễn phạt",
    "penalised": "Checkout Miss — Xác nhận phạt",
    "closed": "Checkout Miss — Đóng ticket",
    "grace_extended": "Checkout Miss — Gia hạn hạn giải trình",
    "appealed": "Checkout Miss — Khiếu nại mới",
}


def _email_notify(name: str, kind: str) -> None:
    """Best-effort event email (B1) — NEVER raises into the primary flow.

    Gated by ``vn_cm_email_enabled`` on VN HR Portal Setting (default off).
    ``appealed`` goes to every HR Manager user; the other kinds go to the
    ticket employee's portal user.
    """
    try:
        if not frappe.db.get_single_value("VN HR Portal Setting", "vn_cm_email_enabled"):
            return
        sendmail = getattr(frappe, "sendmail", None)
        template = _EMAIL_TEMPLATES.get(kind)
        if sendmail is None or not template:
            return
        ticket = (
            frappe.db.get_value(
                MISS_DOCTYPE,
                name,
                ["employee", "employee_name", "work_date", "status", "penalty_amount"],
                as_dict=True,
            )
            or {}
        )
        recipients: list[str] = []
        if kind == "appealed":
            for r in (
                frappe.db.get_all(
                    "Has Role",
                    filters={"role": "HR Manager", "parenttype": "User"},
                    fields=["parent"],
                    limit_page_length=20,
                )
                or []
            ):
                if r.get("parent") and r.get("parent") not in recipients:
                    recipients.append(r.get("parent"))
        else:
            uid = frappe.db.get_value("Employee", ticket.get("employee"), "user_id")
            if uid:
                recipients.append(uid)
        if not recipients:
            return
        sendmail(recipients=recipients, template=template, args={"doc": ticket, "ticket": name})
    except Exception:
        frappe.log_error(title="checkout_miss.email notify failed")


def _evidence_caps() -> dict:
    """Evidence caps from VN HR Portal Setting — ``0`` means UNLIMITED (B6).

    Mirrors the "never ``or``" config law: an unset (None) field falls back
    to the default, a legit ``0`` stays ``0``.
    """

    def _get(field: str, cast, default):
        try:
            v = frappe.db.get_single_value("VN HR Portal Setting", field)
            return cast(v) if v is not None else default
        except Exception:
            return default

    return {
        "max_evidence_files": _get("vn_cm_max_evidence_files", int, 5),
        "max_evidence_mb": _get("vn_cm_max_evidence_mb", float, 10.0),
    }


def _validate_evidence_caps(name: str, evidence_ref: str | None) -> None:
    """Enforce the configured caps on an explanation's evidence (B6).

    File count: URLs in the (joined) ``evidence_ref`` string. Size: attached
    File docs' ``file_size`` sum. Either cap at ``0`` disables that check.
    """
    caps = _evidence_caps()
    urls = [s for s in re.split(r"[,\s]+", str(evidence_ref or "")) if s]
    max_files = int(caps.get("max_evidence_files") or 0)
    if max_files and len(urls) > max_files:
        frappe.throw(
            _("Tối đa {0} tệp minh chứng mỗi giải trình (đã gửi {1}).").format(max_files, len(urls))
        )
    max_mb = float(caps.get("max_evidence_mb") or 0)
    if max_mb:
        try:
            files = (
                frappe.db.get_all(
                    "File",
                    filters={"attached_to_doctype": MISS_DOCTYPE, "attached_to_name": name},
                    fields=["file_size"],
                )
                or []
            )
            total_mb = sum(float(f.get("file_size") or 0) for f in files) / (1024.0 * 1024.0)
        except Exception:
            total_mb = 0.0
        if total_mb > max_mb:
            frappe.throw(
                _("Tổng dung lượng minh chứng vượt giới hạn {0}MB.").format(max_mb),
                frappe.ValidationError,
            )


def on_ticket_created(doc, method: str | None = None) -> None:
    """doc_events hook (after_insert) — email the employee on a NEW ticket (B1).

    Replaces the standard Notification fixture for this event: Frappe
    validates a Notification ``condition`` in a sandbox WITHOUT ``frappe.db``,
    so the ``vn_cm_email_enabled`` gate cannot live there. This hook rides the
    SAME best-effort, toggle-respecting ``_email_notify`` path as every other
    checkout-miss email — no engine change, no extra channel to maintain.
    """
    try:
        _email_notify(doc.get("name"), "created")
    except Exception:
        frappe.log_error(title="checkout_miss.on_ticket_created failed")


@frappe.whitelist()
def checkout_miss_meta() -> dict:
    """Employee-safe feature metadata (B6): upload caps for the explain form.

    No role guard — the caps are public feature limits, not sensitive config.
    """
    return _evidence_caps()


# --------------------------------------------------------------------------- #
# Desk-free COMPLETE — group C (plan §3 C1/C5/C6): server-driven transitions,
# bulk historical backfill, one-shot employee appeal.
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def get_checkout_miss_transitions() -> dict:
    """Single source of truth for the SPA state machine (C1).

    The FE fetches this once per view and falls back to its bundled copy when
    offline — the mirror can never silently drift again.
    """
    return {
        "actions": {k: sorted(v) for k, v in _ALLOWED_ACTIONS.items()},
        "can_extend_grace": ["Pending", "Explained"],
        "can_reopen": ["Closed"],
        "can_appeal": ["Penalised"],
    }


@frappe.whitelist()
def bulk_create_checkout_misses(rows) -> dict:
    """HR — backfill many historical tickets from JSON rows (C5, cap 100).

    Each row: ``{employee, work_date, shift_type?, note?, penalty_amount?}``.
    Partial-safe (parity :func:`bulk_resolve_checkout_misses`); every guard of
    the single-create endpoint keeps firing per row.
    """
    _require_hr()
    if isinstance(rows, str):
        try:
            rows = json.loads(rows)
        except Exception:
            rows = None
    if not isinstance(rows, list):
        frappe.throw(_("Danh sách ticket không hợp lệ (JSON)."))
    if not rows:
        frappe.throw(_("Chọn ít nhất một dòng."))
    if len(rows) > BULK_MAX:
        frappe.throw(_("Tối đa {0} ticket mỗi lần.").format(BULK_MAX))
    updated: list[str] = []
    failed: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            failed.append({"name": str(row)[:50], "error": "Dòng không hợp lệ."})
            continue
        label = f"{row.get('employee') or '?'}@{row.get('work_date') or '?'}"
        try:
            doc = _create_one(
                row.get("employee"),
                row.get("work_date"),
                row.get("shift_type"),
                row.get("note"),
                row.get("penalty_amount"),
            )
            updated.append(doc.get("name"))
        except Exception as e:
            failed.append({"name": label, "error": str(e).strip() or "Không tạo được."})
    _publish(
        "checkout_miss_updated",
        {"action": "bulk", "kind": "create", "updated": len(updated), "failed": len(failed)},
    )
    return {"updated": updated, "failed": failed, "counts": {"created": len(updated)}}


@frappe.whitelist()
@_rate_limited
def appeal_checkout_miss(name: str, text: str) -> dict:
    """Employee — ONE appeal against a Penalised ticket (C6).

    Flips the ticket back to Explained (re-enters the HR queue) with the
    appeal text kept separately (``appeal_text``); audited as "Checkout Miss
    Appeal". Payroll guards mirror resolve: ``locked`` → refused;
    ``calculated`` → the response flags ``payroll_recalc_required`` (the
    penalty stops counting the moment the status leaves Penalised).
    """
    name = (name or "").strip()
    text = (text or "").strip()
    if not text:
        frappe.throw(_("Nội dung khiếu nại là bắt buộc."))
    if len(text) > _COMMENT_MAX:
        frappe.throw(_("Khiếu nại tối đa {0} ký tự.").format(_COMMENT_MAX))
    if not name or not frappe.db.exists(MISS_DOCTYPE, name):
        frappe.throw(_("Ticket không tồn tại."))
    ticket = (
        frappe.db.get_value(
            MISS_DOCTYPE, name, ["employee", "status", "appeal_count", "work_date", "company"], as_dict=True
        )
        or {}
    )
    ticket_emp = ticket.get("employee")
    emp = emp_utils.get_employee_for_user()
    if not emp or (ticket_emp and emp != ticket_emp):
        frappe.throw(_("Bạn chỉ được khiếu nại ticket của mình."), frappe.PermissionError)
    status = (ticket.get("status") or "").strip()
    if status != "Penalised":
        frappe.throw(_("Chỉ ticket đã bị phạt mới có thể khiếu nại."))
    try:
        prior = int(ticket.get("appeal_count") or 0)
    except (TypeError, ValueError):
        prior = 0
    if prior >= 1:
        frappe.throw(_("Mỗi ticket chỉ được khiếu nại một lần."))
    payroll_state = _payroll_state_for(
        {
            "work_date": ticket.get("work_date"),
            "employee": ticket.get("employee"),
            "company": ticket.get("company"),
        }
    )
    if payroll_state == "locked":
        frappe.throw(
            _(
                "Kỳ lương chứa ngày {0} đã phê duyệt và sinh phiếu lương — hãy xử "
                "lý bù/trừ qua VN Payroll Adjustment thay vì khiếu nại."
            ).format(ticket.get("work_date")),
            frappe.ValidationError,
        )
    now = tz_wall(now_datetime()).strftime("%Y-%m-%d %H:%M:%S")
    frappe.db.set_value(
        MISS_DOCTYPE,
        name,
        {"status": "Explained", "appeal_count": 1, "appeal_text": text, "appealed_on": now},
    )
    try:
        audit_api.log(
            "Checkout Miss Appeal",
            company=ticket.get("company"),
            employee=ticket.get("employee"),
            work_date=ticket.get("work_date"),
            reference_doctype=MISS_DOCTYPE,
            reference_name=name,
            description=f"employee appealed: {text[:120]}",
            old_value="Penalised",
            new_value="Explained",
        )
    except Exception:
        pass
    _publish("checkout_miss_updated", {"ticket": name, "appealed": 1})
    _email_notify(name, "appealed")  # B1 — HR managers get pinged
    result = frappe.db.get_value(MISS_DOCTYPE, name, _MISS_FIELDS, as_dict=True) or {}
    result["payroll_recalc_required"] = payroll_state == "calculated"
    return result
