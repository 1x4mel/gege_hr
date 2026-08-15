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
    # Guarded: ``leave_approvers`` is an ERPNext HR custom field that may not be
    # installed on a gege_hr-only bench → the column won't exist → pymysql crash.
    if vals.get("department"):
        try:
            if frappe.get_meta("Department").has_field("leave_approvers"):
                attrs["dept_head_user"] = (
                    frappe.db.get_value("Department", vals["department"], "leave_approvers") or None
                )
        except Exception:
            pass
    return attrs


def _config(transaction_type: str) -> dict:
    cfg = rules.TRANSACTION_CONFIG.get(transaction_type)
    if not cfg:
        frappe.throw(_("Loại yêu cầu không hỗ trợ: {0}").format(transaction_type))
    return cfg


def _lock_request_row(cfg: dict, name: str) -> None:
    """Serialize concurrent approve/reject on the same request.

    Without the lock, two approvers acting at the same moment both read the
    same ``current`` state, both compute a next state from that stale value,
    and the last writer wins — an already-Approved request can be dragged back
    to a pending state (then approved AGAIN, firing side-effects twice: double
    OUT-log, double OT write-back). SELECT ... FOR UPDATE inside the request's
    open transaction makes the second request block until the first commits;
    combined with the post-lock re-check of the state in approve/reject, the
    loser now aborts with "không ở trạng thái chờ duyệt" instead of racing.
    """
    doctype = cfg["doctype"].replace("`", "``")
    frappe.db.sql(
        f"SELECT name FROM `tab{doctype}` WHERE name = %(name)s FOR UPDATE",
        {"name": name},
    )


# Flat row shape returned to the SPA — keeps the inbox list renderable without a
# second lookup (matches the row docstring in hr-ui/src/api/index.js).
# Fields common to every request row (none of these are type-specific, so they
# never risk a "column does not exist" error on a given DocType).
# Upper bound of pending rows fetched per request type in the unified inbox.
# Unbounded fetches over the five request DocTypes could load the whole table
# into one HTTP response (the inbox endpoint previously had NO limit at all).
_PENDING_INBOX_ROW_CAP = 200

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
    "Leave Cancellation Request": [
        "workflow_state",
        "leave_application",
        "rejection_reason",
        "creation",
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
    if transaction_type == "Leave Cancellation Request":
        # No natural date column — surface creation day for the FE date display.
        # `creation` is a datetime; coerce to str before slicing.
        out.setdefault("work_date", str(out.get("creation") or "")[:10])
        out.setdefault("leave_application_ref", out.get("leave_application"))
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


def _user_can_act(doc_dict: dict, transaction_type: str, user: str, _cache: dict | None = None) -> bool:
    """Does ``user`` hold the active approval step for this request?

    ``_cache`` (optional) memoizes matrix + employee lookups per request — pass
    the same dict for every row of an inbox list to avoid the N×M query storm
    (each row used to re-load its matrix + 3 employee queries)."""
    company = doc_dict.get("company")
    if _cache is None:
        matrices = _load_matrices(transaction_type, company)
    else:
        mkey = ("mat", transaction_type, company)
        if mkey not in _cache:
            _cache[mkey] = _load_matrices(transaction_type, company)
        matrices = _cache[mkey]
    if not matrices:
        # No matrix configured → fall back to coarse role gating: HR/Line Manager
        # roles may act on any pending request of that type.
        roles = set(emp_utils.get_user_roles() or [])
        return bool(roles & (emp_utils.HR_MANAGER_ROLES | {"HR User", "Line Manager"}))

    if _cache is not None:
        ekey = ("emp", doc_dict.get("employee"))
        if ekey not in _cache:
            _cache[ekey] = _employee_attrs(doc_dict.get("employee"))
        attrs = _cache[ekey]
    else:
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


# --------------------------------------------------------------------------- #
# Overtime → Work Session recalc on approval state change (BUG-1 fix, plan T1)
# --------------------------------------------------------------------------- #
# Workflow states the calculation engine treats as "approved/usable" — must stay
# in sync with ``calc.get_approved_ot_requests`` (workflow_state in [Approved,
# Confirmed]). A request entering OR leaving this set must recompute the Work
# Session so ``approved_overtime_hours`` reflects reality immediately.
_OT_ENGINE_STATES = ("Approved", "Confirmed")


def _shift_instance_for_day(employee: str, work_date) -> str | None:
    """The ``VN Employee Shift Instance`` covering ``work_date`` for ``employee``.

    Prefer an exact ``work_date`` match; fall back to the planned-window bound.
    Returns ``None`` when the doctype is absent or no instance covers the day
    (the approved OT will then be picked up when the shift is later submitted /
    a check-in lands — it is already persisted as ``Approved`` in the DB).
    """
    if not employee or not work_date:
        return None
    try:
        if not frappe.db.table_exists("VN Employee Shift Instance"):  # type: ignore[attr-defined]
            return None
    except Exception:
        return None
    name = frappe.db.get_value(
        "VN Employee Shift Instance",
        {"employee": employee, "work_date": work_date},
        "name",
    )
    if name:
        return name
    rows = frappe.db.get_all(
        "VN Employee Shift Instance",
        filters={
            "employee": employee,
            "planned_start": ["<=", f"{work_date} 23:59:59"],
            "planned_end": [">=", f"{work_date} 00:00:00"],
        },
        fields=["name"],
        order_by="planned_start asc",
        limit=1,
    )
    return rows[0]["name"] if rows else None


def _recalc_ot_shift_instance(employee, work_date, shift_instance=None) -> None:
    """Recompute the Work Session for an OT day (enqueue on ``short``; inline
    fallback). Best-effort: a failure is logged but never raised, so it can never
    abort an approve/reject. Mirrors the enqueue the check-in hook uses
    (``attendance.on_employee_checkin_create``)."""
    from gege_hr.gege_hr.utils import calc

    si = shift_instance or _shift_instance_for_day(employee, work_date)
    if not si:
        return  # No shift yet → nothing to recompute; OT picked up later.
    try:
        frappe.enqueue(
            "gege_hr.gege_hr.utils.calc.persist_work_session",
            queue="short",
            shift_instance_name=si,
            calculate_mode="recalculate",
        )
        return
    except Exception:
        pass  # Enqueue unavailable (e.g. in tests) → compute inline below.
    try:
        calc.persist_work_session(si, calculate_mode="recalculate")
    except Exception:
        frappe.log_error(
            title="OT recalc inline failed",
            message=f"shift_instance={si} employee={employee} work_date={work_date}",
        )


def _after_ot_state_change(doc, *, from_state, to_state) -> None:
    """Recompute the Work Session when a ``VN Overtime Request`` enters or leaves
    the engine-active state set (Approved/Confirmed).

    Called from ``approve_request`` / ``reject_request`` (and the OT cancel path)
    so the Work Session's ``approved_overtime_hours`` stays in sync with the
    request lifecycle the moment a manager decides — not only on the next
    check-in. No-op for non-OT doctypes and for intermediate pending→pending
    transitions. Always best-effort (plan T1 / BUG-1)."""
    try:
        if getattr(doc, "doctype", None) != "VN Overtime Request":
            return
        # Fire only when membership in the engine-active set (Approved/Confirmed)
        # actually flips — an enter (Pending → Approved/Confirmed) or a leave
        # (Approved/Confirmed → Rejected). A stay inside the set (e.g. Approved →
        # Confirmed) or a pending→pending move changes nothing w.r.t. the engine,
        # so no recalc is needed (idempotency + no redundant jobs).
        in_before = from_state in _OT_ENGINE_STATES
        in_after = to_state in _OT_ENGINE_STATES
        if in_before == in_after:
            return
        _recalc_ot_shift_instance(
            doc.get("employee"),
            doc.get("work_date"),
            doc.get("shift_instance"),
        )
    except Exception:
        try:
            frappe.log_error(
                title="OT recalc on state change failed",
                message=f"{doc.get('doctype')} {doc.get('name')} {from_state}->{to_state}",
            )
        except Exception:
            pass


def _after_correction_state_change(doc, *, from_state, to_state) -> None:
    """Sync a ``VN Checkout Miss`` ticket when its Correction Request is approved.

    BUG-5 fix (plans/checkout-miss-fix-plan.md §BUG-5): a correction request
    opened from a checkout-miss explanation carries ``vn_checkout_miss``. When
    it reaches ``Approved`` with a real ``requested_checkout_time``:

      1. synthesise the real ``OUT`` Employee Checkin,
      2. delete the auto-generated (fake) OUT at planned_end so payroll's
         IN/OUT pairing doesn't double-count,
      3. repoint the Work Session to the real OUT,
      4. auto-waive the ticket (the real checkout is now evidenced).

    No-op for non-correction docs / non-Approved transitions / CRs without a
    ticket link. Always best-effort — never aborts the approval itself.
    """
    try:
        if getattr(doc, "doctype", None) != "VN Attendance Correction Request":
            return
        if to_state != "Approved":
            return
        miss_name = doc.get("vn_checkout_miss")
        if not miss_name:
            return

        miss = frappe.db.get_value(
            "VN Checkout Miss",
            miss_name,
            ["name", "employee", "employee_name", "auto_checkout", "shift_instance", "status"],
            as_dict=True,
        )
        if not miss:
            return

        real_out = doc.get("requested_checkout_time")
        new_log_name = None
        if real_out:
            from frappe.utils import get_datetime

            out_log = frappe.get_doc(
                {
                    "doctype": "Employee Checkin",
                    "employee": miss.employee,
                    "employee_name": miss.employee_name,
                    "time": get_datetime(real_out),
                    "log_type": "OUT",
                    "vn_source_type": "Correction",
                    "vn_auto_generated": 0,
                    "vn_checkout_miss": miss_name,
                }
            )
            out_log.insert(ignore_permissions=True)
            new_log_name = out_log.name
            frappe.db.set_value(
                "VN Attendance Correction Request",
                doc.get("name"),
                "generated_checkin",
                new_log_name,
            )
            # Replace the fake OUT at planned_end with the real one.
            if miss.auto_checkout:
                frappe.delete_doc(
                    "Employee Checkin", miss.auto_checkout, ignore_permissions=True
                )
            if miss.shift_instance:
                frappe.db.set_value(
                    "VN Attendance Work Session",
                    {"shift_instance": miss.shift_instance},
                    {
                        "actual_checkout": get_datetime(real_out),
                        "last_checkout_log": new_log_name,
                    },
                    update_modified=False,
                )

        if (miss.status or "").strip() in ("Pending", "Explained", "Penalised"):
            ticket = frappe.get_doc("VN Checkout Miss", miss_name)
            ticket.status = "Waived"
            ticket.penalty_waived = 1
            if new_log_name:
                ticket.auto_checkout = new_log_name
            ticket.resolved_by = frappe.session.user
            ticket.resolved_on = frappe.utils.now()
            ticket.note = (
                (ticket.note or "")
                + f"\nTự động miễn phạt: CR {doc.get('name')} được duyệt"
                + (f" (OUT thật {real_out})." if real_out else ".")
            ).strip()
            ticket.save(ignore_permissions=True)
    except Exception:
        try:
            frappe.log_error(
                title="checkout_miss correction sync failed",
                message=f"{doc.get('name')} {from_state}->{to_state}",
            )
        except Exception:
            pass


def _delegate_leave(name: str, request_type: str, comment: str | None, *, approved: bool) -> dict:
    """Leave Application approve/reject — delegated to the HRMS-aware leave handler.

    Phase 0 of the inbox-centric migration
    (plans/leave_approval_inbox_centric_plan.md §1.2): the unified inbox must
    persist a leave decision through the SAME path the self-service leave
    endpoints use — ``leave._approve_one`` / ``leave._reject_one`` — so HRMS
    ``validate`` runs and the **Leave Ledger Entry is created** (leave balance
    actually deducted). The previous raw ``frappe.db.set_value`` bypassed
    ``on_submit`` and left the leave ledger stale.

    The VN Approval Log row is still appended (matrix-history parity with the
    OT/Correction/Advance path). Validation failures (insufficient balance,
    blackout) are intentionally allowed to propagate so the inbox can surface
    the real reason instead of silently no-op'ing (plan R2).
    """
    from gege_hr.gege_hr.api import leave as leave_api

    action = "Approve" if approved else "Reject"
    target_state = "Approved" if approved else "Rejected"

    # Capture the current state BEFORE the decision so the VN Approval Log records
    # an accurate from→to transition. A direct db.get_value avoids a second
    # Document.load() (and its read-perm check); authorization is already enforced
    # by _user_can_act() in the calling endpoint.
    try:
        current = frappe.db.get_value("Leave Application", name, "status") or ""
    except Exception:
        current = ""
    if current == target_state:
        return {
            "name": name,
            "request_type": request_type,
            "status": target_state,
            "message": _("Đơn nghỉ phép {0} đã ở trạng thái {1}.").format(name, target_state),
        }

    # Persist via the single HRMS-aware source of truth: _approve_one / _reject_one
    # set status + leave_approver, call _save_or_submit (which submits a Draft →
    # creates the Leave Ledger Entry so the balance is deducted), then fire
    # _after_leave_decision audit + notify. State/idempotency checks live there.
    # Let exceptions propagate so the inbox sees real validation errors.
    if approved:
        result = leave_api._approve_one(name)
    else:
        result = leave_api._reject_one(name, comment)

    # Matrix history (best-effort — never abort a successful decision over a log
    # write; mirrors the _write_log guard in approve_request).
    try:
        _write_log(
            transaction_type=request_type,
            name=name,
            action=action,
            from_state=current,
            to_state=target_state,
            comment=comment,
            actor=_user(),
        )
    except Exception:
        frappe.log_error(
            title="VN Approval Log write failed",
            message=f"Leave Application {name} {action}",
        )

    # Normalise the return shape so the inbox keeps seeing request_type + message.
    return {
        "name": name,
        "request_type": request_type,
        "status": result.get("status", target_state),
        "message": result.get("message"),
    }


def _delegate_leave_cancellation(
    name: str, request_type: str, comment: str | None, *, approved: bool
) -> dict:
    """Leave *cancellation* approve/reject — delegated to the HRMS-aware handler.

    Phase 2A of the inbox-centric migration: a ``VN Leave Cancellation Request``
    is approved/rejected through ``leave._approve_cancellation_one`` /
    ``leave._reject_cancellation_one`` (the path that cancels the linked Leave
    Application and restores the leave balance). Mirrors ``_delegate_leave``: a
    VN Approval Log row is still appended; validation failures propagate so the
    inbox can surface the real reason (plan R2).
    """
    from gege_hr.gege_hr.api import leave as leave_api

    action = "Approve" if approved else "Reject"
    target_state = "Approved" if approved else "Rejected"

    try:
        current = (
            frappe.db.get_value("VN Leave Cancellation Request", name, "workflow_state") or ""
        )
    except Exception:
        current = ""
    if current == target_state:
        return {
            "name": name,
            "request_type": request_type,
            "status": target_state,
            "message": _("Yêu cầu hủy {0} đã ở trạng thái {1}.").format(name, target_state),
        }

    if approved:
        result = leave_api._approve_cancellation_one(name)
    else:
        result = leave_api._reject_cancellation_one(name, comment)

    try:
        _write_log(
            transaction_type=request_type,
            name=name,
            action=action,
            from_state=current,
            to_state=target_state,
            comment=comment,
            actor=_user(),
        )
    except Exception:
        frappe.log_error(
            title="VN Approval Log write failed",
            message=f"Leave Cancellation {name} {action}",
        )

    return {
        "name": name,
        "request_type": request_type,
        "status": result.get("status", target_state),
        "message": result.get("message"),
    }


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
def _matches_search(row: dict, search: str | None) -> bool:
    """Server-side free-text match across a request row's text fields.

    Applied after the rows are fetched — the searchable columns differ per
    transaction DocType, so we match on the loaded row rather than risk an
    ``or_filters`` "column does not exist" error. DNA §6.6 D — HR-BL-07.
    """
    q = (search or "").strip().lower()
    if not q:
        return True
    for key in ("employee", "employee_name", "reason", "description", "name", "department"):
        val = row.get(key)
        if val is not None and q in str(val).lower():
            return True
    return False


@frappe.whitelist()
def get_pending_approvals(
    approver: str | None = None,
    request_type: str | None = None,
    date: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    search: str | None = None,
) -> dict:
    """Plan §10.6 — pending requests the approver can act on, grouped by type.

    ``from_date``/``to_date`` narrow each request type on its own ``date_field``
    (``>= from`` / ``<= to``) — applied server-side as two LIST filters so both
    bounds can coexist on the same field (a dict cannot hold two keys —
    DNA §6.6 B). ``date`` is kept for backward-compat (single ``>=``).
    ``search`` OR-matches a free-text query across each request's text fields
    (employee / employee_name / reason / description / name / department),
    applied server-side (DNA §6.6 D, HR-BL-07). Returns
    ``{ groups: [{ request_type, label, count, requests: [] }] }``. An empty
    ``request_type`` returns every supported type.
    """
    user = _resolve_approver(approver)
    types = [request_type] if request_type else rules.supported_types()

    groups = []
    for ttype in types:
        cfg = rules.TRANSACTION_CONFIG.get(ttype)
        if not cfg:
            continue
        # LIST filters (not dict) so two bounds on the same date_field can
        # coexist — DNA §6.6 B (HR-BL-approvals-date).
        filters = [[cfg["status_field"], "in", cfg["pending_states"]]]
        date_field = cfg["date_field"]
        if date:
            filters.append([date_field, ">=", date])
        if from_date:
            filters.append([date_field, ">=", from_date])
        if to_date:
            filters.append([date_field, "<=", to_date])
        try:
            rows = frappe.db.get_all(
                cfg["doctype"],
                filters=filters,
                fields=_row_fields(ttype),
                order_by="creation desc",
                limit_page_length=_PENDING_INBOX_ROW_CAP,
            )
        except Exception:
            rows = []

        # Keep only the rows the approver may actually act on (and match search).
        # ``_act_cache`` memoizes matrix/employee lookups across rows (without it
        # each row cost 3-4 queries — an inbox storm on busy days).
        _act_cache: dict = {}
        actionable = [
            _normalise_row(r, ttype, user)
            for r in rows
            if _user_can_act(r, ttype, user, _cache=_act_cache) and _matches_search(r, search)
        ]
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

    # Proper Frappe flow: load via get_doc — the approver's role MUST grant read
    # on the request DocType. We no longer use the raw db.get_value bypass (that
    # skipped the read-permission check). If a 403 occurs, grant the role
    # read/write on the DocType via Role Permissions Manager — do NOT bypass in
    # code, or validate/on_update and the request's own audit/hooks won't run.
    _lock_request_row(cfg, name)  # serialize concurrent approve/reject (H1)
    doc = frappe.get_doc(cfg["doctype"], name)
    _data = doc.as_dict()
    current = doc.get(cfg["status_field"])
    if current not in cfg["pending_states"]:
        frappe.throw(
            _("Yêu cầu không ở trạng thái chờ duyệt ({0}).").format(current),
            frappe.PermissionError,
        )
    if not _user_can_act(_data, request_type, user):
        frappe.throw(
            _("Bạn không phải người duyệt ở bước này."),
            frappe.PermissionError,
        )

    # Leave Application status is managed by core Frappe HR via docstatus, not a
    # free `status` field — so delegate to the HRMS-aware leave endpoints that
    # submit/cancel correctly (the matrix step states don't apply here).
    if request_type == "Leave Application":
        return _delegate_leave(name, request_type, comment, approved=True)
    if request_type == "Leave Cancellation Request":
        return _delegate_leave_cancellation(name, request_type, comment, approved=True)

    # Resolve the next state from the matrix (→ Approved when last step done).
    matrices = _load_matrices(request_type, _data.get("company"))
    attrs = _employee_attrs(_data.get("employee"))
    matrix = rules.pick_matrix(matrices, attrs) if matrices else None
    next_state = (
        rules.next_state_after(current, matrix.get("steps") or []) if matrix else cfg["approve_state"]
    )

    # Proper Frappe flow: set the workflow_state then save() — this runs the
    # DocType's validate + on_update (so the request's own hooks/audit and any
    # downstream side-effects fire) and respects the approver's write permission.
    # We no longer raw-write the status column (that skipped validate/on_update).
    # If a 403 occurs, grant the approver role write on the DocType.
    doc.set(cfg["status_field"], next_state)
    doc.save()
    _data[cfg["status_field"]] = next_state

    # OT only: recompute the Work Session the moment the request enters the
    # engine-active set (Approved/Confirmed) so approved_overtime_hours stays in
    # sync immediately (BUG-1 fix, plan T1). Best-effort — never aborts.
    _after_ot_state_change(doc, from_state=current, to_state=next_state)

    # Correction only: when a CR opened from a checkout-miss explanation is
    # finally Approved, replace the fake OUT with the real one and auto-waive
    # the ticket (BUG-5 fix). Best-effort — never aborts.
    _after_correction_state_change(doc, from_state=current, to_state=next_state)

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
            employee=_data.get("employee"),
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
            doc=_data,
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

    # Proper Frappe flow: load via get_doc (read permission checked). See
    # approve_request — no raw db.get_value bypass.
    _lock_request_row(cfg, name)  # serialize concurrent approve/reject (H1)
    doc = frappe.get_doc(cfg["doctype"], name)
    _data = doc.as_dict()
    current = doc.get(cfg["status_field"])
    if current not in cfg["pending_states"]:
        frappe.throw(
            _("Yêu cầu không ở trạng thái chờ duyệt ({0}).").format(current),
            frappe.PermissionError,
        )
    if not _user_can_act(_data, request_type, user):
        frappe.throw(
            _("Bạn không phải người duyệt ở bước này."),
            frappe.PermissionError,
        )

    # Leave Application: delegate to the HRMS-aware reject (see approve_request).
    if request_type == "Leave Application":
        return _delegate_leave(name, request_type, comment, approved=False)
    if request_type == "Leave Cancellation Request":
        return _delegate_leave_cancellation(name, request_type, comment, approved=False)

    doc.set(cfg["status_field"], cfg["reject_state"])
    # Proper Frappe flow: save() runs validate + on_update (the request's own
    # hooks/audit fire) and respects the approver's write permission. No
    # ignore_permissions bypass — grant the role write on the DocType if 403.
    doc.save()

    # OT only: if the request was engine-active (Approved/Confirmed) before the
    # reject, recompute the Work Session so the OT is removed (BUG-1 fix, T1).
    _after_ot_state_change(doc, from_state=current, to_state=cfg["reject_state"])

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
