"""
Unified Approval Inbox API — plan v5 §10.6 / doctype-design §15.

These endpoints front the SPA approval inbox (``hr-ui/src/composables/useApprovals.js``)
and map 1:1 to ``gege_hr.gege_hr.api.approval.<fn>`` calls in
``hr-ui/src/api/index.js``:

  * ``get_pending_approvals`` — pending requests the approver can act on, grouped
    by transaction type. Returns ``{ groups: [...] }`` (or a flat list).
    ``bucket="processed"`` (plans/approvals-deskfree-complete §3.1) flips it
    into the approver's decision history via ``VN Approval Log``.
  * ``approve_request``     — advance one request to its next approval state.
  * ``reject_request``      — send a request to Rejected.
  * ``bulk_approve``        — approve many requests of one type at once.
  * ``bulk_reject``         — reject many requests of one type at once.
  * ``filter_options``      — distinct employees/departments/branches across the
    caller's pending queue (desk-free §3.2).
  * ``get_request_detail``  — 360° decision context for the SPA drawer
    (desk-free §3.3): doc + requester + matrix step + history + comments +
    attachments + a server-driven ``can`` action matrix.
  * ``export_pending_csv``  — CSV of the filtered pending queue (desk-free §3.8).

Routing is driven by :doc:`VN Approval Matrix` (with :doc:`VN Approval Step`
children); every transition is recorded in :doc:`VN Approval Log`. The pure
state-machine lives in :mod:`gege_hr.gege_hr.utils.approval` so it can be tested
without a bench.
"""

from __future__ import annotations

import frappe
from frappe import _

from gege_hr.gege_hr.api import audit as audit_api
from gege_hr.gege_hr.utils import approval as rules, employee as emp_utils, notify


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
    # plans/plan-expense-desk-free.md §2.11 — hrms Expense Claim (Draft chờ duyệt).
    "Expense Claim": [
        "approval_status",
        "company",
        "posting_date",
        "total_claimed_amount",
        "remark",
    ],
    # plans/leave-extra-deskfree-complete §2.7 P1c — hrms Leave Encashment /
    # Compensatory Leave Request (portal Draft vn_status chờ HR duyệt).
    "Leave Encashment": [
        "vn_status",
        "company",
        "leave_type",
        "encashment_days",
        "encashment_amount",
    ],
    "Compensatory Leave Request": [
        "vn_status",
        "company",
        "leave_type",
        "work_from_date",
        "work_end_date",
        "reason",
    ],
    # services-deskfree P1c — the two remaining routed types. Only columns the
    # TRANSACTION_CONFIG already filters on (they must exist) + base company.
    "Employee Grievance": [
        "status",
        "company",
        "date",
    ],
    "Travel Request": [
        "vn_status",
        "company",
        "vn_from_date",
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
    if transaction_type == "Expense Claim":
        out.setdefault("work_date", out.get("posting_date"))
        # Same tile treatment for claims (FE renders `expense_amount`).
        out["expense_amount"] = out.get("total_claimed_amount")
    if transaction_type == "Leave Cancellation Request":
        # No natural date column — surface creation day for the FE date display.
        # `creation` is a datetime; coerce to str before slicing.
        out.setdefault("work_date", str(out.get("creation") or "")[:10])
        out.setdefault("leave_application_ref", out.get("leave_application"))
    if transaction_type == "Leave Encashment":
        out.setdefault("work_date", str(out.get("creation") or "")[:10])
    if transaction_type == "Compensatory Leave Request":
        out.setdefault("work_date", out.get("work_from_date"))
    # Unify the human reason/description onto a single ``reason`` field so the
    # approval-inbox card always has something to show the manager. Leave
    # Application stores its reason in ``description`` (Frappe has no ``reason``
    # column); OT / Correction / Advance use ``reason``. Prefer an explicit
    # ``reason`` when present, otherwise fall back to ``description``.
    unified_reason = (out.get("reason") or out.get("description") or out.get("remark") or "").strip()
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
    if transaction_type == "Expense Claim":
        # Single-step plan §2.11: matrix steps are P2 — coarse role gating
        # (HR/manager roles + Expense Claim Approver) decides, exactly like the
        # no-matrix fallback below but without the matrix lookup (a configured
        # VN Approval Matrix for Expense Claim must not HIDE pending claims).
        roles = set(emp_utils.get_user_roles() or [])
        return bool(
            roles
            & (emp_utils.HR_MANAGER_ROLES | {"HR User", "Expense Claim Approver", "Line Manager"})
        )
    if transaction_type in ("Leave Encashment", "Compensatory Leave Request"):
        # Single-step (leave-extra P1c): parity the leave_extra API gate —
        # HR Manager / HR User / System Manager may act; matrix steps are P2.
        roles = set(emp_utils.get_user_roles() or [])
        return bool(roles & emp_utils.HR_MANAGER_ROLES)
    if transaction_type in ("Employee Grievance", "Travel Request"):
        # Single-step (services-deskfree P1c): parity the employee_services
        # manager gate (_is_manager) — matrix steps are P2.
        roles = set(emp_utils.get_user_roles() or [])
        return bool(roles & emp_utils.HR_MANAGER_ROLES)
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
    if rules.approver_matches(
        step,
        user=user,
        roles=roles,
        line_manager_user=attrs.get("line_manager_user"),
        dept_head_user=attrs.get("dept_head_user"),
    ):
        return True
    # Delegation (desk-free B2, §3.5): a user who holds the step may hand it to
    # another user for a window — expand the concrete holder set with their
    # active delegates. Role-based holder types stay outside the contract.
    for holder in rules.step_holder_users(
        step,
        line_manager_user=attrs.get("line_manager_user"),
        dept_head_user=attrs.get("dept_head_user"),
    ):
        if user and user in _delegated_users(
            holder, transaction_type, doc_dict.get("name"), _cache=_cache
        ):
            return True
    return False


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
    transitions. Always best-effort (plan T1 / BUG-1).

    Also pings the ``overtime_updated`` realtime channel on EVERY OT state
    change (plan overtime-deskfree OT6) so open ``/hr/overtime`` tabs refresh
    without F5 — this one function covers approve / reject / cancel."""
    try:
        if getattr(doc, "doctype", None) != "VN Overtime Request":
            return
        # Realtime ping first — it must fire for pending→pending moves too, so
        # it sits BEFORE the engine-membership early-return below. Best-effort:
        # a missing overtime module (stub environments) never breaks the recalc.
        try:
            from gege_hr.gege_hr.api.overtime import _publish_overtime

            _publish_overtime(doc)
        except Exception:
            pass
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
        # Realtime ping for open /hr/correction tabs (plans/correction-deskfree
        # §3.4 CD6) — placed before the state guards so it fires on EVERY CR
        # state change (approve_request and reject_request both call this hook).
        # Own try/except: a publish/import failure must never abort the
        # ticket-waive business logic below (best-effort semantics).
        try:
            from gege_hr.gege_hr.api.correction import _publish_correction

            _publish_correction(doc)
        except Exception:
            pass
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
                    # E2E-D3b: the Select field only accepts
                    # ""/Mobile/App/Device/Manual/Import/Auto — "Correction"
                    # made the insert throw and silently killed the whole
                    # BUG-5 sync (fake OUT stayed, ticket never waived).
                    "vn_source_type": "Manual",
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
            # Replace the fake OUT at planned_end with the real one. The ticket
            # LINKS the fake log (auto_checkout) — repoint it to the real OUT
            # FIRST, or delete_doc raises LinkExistsError and the whole sync
            # dies (E2E-D3b: fake OUT stayed, ticket never waived).
            if miss.auto_checkout:
                frappe.db.set_value(
                    "VN Checkout Miss",
                    miss_name,
                    "auto_checkout",
                    new_log_name,
                    update_modified=False,
                )
                frappe.delete_doc("Employee Checkin", miss.auto_checkout, ignore_permissions=True)
            if miss.shift_instance:
                frappe.db.set_value(
                    "VN Attendance Work Session",
                    {"shift_instance": miss.shift_instance},
                    {
                        "actual_checkout": get_datetime(real_out),
                        "last_checkout_log": new_log_name,
                        # Clear the synthetic-OUT flags: the UI shows the
                        # APPROVED real checkout time (not "Quên chấm ra"/--:--)
                        # and payroll pairs the real OUT.
                        "vn_auto_checkout": 0,
                        "missing_checkout": 0,
                    },
                    update_modified=False,
                )
                # The real-OUT insert above ENQUEUED an async WS recalc; that
                # job may snapshot logs mid-transition (fake OUT still present)
                # and overwrite this write with the stale pairing. Recompute
                # synchronously from the FINAL log state so the request ends
                # consistent — any later queued run converges to the same.
                try:
                    from gege_hr.gege_hr.utils import calc as calc_util

                    calc_util.persist_work_session(miss.shift_instance, calculate_mode="batch")
                except Exception:
                    pass

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
                message=f"{doc.get('name')} {from_state}->{to_state}\n" + frappe.get_traceback(),
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
        current = frappe.db.get_value("VN Leave Cancellation Request", name, "workflow_state") or ""
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


def _delegate_leave_extra(
    name: str, request_type: str, comment: str | None, *, approved: bool, doctype: str
) -> dict:
    """Leave Encashment / Comp-off approve/reject — delegated to the
    HRMS-aware leave_extra handler (plans/leave-extra-deskfree-complete §2.7).

    Same pattern as :func:`_delegate_expense`: the inbox persists the decision
    through the SAME path the self-service page uses —
    ``leave_extra.approve_*`` runs the Administrator-set_user submit (minting
    the Additional Salary / Leave Allocation), ``reject_*`` cancels a submitted
    row or rejects the draft with the comment appended to ``vn_note``.
    Validation failures propagate so the inbox surfaces the real reason.
    """
    from gege_hr.gege_hr.api import leave_extra as leave_extra_api

    action = "Approve" if approved else "Reject"
    target_state = "Approved" if approved else "Rejected"

    try:
        current = frappe.db.get_value(doctype, name, "vn_status") or ""
    except Exception:
        current = ""
    if current == target_state:
        return {
            "name": name,
            "request_type": request_type,
            "status": target_state,
            "message": _("Yêu cầu {0} đã ở trạng thái {1}.").format(name, target_state),
        }

    if doctype == "Leave Encashment":
        result = (
            leave_extra_api.approve_leave_encashment(name)
            if approved
            else leave_extra_api.reject_leave_encashment(name, reason=comment)
        )
    else:
        result = (
            leave_extra_api.approve_comp_off(name)
            if approved
            else leave_extra_api.reject_comp_off(name, reason=comment)
        )

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
            message=f"{doctype} {name} {action}",
        )

    return {
        "name": name,
        "request_type": request_type,
        "status": result.get("status", target_state),
        "message": result.get("message", ""),
    }


def _delegate_services(
    name: str, request_type: str, comment: str | None, *, approved: bool, doctype: str
) -> dict:
    """Employee Grievance / Travel Request approve/reject — delegated to the
    employee_services handler (plans/services-deskfree-complete P1c).

    Same pattern as :func:`_delegate_leave_extra`: the inbox persists the
    decision through the SAME path the self-service page uses — grievance
    approve resolves (``cause_of_grievance`` filled from the comment, F2),
    travel approve runs the Administrator-set_user ``doc.submit()`` (F3) and
    reject appends the comment to ``vn_note``. Validation failures propagate.
    """
    from gege_hr.gege_hr.api import employee_services as services_api

    action = "Approve" if approved else "Reject"
    if doctype == "Employee Grievance":
        target_state = "Resolved" if approved else "Invalid"
    else:
        target_state = "Approved" if approved else "Rejected"

    try:
        field = "status" if doctype == "Employee Grievance" else "vn_status"
        current = frappe.db.get_value(doctype, name, field) or ""
    except Exception:
        current = ""
    if current == target_state:
        return {
            "name": name,
            "request_type": request_type,
            "status": target_state,
            "message": _("Yêu cầu {0} đã ở trạng thái {1}.").format(name, target_state),
        }

    if doctype == "Employee Grievance":
        result = (
            services_api.resolve_grievance(
                name, resolution=(comment or "").strip() or "Xử lý qua hộp duyệt", cause=comment
            )
            if approved
            else services_api.invalidate_grievance(name, reason=comment)
        )
    else:
        result = (
            services_api.approve_travel_request(name)
            if approved
            else services_api.reject_travel_request(name, reason=comment)
        )

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
            message=f"{doctype} {name} {action}",
        )

    return {
        "name": name,
        "request_type": request_type,
        "status": result.get("status", target_state),
        "message": result.get("message", ""),
    }


# --------------------------------------------------------------------------- #
# Endpoints
def _delegate_expense(name: str, request_type: str, comment: str | None, *, approved: bool) -> dict:
    """Expense Claim approve/reject — delegated to the HRMS-aware expense handler
    (plans/plan-expense-desk-free.md §2.11).

    Same pattern as :func:`_delegate_leave`: the unified inbox must persist the
    decision through the SAME path the self-service expense endpoints use —
    ``expense.approve_expense_claim`` stamps per-line sanctions (default = the
    full claimed amount), sets ``approval_status = Approved`` and SUBMITS the
    claim (GL), while ``reject`` requires a reason and logs a Comment. Validation
    failures are allowed to propagate so the inbox surfaces the real reason.
    """
    from gege_hr.gege_hr.api import expense as expense_api

    action = "Approve" if approved else "Reject"
    target_state = "Approved" if approved else "Rejected"

    try:
        current = frappe.db.get_value("Expense Claim", name, "approval_status") or ""
    except Exception:
        current = ""
    if current == target_state:
        return {
            "name": name,
            "request_type": request_type,
            "status": target_state,
            "message": _("Phiếu chi phí {0} đã ở trạng thái {1}.").format(name, target_state),
        }

    if approved:
        result = expense_api.approve_expense_claim(name, comment=comment)
    else:
        result = expense_api.reject_expense_claim(name, reason=comment)

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
            message=f"Expense Claim {name} {action}",
        )

    return {
        "name": name,
        "request_type": request_type,
        "status": result.get("approval_status", target_state),
        "message": result.get("message") or result.get("submit_note"),
    }


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
    bucket: str | None = None,
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

    plans/approvals-deskfree-complete §3.1 — ``bucket="processed"`` returns the
    approver's DECISION HISTORY instead: the latest Approve/Reject
    ``VN Approval Log`` rows of ``approver`` (same date window applied on
    ``action_at``), resolved back to the live documents (deleted docs are
    skipped silently). Rows carry ``last_action`` / ``last_action_at``. The
    default (empty / ``"pending"``) keeps the legacy contract unchanged (AD25).
    """
    user = _resolve_approver(approver)
    types = [request_type] if request_type else rules.supported_types()

    if (bucket or "pending") == "processed":
        return {"groups": _processed_groups(user, request_type, from_date, to_date, search)}

    groups = []
    # ``_act_cache`` memoizes matrix/employee lookups across rows (without it
    # each row cost 3-4 queries — an inbox storm on busy days). Shared across
    # types: the cache keys already carry transaction_type.
    _act_cache: dict = {}
    for ttype in types:
        rows = _actionable_pending_rows(
            ttype,
            user,
            date=date,
            from_date=from_date,
            to_date=to_date,
            search=search,
            _cache=_act_cache,
        )
        if not rows:
            continue
        groups.append(
            {
                "request_type": ttype,
                "label": rules.TYPE_LABELS.get(ttype, ttype),
                "count": len(rows),
                "requests": [_normalise_row(r, ttype, user) for r in rows],
            }
        )

    return {"groups": groups}


@frappe.whitelist()
def approve_request(
    name: str | None = None,
    request_type: str | None = None,
    comment: str | None = None,
    approved_amount=None,
) -> dict:
    """Advance one request to its next approval state (→ Approved on last step).

    ``approved_amount`` (plans/advance-deskfree-complete.md §2.4) only applies
    to a ``Salary Advance Request`` reaching its FINAL approval step: the
    approver may sanction less than requested (standard Frappe behaviour —
    ``approved_amount`` drives the Additional Salary deduction at payout).
    It is validated against the requested amount and the eligible cap; for
    every other request type the parameter is ignored.

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
    if request_type == "Expense Claim":
        return _delegate_expense(name, request_type, comment, approved=True)
    if request_type == "Leave Encashment":
        return _delegate_leave_extra(
            name, request_type, comment, approved=True, doctype="Leave Encashment"
        )
    if request_type == "Compensatory Leave Request":
        return _delegate_leave_extra(
            name, request_type, comment, approved=True, doctype="Compensatory Leave Request"
        )
    # services-deskfree P1c — delegate về endpoint self-service (giữ set_user path).
    if request_type == "Employee Grievance":
        return _delegate_services(
            name, request_type, comment, approved=True, doctype="Employee Grievance"
        )
    if request_type == "Travel Request":
        return _delegate_services(name, request_type, comment, approved=True, doctype="Travel Request")

    # Resolve the next state from the matrix (→ Approved when last step done).
    matrices = _load_matrices(request_type, _data.get("company"))
    attrs = _employee_attrs(_data.get("employee"))
    matrix = rules.pick_matrix(matrices, attrs) if matrices else None
    next_state = (
        rules.next_state_after(current, matrix.get("steps") or []) if matrix else cfg["approve_state"]
    )

    # Salary Advance only (plan §2.4): stamp the sanctioned amount on the FINAL
    # approval step. The approver may approve LESS than requested; never more,
    # and never above the policy cap. Every other request type ignores the param.
    if (
        request_type == "Salary Advance Request"
        and approved_amount not in (None, "")
        and next_state == cfg["approve_state"]
    ):
        try:
            amt = float(approved_amount)
        except (TypeError, ValueError):
            frappe.throw(_("Số tiền duyệt không hợp lệ."))
        requested = float(doc.get("requested_amount") or 0)
        eligible = float(doc.get("eligible_amount") or 0)
        if amt <= 0 or requested <= 0 or amt > requested + 0.01:
            frappe.throw(_("Số tiền duyệt phải lớn hơn 0 và không vượt số tiền xin."))
        if eligible > 0 and amt > eligible + 0.01:
            frappe.throw(_("Số tiền duyệt vượt hạn mức được ứng ({0}).").format(eligible))
        doc.approved_amount = amt

    # Proper Frappe flow: set the workflow_state then save() — this runs the
    # DocType's validate + on_update (so the request's own hooks/audit and any
    # downstream side-effects fire) and respects the approver's write permission.
    # We no longer raw-write the status column (that skipped validate/on_update).
    # If a 403 occurs, grant the approver role write on the DocType.
    doc.set(cfg["status_field"], next_state)
    doc.save()
    _data[cfg["status_field"]] = next_state

    # Advance only: realtime ping for open /hr/advance tabs (plan §2.10).
    if request_type == "Salary Advance Request":
        try:
            from gege_hr.gege_hr.api.advance import _publish_advance

            _publish_advance(doc)
        except Exception:
            pass

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
    if request_type == "Expense Claim":
        return _delegate_expense(name, request_type, comment, approved=False)
    if request_type == "Leave Encashment":
        return _delegate_leave_extra(
            name, request_type, comment, approved=False, doctype="Leave Encashment"
        )
    if request_type == "Compensatory Leave Request":
        return _delegate_leave_extra(
            name, request_type, comment, approved=False, doctype="Compensatory Leave Request"
        )
    # services-deskfree P1c — delegate về endpoint self-service (giữ set_user path).
    if request_type == "Employee Grievance":
        return _delegate_services(
            name, request_type, comment, approved=False, doctype="Employee Grievance"
        )
    if request_type == "Travel Request":
        return _delegate_services(name, request_type, comment, approved=False, doctype="Travel Request")

    doc.set(cfg["status_field"], cfg["reject_state"])
    # Proper Frappe flow: save() runs validate + on_update (the request's own
    # hooks/audit fire) and respects the approver's write permission. No
    # ignore_permissions bypass — grant the role write on the DocType if 403.
    doc.save()

    # Advance only: realtime ping for open /hr/advance tabs (plan §2.10).
    if request_type == "Salary Advance Request":
        try:
            from gege_hr.gege_hr.api.advance import _publish_advance

            _publish_advance(doc)
        except Exception:
            pass

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
def return_request(
    name: str | None = None,
    request_type: str | None = None,
    comment: str | None = None,
) -> dict:
    """Desk-free Phase B1 (plans/approvals-deskfree-complete §3.4) — "Trả lại
    để sửa": move a pending request of a matrix-driven VN doctype back to
    ``Draft`` so the employee can fix + resend it (instead of losing it to a
    Reject).

    Only the 4 VN workflow doctypes (OT / Correction / Advance / Leave-Cancel)
    support Return — the HRMS-managed types manage their own state machines and
    must be rejected instead (AD11). The pending state machine is walked via
    ``doc.save()`` so the seeded ``Return`` workflow transitions validate and
    the doc_events/realtime hooks fire (approve/reject parity).
    Returns ``{ name, request_type, status, message }``.
    """
    if not name or not request_type:
        frappe.throw(_("Thiếu mã yêu cầu / loại yêu cầu."))
    cfg = _config(request_type)
    if request_type not in _RETURNABLE_TYPES:
        frappe.throw(_("Loại yêu cầu không hỗ trợ trả lại."))
    user = _user()

    _lock_request_row(cfg, name)  # serialize concurrent decide (H1 parity)
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

    doc.set(cfg["status_field"], "Draft")
    doc.save()  # validate_workflow recognises the seeded Return transitions

    # Realtime pings (best-effort) — pending→Draft left the approver's queue
    # and may flip the engine-visible OT/checkout-miss wiring on resubmit.
    if request_type == "Salary Advance Request":
        try:
            from gege_hr.gege_hr.api.advance import _publish_advance

            _publish_advance(doc)
        except Exception:
            pass
    _after_ot_state_change(doc, from_state=current, to_state="Draft")
    _after_correction_state_change(doc, from_state=current, to_state="Draft")

    _write_log(
        transaction_type=request_type,
        name=name,
        action="Return",
        from_state=current,
        to_state="Draft",
        comment=comment,
        actor=user,
    )

    # A standard Comment row so the employee sees WHY it bounced (best-effort).
    try:
        frappe.get_doc(
            {
                "doctype": "Comment",
                "comment_type": "Comment",
                "reference_doctype": cfg["doctype"],
                "reference_name": name,
                "content": comment or "Yêu cầu bị trả lại để chỉnh sửa rồi gửi lại.",
            }
        ).insert(ignore_permissions=True)
    except Exception:
        pass

    # Notify the requester (best-effort, bench-guarded) — "returned" template.
    try:
        notify.push_request_outcome(
            transaction_type=request_type,
            name=name,
            employee=_data.get("employee"),
            outcome="returned",
            state="Draft",
        )
    except Exception:
        pass

    return {
        "name": name,
        "request_type": request_type,
        "status": "Draft",
        "message": _("Đã trả lại yêu cầu để nhân viên sửa."),
    }


@frappe.whitelist()
def delegate_request(
    name: str | None = None,
    request_type: str | None = None,
    to_user: str | None = None,
    comment: str | None = None,
    until_date: str | None = None,
) -> dict:
    """Desk-free Phase B2 (§3.5) — hand THIS request's current step to ``to_user``.

    Creates a scoped ``VN Approval Delegation`` (``request_name`` = this
    request, default window today → +7 days) and logs a ``Delegate`` action.
    The request itself stays in its pending state — the delegate simply becomes
    able to act (``_user_can_act`` expands the step holder set), and every
    later decision still logs the ACTING user. Gate: the caller must hold the
    current step (or pass the HR fallback gate).
    """
    if not name or not request_type or not to_user:
        frappe.throw(_("Thiếu mã yêu cầu / loại yêu cầu / người nhận ủy quyền."))
    cfg = _config(request_type)
    if request_type not in _RETURNABLE_TYPES:
        frappe.throw(_("Loại yêu cầu không hỗ trợ ủy quyền duyệt."))
    to_user = (to_user or "").strip()
    user = _user()
    if to_user == user:
        frappe.throw(_("Không thể ủy quyền cho chính mình."))

    _lock_request_row(cfg, name)  # serialize concurrent decide (H1 parity)
    doc = frappe.get_doc(cfg["doctype"], name)
    data = doc.as_dict()
    current = doc.get(cfg["status_field"])
    if current not in cfg["pending_states"]:
        frappe.throw(
            _("Yêu cầu không ở trạng thái chờ duyệt ({0}).").format(current),
            frappe.PermissionError,
        )
    if not _user_can_act(data, request_type, user):
        frappe.throw(
            _("Bạn không phải người duyệt ở bước này."),
            frappe.PermissionError,
        )

    try:
        today = frappe.utils.today()
        until = until_date or frappe.utils.add_days(today, 7)
    except Exception:
        today, until = "", until_date or ""

    d = frappe.get_doc(
        {
            "doctype": "VN Approval Delegation",
            "from_user": user,
            "to_user": to_user,
            "transaction_type": request_type,
            "request_name": name,
            "from_date": today,
            "to_date": until,
            "reason": comment or "",
        }
    )
    d.insert(ignore_permissions=True)  # gate already passed (step holder / HR)

    _write_log(
        transaction_type=request_type,
        name=name,
        action="Delegate",
        from_state=current,
        to_state=current,
        comment=comment,
        actor=user,
    )

    # Best-effort notification to the delegate (their inbox just grew). The
    # delegate is a USER (not an Employee), so push_notification's
    # employee-first resolution does not fit — insert the payload directly.
    try:
        from gege_hr.gege_hr.utils.notify import build_notification_payload

        payload = build_notification_payload(
            employee=None,
            user=to_user,
            notification_type="Alert",
            title="Bạn được ủy quyền duyệt",
            message=f"{user} ủy quyền cho bạn duyệt yêu cầu {name} ({request_type}) đến {until}.",
            reference_doctype=cfg["doctype"],
            reference_name=name,
        )
        ndoc = frappe.get_doc(payload)
        ndoc.insert(ignore_permissions=True)
    except Exception:
        pass

    return {
        "name": name,
        "request_type": request_type,
        "to_user": to_user,
        "until_date": str(until),
        "delegation": getattr(d, "name", None),
        "status": current,
        "message": _("Đã ủy quyền duyệt cho {0}.").format(to_user),
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


# --------------------------------------------------------------------------- #
# Desk-free Phase A (plans/approvals-deskfree-complete) — shared pending loader
# + processed bucket + filter options + request detail + CSV export.
# --------------------------------------------------------------------------- #
# Request types whose pending state is a VN workflow field that can be safely
# moved back to Draft ("Trả lại để sửa", Phase B). HRMS-managed types reject.
_RETURNABLE_TYPES = {
    "Overtime Request",
    "Correction Request",
    "Salary Advance Request",
    "Leave Cancellation Request",
}

# Upper bound of VN Approval Log rows scanned for the processed bucket.
_PROCESSED_LOG_CAP = 200
# Upper bound of CSV export rows (parity checkout-miss export doctrine).
_EXPORT_ROW_CAP = 10000


def _delegated_users(
    owner_user: str,
    transaction_type: str,
    request_name: str | None = None,
    _cache: dict | None = None,
) -> list[str]:
    """Users ``owner_user`` has handed approval authority to (desk-free B2 §3.5).

    Active = ``is_active`` + today within ``[from_date, to_date]`` +
    ``transaction_type`` matching (or ``All``) + the row being unscoped or
    scoped to exactly ``request_name``. Fail-soft → ``[]`` (a missing doctype
    on a fresh bench never breaks the decide-path). ``_cache`` memoizes per
    (owner, type, request) across an inbox load.
    """
    if not owner_user:
        return []
    key = ("del", owner_user, transaction_type, request_name or "")
    if _cache is not None and key in _cache:
        return _cache[key]
    try:
        today = frappe.utils.today()
        rows = frappe.db.get_all(
            "VN Approval Delegation",
            filters={
                "from_user": owner_user,
                "is_active": 1,
                "from_date": ["<=", today],
                "to_date": [">=", today],
                "transaction_type": ["in", ["All", transaction_type]],
            },
            fields=["name", "to_user", "request_name"],
        )
    except Exception:
        rows = []
    out: list[str] = []
    for r in rows:
        scoped = (r.get("request_name") or "").strip()
        if scoped and scoped != (request_name or ""):
            continue
        if r.get("to_user") and r["to_user"] not in out:
            out.append(r["to_user"])
    if _cache is not None:
        _cache[key] = out
    return out


def _actionable_pending_rows(
    ttype: str,
    user: str,
    *,
    date: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    search: str | None = None,
    _cache: dict | None = None,
) -> list[dict]:
    """Pending rows of one transaction type the approver may act on.

    Extracted from ``get_pending_approvals`` so the inbox, ``filter_options``
    and ``export_pending_csv`` share ONE loading path (identical filters +
    matrix gate + search semantics). ``_cache`` memoizes matrix/employee
    lookups across rows — pass the same dict for every type of one request.
    """
    cfg = rules.TRANSACTION_CONFIG.get(ttype)
    if not cfg:
        return []
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
        return []
    cache = _cache if _cache is not None else {}
    return [
        r
        for r in rows
        if _user_can_act(r, ttype, user, _cache=cache) and _matches_search(r, search)
    ]


def _processed_groups(
    user: str,
    request_type: str | None,
    from_date: str | None,
    to_date: str | None,
    search: str | None,
) -> list[dict]:
    """The approver's decision history, grouped like the pending inbox (§3.1).

    Latest Approve/Reject log row per document wins; documents that no longer
    exist are skipped silently. ``from_date``/``to_date`` filter on
    ``action_at`` (the decision time, not the request date).
    """
    filters = [["actor", "=", user], ["action", "in", ["Approve", "Reject"]]]
    if request_type:
        cfg = rules.TRANSACTION_CONFIG.get(request_type)
        if cfg:
            filters.append(["reference_doctype", "=", cfg["doctype"]])
    if from_date:
        filters.append(["action_at", ">=", f"{from_date} 00:00:00"])
    if to_date:
        filters.append(["action_at", "<=", f"{to_date} 23:59:59"])
    try:
        logs = frappe.db.get_all(
            "VN Approval Log",
            filters=filters,
            fields=["reference_doctype", "reference_name", "action", "action_at"],
            order_by="action_at desc",
            limit_page_length=_PROCESSED_LOG_CAP,
        )
    except Exception:
        return []

    seen: dict = {}  # (doctype, name) → newest log row
    for lg in logs:
        key = (lg.get("reference_doctype"), lg.get("reference_name"))
        if key not in seen:
            seen[key] = lg

    dt_to_type = {cfg["doctype"]: t for t, cfg in rules.TRANSACTION_CONFIG.items()}
    grouped: dict = {}
    for (dt, nm), lg in seen.items():
        t = dt_to_type.get(dt)
        if not t or (request_type and t != request_type):
            continue
        try:
            rows = frappe.db.get_all(
                dt, filters={"name": nm}, fields=_row_fields(t), limit_page_length=1
            )
        except Exception:
            rows = []
        if not rows:
            continue  # deleted doc — skip silently (AD5)
        row = _normalise_row(rows[0], t, user)
        if not _matches_search(row, search):
            continue
        row["last_action"] = lg.get("action")
        row["last_action_at"] = str(lg.get("action_at") or "")
        grouped.setdefault(t, []).append(row)

    return [
        {
            "request_type": t,
            "label": rules.TYPE_LABELS.get(t, t),
            "count": len(rs),
            "requests": rs,
        }
        for t, rs in grouped.items()
    ]


@frappe.whitelist()
def filter_options(approver: str | None = None) -> dict:
    """§3.2 — distinct employees / departments / branches across the caller's
    pending queue (gear-popover dropdowns, parity ``leave_approval_options``).

    A plain employee (no HR/Line-Manager role) gets three empty lists — the
    inbox itself is manager-scoped, so there is nothing to filter.
    """
    user = _resolve_approver(approver)
    roles = set(emp_utils.get_user_roles() or [])
    if not (roles & (emp_utils.HR_MANAGER_ROLES | {"HR User", "Line Manager"})):
        return {"employees": [], "departments": [], "branches": []}

    # employee → { label, department, branch } — the FE facet filters map rows
    # back to department/branch through THIS mapping (rows carry no department).
    employees: dict = {}
    _act_cache: dict = {}
    for ttype in rules.supported_types():
        for r in _actionable_pending_rows(ttype, user, _cache=_act_cache):
            if r.get("employee"):
                employees.setdefault(
                    r["employee"],
                    {
                        "label": r.get("employee_name") or r["employee"],
                        "department": "",
                        "branch": "",
                    },
                )

    departments: dict = {}
    branches: dict = {}
    if employees:
        try:
            emp_rows = frappe.db.get_all(
                "Employee",
                filters={"name": ["in", list(employees.keys())]},
                fields=["name", "employee_name", "department", "branch"],
            )
        except Exception:
            emp_rows = []
        for er in emp_rows:
            info = employees.setdefault(
                er["name"],
                {"label": er.get("employee_name") or er["name"], "department": "", "branch": ""},
            )
            if er.get("department"):
                info["department"] = er["department"]
                departments.setdefault(er["department"], er["department"])
            if er.get("branch"):
                info["branch"] = er["branch"]
                branches.setdefault(er["branch"], er["branch"])

    def _opts(d: dict) -> list[dict]:
        ordered = sorted(
            d.items(), key=lambda kv: kv[1]["label"] if isinstance(kv[1], dict) else kv[1]
        )
        out: list[dict] = []
        for v, info in ordered:
            if isinstance(info, dict):
                out.append({"value": v, **info})
            else:
                out.append({"value": v, "label": info})
        return out

    return {
        "employees": _opts(employees),
        "departments": _opts(departments),
        "branches": _opts(branches),
    }


def _history_rows(doctype: str, name: str, limit: int = 15) -> list[dict]:
    """Newest-first VN Approval Log rows of one document (fail-soft)."""
    try:
        rows = frappe.db.get_all(
            "VN Approval Log",
            filters={"reference_doctype": doctype, "reference_name": name},
            fields=["action", "from_state", "to_state", "actor", "comment", "action_at"],
            order_by="action_at desc",
            limit_page_length=limit,
        )
    except Exception:
        return []
    return [dict(r) for r in rows]


def _comment_rows(doctype: str, name: str, limit: int = 20) -> list[dict]:
    """Standard Frappe Comment thread on the document (fail-soft)."""
    try:
        rows = frappe.get_all(
            "Comment",
            filters={"reference_doctype": doctype, "reference_name": name, "comment_type": "Comment"},
            fields=["name", "owner", "content", "creation"],
            order_by="creation desc",
            limit=limit,
        )
    except Exception:
        return []
    return [dict(r) for r in rows]


def _attachment_rows(doctype: str, name: str, limit: int = 20) -> list[dict]:
    """Standard Frappe File attachments on the document (fail-soft)."""
    try:
        rows = frappe.get_all(
            "File",
            filters={"attached_to_doctype": doctype, "attached_to_name": name},
            fields=["name", "file_name", "file_url", "file_size"],
            order_by="creation desc",
            limit=limit,
        )
    except Exception:
        return []
    return [dict(r) for r in rows]


def _requester_block(employee: str | None) -> dict:
    """Requester attributes for the drawer header (fail-soft)."""
    if not employee:
        return {}
    try:
        vals = (
            frappe.db.get_value(
                "Employee",
                employee,
                ["name", "employee_name", "department", "branch", "reports_to", "grade"],
                as_dict=True,
            )
            or {}
        )
    except Exception:
        vals = {}
    out = dict(vals)
    if vals.get("reports_to"):
        try:
            out["reports_to_name"] = frappe.db.get_value(
                "Employee", vals["reports_to"], "employee_name"
            )
        except Exception:
            out["reports_to_name"] = None
    return out


def _detail_matrix(request_type: str, data: dict) -> dict:
    """The active matrix step context for the drawer (fail-soft, no matrices
    configured → an empty approver list; the role gate decides as usual)."""
    cfg = rules.TRANSACTION_CONFIG[request_type]
    state = data.get(cfg["status_field"])
    out: dict = {"current_state": state, "current_step_no": None, "approvers": []}
    try:
        matrices = _load_matrices(request_type, data.get("company"))
        attrs = _employee_attrs(data.get("employee"))
        matrix = rules.pick_matrix(matrices, attrs) if matrices else None
    except Exception:
        matrix = None
    if not matrix:
        return out
    step = rules.current_step(matrix, state)
    if not step:
        return out
    out["current_step_no"] = step.get("step_no")
    approver = step.get("approver_user") or step.get("approver_role") or step.get("approver_type")
    out["approvers"] = [approver] if approver else []
    return out


def _detail_context(request_type: str, data: dict) -> dict:
    """Type-specific decision context for the drawer (each block fail-soft —
    a missing helper/table must never blank the whole payload)."""
    ctx: dict = {}
    employee = data.get("employee")
    try:
        if request_type == "Leave Application":
            from gege_hr.gege_hr.api import leave as leave_api

            ctx["leave_balance"] = leave_api.my_leave_balance(employee) or []
            ctx["blackout"] = {
                "requires_approval": bool(data.get("vn_requires_blackout_approval")),
                "decision": data.get("vn_blackout_decision") or "",
            }
        elif request_type == "Leave Cancellation Request":
            linked = data.get("leave_application")
            if linked:
                ctx["linked_leave"] = frappe.db.get_value(
                    "Leave Application",
                    linked,
                    [
                        "name",
                        "employee",
                        "leave_type",
                        "from_date",
                        "to_date",
                        "total_leave_days",
                        "status",
                    ],
                    as_dict=True,
                )
        elif request_type == "Overtime Request":
            from gege_hr.gege_hr.api import overtime as ot_api

            ctx["shift"] = ot_api._shift_instance_meta(data.get("shift_instance"))
            ctx["work_session"] = ot_api._work_session_meta(data.get("work_session"))
        elif request_type == "Correction Request":
            from gege_hr.gege_hr.api import correction as cr_api

            ctx["shift"] = cr_api._shift_instance_meta(data.get("shift_instance"))
            ctx["work_session"] = cr_api._work_session_meta(data.get("work_session"))
            ctx["generated_checkin"] = cr_api._checkin_meta(data.get("generated_checkin"))
            ctx["generated_attendance"] = cr_api._attendance_meta(data.get("generated_attendance"))
    except Exception:
        # Keep whatever was collected before the failure — partial context is
        # strictly better than none for the deciding approver.
        pass
    return ctx


@frappe.whitelist()
def get_request_detail(name: str | None = None, request_type: str | None = None) -> dict:
    """§3.3 — one request's 360° decision context for the SPA drawer.

    Unifies what the approver needs BEFORE deciding: the live document row, the
    requester block, the active matrix step, type-specific context (leave
    balance / planned-vs-actual windows / linked docs), the approval history
    (``VN Approval Log``), standard Comments + attachments, and a
    server-driven ``can`` action matrix.

    Viewer gate: the current-step approver, an HR role, or the owning employee
    — nobody else (AD8).
    """
    if not name or not request_type:
        frappe.throw(_("Thiếu mã yêu cầu / loại yêu cầu."))
    cfg = _config(request_type)
    user = _user()

    doc = frappe.get_doc(cfg["doctype"], name)  # read permission checked here
    data = doc.as_dict()
    state = data.get(cfg["status_field"])
    pending = state in cfg["pending_states"]
    roles = set(emp_utils.get_user_roles() or [])
    is_hr = bool(roles & emp_utils.HR_MANAGER_ROLES)
    can_act = bool(pending and _user_can_act(data, request_type, user))
    try:
        own_employee = emp_utils.get_employee_for_user()
    except Exception:
        own_employee = None
    is_owner = bool(data.get("employee") and data.get("employee") == own_employee)
    if not (can_act or is_hr or is_owner):
        frappe.throw(_("Bạn không có quyền xem yêu cầu này."), frappe.PermissionError)

    row = _normalise_row(data, request_type, user)
    can = {
        "approve": can_act,
        "reject": can_act,
        "return": bool(can_act and request_type in _RETURNABLE_TYPES),
        "edit_amount": bool(can_act and request_type == "Salary Advance Request"),
    }

    return {
        "name": name,
        "request_type": request_type,
        "label": rules.TYPE_LABELS.get(request_type, request_type),
        "doc": row,
        "requester": _requester_block(data.get("employee")),
        "matrix": _detail_matrix(request_type, data),
        "context": _detail_context(request_type, data),
        "history": _history_rows(cfg["doctype"], name),
        "comments": _comment_rows(cfg["doctype"], name),
        "attachments": _attachment_rows(cfg["doctype"], name),
        "can": can,
    }


def _csv_cell(v) -> str:
    """RFC-4180 quoting for one CSV cell."""
    s = str(v if v is not None else "")
    if any(c in s for c in (",", '"', "\n", "\r")):
        s = '"' + s.replace('"', '""') + '"'
    return s


def _departments_by_employee(employees: list) -> dict:
    """``{employee: department}`` for a batch (one query, fail-soft)."""
    emps = [e for e in (employees or []) if e]
    if not emps:
        return {}
    try:
        rows = frappe.db.get_all(
            "Employee", filters={"name": ["in", emps]}, fields=["name", "department"]
        )
    except Exception:
        return {}
    return {r["name"]: (r.get("department") or "") for r in rows}


@frappe.whitelist()
def export_pending_csv(
    approver: str | None = None,
    request_type: str | None = None,
    date: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    search: str | None = None,
) -> dict:
    """§3.8 — CSV of the CURRENT pending queue (same filters as the inbox).

    Returns ``{ csv, filename }`` — the csv carries a UTF-8 BOM so Excel opens
    the Vietnamese text correctly. Cap 10 000 rows (parity checkout-miss
    export doctrine).
    """
    user = _resolve_approver(approver)
    types = [request_type] if request_type else rules.supported_types()
    _act_cache: dict = {}
    rows: list[dict] = []
    for ttype in types:
        for r in _actionable_pending_rows(
            ttype,
            user,
            date=date,
            from_date=from_date,
            to_date=to_date,
            search=search,
            _cache=_act_cache,
        ):
            row = _normalise_row(r, ttype, user)
            row["request_type"] = ttype
            rows.append(row)
            if len(rows) >= _EXPORT_ROW_CAP:
                break
        if len(rows) >= _EXPORT_ROW_CAP:
            break

    dept_by_emp = _departments_by_employee([r.get("employee") for r in rows])
    header = [
        "Loại",
        "Mã",
        "Nhân viên",
        "Mã NV",
        "Phòng ban",
        "Trạng thái",
        "Ngày yêu cầu",
        "Gửi lúc",
        "Lý do",
    ]
    lines = [",".join(header)]
    for r in rows:
        cfg = rules.TRANSACTION_CONFIG[r["request_type"]]
        status = r.get(cfg["status_field"]) or ""
        date_val = r.get("work_date") or r.get("from_date") or r.get("posting_date") or ""
        lines.append(
            ",".join(
                _csv_cell(v)
                for v in [
                    rules.TYPE_LABELS.get(r["request_type"], r["request_type"]),
                    r.get("name"),
                    r.get("employee_name") or r.get("employee"),
                    r.get("employee"),
                    dept_by_emp.get(r.get("employee")),
                    status,
                    str(date_val)[:10],
                    str(r.get("creation") or "")[:16].replace("T", " "),
                    r.get("reason") or "",
                ]
            )
        )
    csv = "\ufeff" + "\r\n".join(lines) + "\r\n"
    try:
        stamp = frappe.utils.today()
    except Exception:
        stamp = ""
    return {"csv": csv, "filename": f"duyet-yeu-cau-{stamp}.csv"}
