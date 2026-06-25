"""
Unified Approval Inbox API — plan v5 §10.6 / doctype-design §15.

These endpoints front the SPA approval inbox (``hr-ui/src/composables/useApprovals.js``)
and map 1:1 to ``gege_hr.gege_hr.api.approval.<fn>`` calls in
``hr-ui/src/api/index.js``:

  * ``get_pending_approvals`` — pending requests the approver can act on, grouped
    by transaction type. Returns ``{ groups: [...] }`` (or a flat list).
  * ``approve_request``     — advance one request to its next approval state.
  * ``reject_request``      — send a request to Rejected.
  * ``bulk_approve``        — approve many requests of one type at once.
  * ``bulk_reject``         — reject many requests of one type at once.

Routing is driven by :doc:`VN Approval Matrix` (with :doc:`VN Approval Step`
children); every transition is recorded in :doc:`VN Approval Log`. The pure
state-machine lives in :mod:`gege_hr.gege_hr.utils.approval` so it can be tested
without a bench.
"""

from __future__ import annotations

import frappe
from frappe import _

from gege_hr.gege_hr.api import audit as audit_api
from gege_hr.gege_hr.utils import approval as rules
from gege_hr.gege_hr.utils import employee as emp_utils
from gege_hr.gege_hr.utils import notify


# --------------------------------------------------------------------------- #
# Bench loaders (frappe-aware; never imported by bench-free tests).
# --------------------------------------------------------------------------- #
def _user() -> str:
    return emp_utils.get_current_user() or "Guest"


def _resolve_approver(approver: str | None) -> str:
    """The user whose inbox we are reading/acting for.

    Defaults to the session user. A manager may pass any approver (e.g. to view a
    delegate's queue); a plain employee is locked to themselves.
    """
    approver = (approver or "").strip()
    if approver and approver != _user():
        roles = set(emp_utils.get_user_roles() or [])
        if not (roles & emp_utils.HR_MANAGER_ROLES):
            approver = _user()
    return approver or _user()


def _load_matrices(transaction_type: str, company: str | None) -> list[dict]:
    """Active matrices for a transaction type (+optional company), with steps."""
    filters = {"transaction_type": transaction_type, "is_active": 1}
    if company:
        filters["company"] = company
    try:
        names = frappe.db.get_all("VN Approval Matrix", filters=filters, pluck="name")
    except Exception:
        return []
    matrices = []
    for name in names:
        try:
            doc = frappe.get_doc("VN Approval Matrix", name)
        except Exception:
            continue
        matrices.append(
            {
                "name": doc.name,
                "apply_to": doc.apply_to or "All",
                "branch": doc.branch,
                "department": doc.department,
                "employee_grade": doc.employee_grade,
                "modified": str(doc.modified or ""),
                "steps": [
                    {
                        "step_no": s.step_no,
                        "approver_type": s.approver_type,
                        "approver_user": s.approver_user,
                        "approver_role": s.approver_role,
                    }
                    for s in (doc.steps or [])
                ],
            }
        )
    return matrices


def _employee_attrs(employee: str) -> dict:
    """Requester attributes needed for matrix scoping + approver resolution."""
    if not employee:
        return {}
    try:
        vals = (
            frappe.db.get_value(
                "Employee",
                employee,
                ["company", "branch", "department", "grade", "reports_to"],
                as_dict=True,
            )
            or {}
        )
    except Exception:
        vals = {}
    attrs = {
        "company": vals.get("company"),
        "branch": vals.get("branch"),
        "department": vals.get("department"),
        "employee_grade": vals.get("grade"),
        "reports_to": vals.get("reports_to"),
        "line_manager_user": None,
        "dept_head_user": None,
    }
    # Resolve the line manager's user (reports_to Employee → user_id).
    if vals.get("reports_to"):
        attrs["line_manager_user"] = frappe.db.get_value("Employee", vals["reports_to"], "user_id")
    # Department head user, if the Department doctype exposes it.
    if vals.get("department"):
        attrs["dept_head_user"] = (
            frappe.db.get_value("Department", vals["department"], "leave_approvers") or None
        )
    return attrs


def _config(transaction_type: str) -> dict:
    cfg = rules.TRANSACTION_CONFIG.get(transaction_type)
    if not cfg:
        frappe.throw(_("Loại yêu cầu không hỗ trợ: {0}").format(transaction_type))
    return cfg


# Flat row shape returned to the SPA — keeps the inbox list renderable without a
# second lookup (matches the row docstring in hr-ui/src/api/index.js).
# Fields common to every request row (none of these are type-specific, so they
# never risk a "column does not exist" error on a given DocType).
_BASE_FIELDS = [
    "name",
    "employee",
    "employee_name",
    "docstatus",
    "creation",
]

# Type-specific fields — only fields that actually exist on the DocType. Querying
# a non-existent column raises in Frappe, so each type carries its own list (the
# date / status / detail fields differ across Leave / OT / Correction / Advance).
_TYPE_FIELDS: dict[str, list[str]] = {
    "Leave Application": [
        "status",
        "from_date",
        "to_date",
        "leave_type",
        "total_leave_days",
        "description",
    ],
    "Overtime Request": [
        "workflow_state",
        "company",
        "work_date",
        "overtime_type",
        "requested_hours",
        "reason",
    ],
    "Correction Request": [
        "workflow_state",
        "company",
        "work_date",
        "correction_type",
        "reason",
    ],
    "Salary Advance Request": [
        "workflow_state",
        "company",
        "posting_date",
        "requested_amount",
        "eligible_amount",
        "approved_amount",
        "payment_status",
        "reason",
    ],
}


def _row_fields(transaction_type: str) -> list[str]:
    """All selectable columns for one transaction type's query."""
    return list(_BASE_FIELDS) + list(_TYPE_FIELDS.get(transaction_type, []))


def _normalise_row(row: dict, transaction_type: str, approver: str) -> dict:
    """Stamp request_type + approver so the FE inbox groups/labels correctly.

    Also normalises the display date onto ``work_date`` (the FE's canonical
    field) for types that use a different date column (Salary Advance uses
    ``posting_date``; Leave already carries ``from_date``).
    """
    out = dict(row or {})
    out["request_type"] = transaction_type
    out["approver"] = approver
    if transaction_type == "Salary Advance Request":
        out.setdefault("work_date", out.get("posting_date"))
        # Surface a single amount for the FE's summary tile (hrStatus ADVANCE).
        out["advance_amount"] = out.get("requested_amount")
    # Unify the human reason/description onto a single ``reason`` field so the
    # approval-inbox card always has something to show the manager. Leave
    # Application stores its reason in ``description`` (Frappe has no ``reason``
    # column); OT / Correction / Advance use ``reason``. Prefer an explicit
    # ``reason`` when present, otherwise fall back to ``description``.
    unified_reason = (out.get("reason") or out.get("description") or "").strip()
    if unified_reason:
        out["reason"] = unified_reason
    elif "reason" not in out:
        out["reason"] = ""
    return out


def _user_can_act(doc_dict: dict, transaction_type: str, user: str) -> bool:
    """Does ``user`` hold the active approval step for this request?"""
    company = doc_dict.get("company")
    matrices = _load_matrices(transaction_type, company)
    if not matrices:
        # No matrix configured → fall back to coarse role gating: HR/Line Manager
        # roles may act on any pending request of that type.
        roles = set(emp_utils.get_user_roles() or [])
        return bool(roles & (emp_utils.HR_MANAGER_ROLES | {"HR User", "Line Manager"}))

    attrs = _employee_attrs(doc_dict.get("employee"))
    matrix = rules.pick_matrix(matrices, attrs)
    if not matrix:
        return False
    cfg = rules.TRANSACTION_CONFIG[transaction_type]
    state = doc_dict.get(cfg["status_field"])
    step = rules.current_step(matrix, state)
    if not step:
        return False
    roles = set(emp_utils.get_user_roles() or [])
    return rules.approver_matches(
        step,
        user=user,
        roles=roles,
        line_manager_user=attrs.get("line_manager_user"),
        dept_head_user=attrs.get("dept_head_user"),
    )


def _write_log(
    *,
    transaction_type: str,
    name: str,
    action: str,
    from_state: str | None,
    to_state: str | None,
    comment: str | None,
    actor: str,
) -> None:
    """Append a VN Approval Log row (best-effort: never fail the transition)."""
    doctype = rules.TRANSACTION_CONFIG[transaction_type]["doctype"]
    try:
        log = frappe.new_doc("VN Approval Log")
        log.update(
            {
                "reference_doctype": doctype,
                "reference_name": name,
                "action": action,
                "from_state": from_state or "",
                "to_state": to_state or "",
                "actor": actor,
                "comment": comment or "",
            }
        )
        log.insert()
    except Exception:
        frappe.log_error(
            title="VN Approval Log write failed",
            message=f"{doctype} {name} {action}",
        )


def _delegate_leave(name: str, request_type: str, comment: str | None, *, approved: bool) -> dict:
    """Leave Application approve/reject that actually persists.

    Core Frappe Leave Application derives ``status`` from ``docstatus`` and
    HRMS's ``validate`` recomputes it on every ``save()`` — so a plain
    ``doc.status = "Approved"; doc.save()`` on a submitted (docstatus 1) leave
    is silently reverted and the request stays ``Open`` (reappearing in the
    inbox). The unified inbox filters on the ``status`` column, so we persist
    the decision there directly (bypassing the recomputing validate), stamp the
    approver, and run the leave-specific audit + notify hooks best-effort.
    """
    from gege_hr.gege_hr.api import leave as leave_api

    action = "Approve" if approved else "Reject"
    target_state = "Approved" if approved else "Rejected"
    doc = frappe.get_doc("Leave Application", name)
    current = doc.get("status")
    if current == target_state:
        return {
            "name": name,
            "request_type": request_type,
            "status": target_state,
            "message": _("Đơn nghỉ phép {0} đã ở trạng thái {1}.").format(name, target_state),
        }
    if current not in ("Open", "Draft"):
        frappe.throw(
            _("Đơn nghỉ đang ở trạng thái {0} — không thể xử lý.").format(current),
            frappe.PermissionError,
        )

    # Stamp the approver when the field exists, then persist the decision on the
    # status column the inbox reads (db.set_value skips HRMS's recomputing validate).
    updates = {"status": target_state}
    try:
        if approved and doc.meta.has_field("leave_approver"):
            updates["leave_approver"] = _user()
    except Exception:
        pass
    frappe.db.set_value("Leave Application", name, updates, update_modified=False)

    # Best-effort: notify the employee + append a leave comment (never abort).
    try:
        leave_api._after_leave_decision(doc, approved=approved, reason=(comment or ""))
    except Exception:
        pass
    try:
        if not approved and comment:
            doc.add_comment("Comment", _("Lý do từ chối: {0}").format(comment))
    except Exception:
        pass

    _write_log(
        transaction_type=request_type,
        name=name,
        action=action,
        from_state=current,
        to_state=target_state,
        comment=comment,
        actor=_user(),
    )
    return {
        "name": name,
        "request_type": request_type,
        "status": target_state,
        "message": _("Đã duyệt đơn nghỉ phép {0}.").format(name)
        if approved
        else _("Đã từ chối đơn nghỉ phép {0}.").format(name),
    }


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@frappe.whitelist()
def get_pending_approvals(
    approver: str | None = None,
    request_type: str | None = None,
    date: str | None = None,
) -> dict:
    """Plan §10.6 — pending requests the approver can act on, grouped by type.

    Returns ``{ groups: [{ request_type, label, count, requests: [] }] }``. An
    empty ``request_type`` returns every supported type.
    """
    user = _resolve_approver(approver)
    types = [request_type] if request_type else rules.supported_types()

    groups = []
    for ttype in types:
        cfg = rules.TRANSACTION_CONFIG.get(ttype)
        if not cfg:
            continue
        filters = {cfg["status_field"]: ["in", cfg["pending_states"]]}
        if date:
            filters[cfg["date_field"]] = [">=", date]
        try:
            rows = frappe.db.get_all(cfg["doctype"], filters=filters, fields=_row_fields(ttype))
        except Exception:
            rows = []

        # Keep only the rows the approver may actually act on.
        actionable = [_normalise_row(r, ttype, user) for r in rows if _user_can_act(r, ttype, user)]
        if not actionable:
            continue
        groups.append(
            {
                "request_type": ttype,
                "label": rules.TYPE_LABELS.get(ttype, ttype),
                "count": len(actionable),
                "requests": actionable,
            }
        )

    return {"groups": groups}


@frappe.whitelist()
def approve_request(
    name: str | None = None,
    request_type: str | None = None,
    comment: str | None = None,
) -> dict:
    """Advance one request to its next approval state (→ Approved on last step).

    Returns ``{ name, request_type, status, message }``.
    """
    if not name or not request_type:
        frappe.throw(_("Thiếu mã yêu cầu / loại yêu cầu."))
    cfg = _config(request_type)
    user = _user()

    doc = frappe.get_doc(cfg["doctype"], name)
    current = doc.get(cfg["status_field"])
    if current not in cfg["pending_states"]:
        frappe.throw(
            _("Yêu cầu không ở trạng thái chờ duyệt ({0}).").format(current),
            frappe.PermissionError,
        )
    if not _user_can_act(doc.as_dict(), request_type, user):
        frappe.throw(
            _("Bạn không phải người duyệt ở bước này."),
            frappe.PermissionError,
        )

    # Leave Application status is managed by core Frappe HR via docstatus, not a
    # free `status` field — so delegate to the HRMS-aware leave endpoints that
    # submit/cancel correctly (the matrix step states don't apply here).
    if request_type == "Leave Application":
        return _delegate_leave(name, request_type, comment, approved=True)

    # Resolve the next state from the matrix (→ Approved when last step done).
    matrices = _load_matrices(request_type, doc.get("company"))
    attrs = _employee_attrs(doc.get("employee"))
    matrix = rules.pick_matrix(matrices, attrs) if matrices else None
    next_state = (
        rules.next_state_after(current, matrix.get("steps") or []) if matrix else cfg["approve_state"]
    )

    doc.set(cfg["status_field"], next_state)
    doc.save()

    _write_log(
        transaction_type=request_type,
        name=name,
        action="Approve",
        from_state=current,
        to_state=next_state,
        comment=comment,
        actor=user,
    )

    msg = (
        _("Đã duyệt ({0}).").format(next_state)
        if next_state != cfg["approve_state"]
        else _("Đã duyệt hoàn tất.")
    )

    # Notify the requesting employee of the outcome (best-effort, bench-guarded).
    try:
        notify.push_request_outcome(
            transaction_type=request_type,
            name=name,
            employee=doc.get("employee"),
            outcome="approved",
            state=next_state,
        )
    except Exception:
        pass

    # Append an audit row for the matrix-driven approve (OT/Correction/Advance).
    audit_type = audit_api.APPROVE_AUDIT_TYPE.get(request_type)
    if audit_type:
        audit_api.log(
            audit_type,
            doc=doc.as_dict(),
            description=f"{current} → {next_state}",
            old_value=current,
            new_value=next_state,
        )

    return {
        "name": name,
        "request_type": request_type,
        "status": next_state,
        "message": msg,
    }


@frappe.whitelist()
def reject_request(
    name: str | None = None,
    request_type: str | None = None,
    comment: str | None = None,
) -> dict:
    """Reject one request (any approver holding the current step).

    Returns ``{ name, request_type, status, message }``.
    """
    if not name or not request_type:
        frappe.throw(_("Thiếu mã yêu cầu / loại yêu cầu."))
    cfg = _config(request_type)
    user = _user()

    doc = frappe.get_doc(cfg["doctype"], name)
    current = doc.get(cfg["status_field"])
    if current not in cfg["pending_states"]:
        frappe.throw(
            _("Yêu cầu không ở trạng thái chờ duyệt ({0}).").format(current),
            frappe.PermissionError,
        )
    if not _user_can_act(doc.as_dict(), request_type, user):
        frappe.throw(
            _("Bạn không phải người duyệt ở bước này."),
            frappe.PermissionError,
        )

    # Leave Application: delegate to the HRMS-aware reject (see approve_request).
    if request_type == "Leave Application":
        return _delegate_leave(name, request_type, comment, approved=False)

    doc.set(cfg["status_field"], cfg["reject_state"])
    doc.save()

    _write_log(
        transaction_type=request_type,
        name=name,
        action="Reject",
        from_state=current,
        to_state=cfg["reject_state"],
        comment=comment,
        actor=user,
    )

    # Notify the requesting employee their request was rejected (best-effort).
    try:
        notify.push_request_outcome(
            transaction_type=request_type,
            name=name,
            employee=doc.get("employee"),
            outcome="rejected",
            state=cfg["reject_state"],
        )
    except Exception:
        pass

    return {
        "name": name,
        "request_type": request_type,
        "status": cfg["reject_state"],
        "message": _("Đã từ chối yêu cầu."),
    }


@frappe.whitelist()
def bulk_approve(
    names: str | list | None = None,
    request_type: str | None = None,
    comment: str | None = None,
) -> dict:
    """Approve many requests of one type at once (plan §10.6).

    ``names`` may arrive as a JSON string (whitelist deserialisation) or a list.
    Returns ``{ approved: Number, failed: Number, message }``.
    """
    if not request_type:
        frappe.throw(_("Thiếu loại yêu cầu."))
    if isinstance(names, str):
        import json

        try:
            names = json.loads(names)
        except Exception:
            names = [names]
    names = list(names or [])
    if not names:
        frappe.throw(_("Thiếu danh sách yêu cầu."))

    approved = 0
    failed = 0
    for nm in names:
        try:
            approve_request(name=nm, request_type=request_type, comment=comment)
            approved += 1
        except Exception:
            failed += 1

    return {
        "approved": approved,
        "failed": failed,
        "message": _("Đã duyệt {0}, thất bại {1}.").format(approved, failed),
    }


@frappe.whitelist()
def bulk_reject(
    names: str | list | None = None,
    request_type: str | None = None,
    comment: str | None = None,
) -> dict:
    """Reject many requests of one type at once (plan §10.6).

    Mirrors ``bulk_approve``: each request is rejected independently so one
    failure (wrong state / not the current approver) does not abort the rest.
    ``comment`` is stamped on every successful rejection (rejection reason).
    Returns ``{ rejected: Number, failed: Number, message }``.
    """
    if not request_type:
        frappe.throw(_("Thiếu loại yêu cầu."))
    if isinstance(names, str):
        import json

        try:
            names = json.loads(names)
        except Exception:
            names = [names]
    names = list(names or [])
    if not names:
        frappe.throw(_("Thiếu danh sách yêu cầu."))

    rejected = 0
    failed = 0
    for nm in names:
        try:
            reject_request(name=nm, request_type=request_type, comment=comment)
            rejected += 1
        except Exception:
            failed += 1

    return {
        "rejected": rejected,
        "failed": failed,
        "message": _("Đã từ chối {0}, thất bại {1}.").format(rejected, failed),
    }
