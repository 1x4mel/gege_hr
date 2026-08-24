"""
Seed-data installer for gege_hr.

Creates the minimal default records required for the portal to be usable
right after ``bench install-app gege_hr``:

* **VN HR Portal Setting** (singleton) — sane portal/timezone defaults, wired
  to the seeded attendance policy.
* **VN Attendance Policy** ("Default Attendance Policy") — standard VN office
  policy (8h day, 5' grace, post-shift OT requiring approval, 22:00-06:00 night
  band, etc.) for the default company.
* **VN Approval Matrix** — one active matrix per transaction type (Leave /
  Overtime / Correction / Salary Advance), each routing through Line Manager →
  HR Manager. These power the unified Approval Inbox
  ([`gege_hr.gege_hr.utils.approval`](gege_hr/gege_hr/gege_hr/utils/approval.py:1)).
* **VN Leave Policy Extension** ("Default Leave Policy Extension") — company-wide
  leave policy attached to the first Leave Type (24h notice, manager approval,
  cancellable, paid). doctype-design §27.
* **VN Leave Staffing Rule** ("Default Leave Staffing Rule") — company-wide
  guardrail ensuring at least one employee stays on shift (Warning action).
  doctype-design §30.

Everything is **idempotent** (records are only created when missing, never
overwritten) and **bench-guarded** (any failure is logged via ``log_error``
so ``bench install-app`` never aborts because of seed data).

Invoked from the ``after_install`` hook
([`gege_hr.hooks.create_seed_data`](gege_hr/gege_hr/hooks.py:155)) and may also
be called manually::

    bench --site <site> execute gege_hr.gege_hr.setup.create_seed_data
"""

from __future__ import annotations

import frappe

# --------------------------------------------------------------------------- #
# Constants — names of the seed records. Centralised so re-runs match.
# --------------------------------------------------------------------------- #
DEFAULT_POLICY_NAME = "Default Attendance Policy"
DEFAULT_COMPANY_FALLBACK = "Gege"
# Bootstrap company for fresh headless deploys (no ERPNext setup wizard run).
DEFAULT_COMPANY_NAME = "GeGe Vietnam"
DEFAULT_COMPANY_ABBR = "GG"

_MATRIX_NAMES = {
    "Leave Application": "Default Leave Approval Matrix",
    "Overtime Request": "Default Overtime Approval Matrix",
    "Correction Request": "Default Correction Approval Matrix",
    "Salary Advance Request": "Default Salary Advance Approval Matrix",
}

DEFAULT_LEAVE_POLICY_NAME = "Default Leave Policy Extension"
DEFAULT_LEAVE_STAFFING_RULE_NAME = "Default Leave Staffing Rule"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _default_company() -> str | None:
    """Return the first enabled Company, falling back to a sentinel name.

    During ``install-app`` the site may not yet have a Company; we still want
    the policy/matrix records to exist (managers rename/wire the company
    afterwards). Returns ``None`` only when the Company table itself is missing
    (e.g. running outside bench), in which case seeding is skipped.

    Some ERPNext versions ship the ``Company`` doctype without a ``disabled``
    column (it was added later). The ``disabled``-filtered lookup is tried
    first; on failure (or when the column is absent) we fall back to an
    unfiltered lookup so seeding is never blocked by a schema difference.
    """
    companies = None
    try:
        companies = frappe.get_all("Company", filters={"disabled": 0}, pluck="name", order_by="name")
    except Exception:
        companies = None
    if companies is None:
        try:
            companies = frappe.get_all("Company", pluck="name", order_by="name")
        except Exception:
            return None
    if companies:
        return companies[0]
    return DEFAULT_COMPANY_FALLBACK


def _exists(doctype: str, name: str) -> bool:
    try:
        return bool(frappe.db.exists(doctype, name))
    except Exception:
        return False


def _table_ready(doctype: str) -> bool:
    """True when ``doctype`` is deployed & queryable on this bench.

    ``frappe.db.table_exists`` already prepends ``tab`` internally, so we pass
    the bare doctype name (passing ``tab<Name>`` would look for ``tabtab<Name>``).
    """
    try:
        return bool(frappe.db.table_exists(doctype))
    except Exception:
        return False


def _ensure_warehouse_type() -> None:
    """Create the ``Transit`` Warehouse Type when ERPNext's install-time
    defaults are missing (e.g. an interrupted ``bench install-app erpnext``).

    Company creation builds default warehouses referencing this type; on a
    healthy ERPNext install it already exists, making this a no-op.
    """
    if not _table_ready("Warehouse Type"):
        return
    if not _exists("Warehouse Type", "Transit"):
        try:
            frappe.get_doc(
                {
                    "doctype": "Warehouse Type",
                    "warehouse_type": "Transit",
                    "__newname": "Transit",
                }
            ).insert(ignore_permissions=True)
            frappe.db.commit()
        except Exception:
            frappe.log_error("gege_hr seed: failed to create Warehouse Type 'Transit'")


def _seed_company() -> None:
    """Create the default Company when none exists (fresh headless deploy).

    ERPNext's setup wizard normally creates the first Company; on a fresh
    server deployed via scripts the wizard never runs and every company-scoped
    seed would silently degrade. Idempotent — never touches existing Companies.
    """
    if not _table_ready("Company"):
        return
    existing = None
    try:
        existing = frappe.get_all("Company", filters={"disabled": 0}, pluck="name", limit=1)
    except Exception:
        try:
            existing = frappe.get_all("Company", pluck="name", limit=1)
        except Exception:
            return
    if existing:
        return

    _ensure_warehouse_type()
    try:
        frappe.get_doc(
            {
                "doctype": "Company",
                "company_name": DEFAULT_COMPANY_NAME,
                "abbr": DEFAULT_COMPANY_ABBR,
                "country": "Vietnam",
                "default_currency": "VND",
                "chart_of_accounts": "Standard",
            }
        ).insert(ignore_permissions=True)
        frappe.db.commit()
    except Exception:
        frappe.log_error("gege_hr seed: failed to create default Company")


# --------------------------------------------------------------------------- #
# Seed builders
# --------------------------------------------------------------------------- #
def _seed_portal_setting(policy_name: str | None) -> None:
    """Upsert the VN HR Portal Setting singleton with sane defaults.

    Single-doctype: there is exactly one row (named after the doctype). We only
    set fields when empty so a manager's manual tweaks survive re-runs.
    """
    try:
        doc = frappe.get_doc("VN HR Portal Setting", "VN HR Portal Setting")
    except Exception:
        return

    def _blank(field: str) -> bool:
        return not (doc.get(field) or "").strip()

    defaults = {
        "enable_mobile_checkin": 1,
        "require_geolocation": 1,
        "timezone": "Asia/Ho_Chi_Minh",
        "payroll_cutoff_day": 25,
        "lock_attendance_after_days": 5,
        "enable_employee_self_service": 1,
        "enable_manager_dashboard": 1,
    }
    changed = False
    for field, value in defaults.items():
        # For checks/ints, 0 is a legitimate value; only default when unset.
        if doc.get(field) in (None, ""):
            doc.set(field, value)
            changed = True

    # Wire the default attendance policy only if the manager hasn't chosen one.
    if policy_name and _blank("default_attendance_policy"):
        doc.set("default_attendance_policy", policy_name)
        changed = True

    if changed:
        doc.flags.ignore_permissions = True
        doc.save(ignore_permissions=True)


def _seed_attendance_policy(company: str) -> str | None:
    """Create the Default Attendance Policy if absent. Returns its name."""
    if not _table_ready("VN Attendance Policy"):
        return None
    if _exists("VN Attendance Policy", DEFAULT_POLICY_NAME):
        return DEFAULT_POLICY_NAME

    doc = frappe.get_doc(
        {
            "doctype": "VN Attendance Policy",
            "policy_name": DEFAULT_POLICY_NAME,
            "company": company,
            "apply_to": "All",
            "is_active": 1,
            "version": 1,
            # Grace & thresholds
            "grace_late_minutes": 5,
            "grace_early_leave_minutes": 0,
            "min_working_hours_full_day": 8.0,
            "min_working_hours_half_day": 4.0,
            "multiple_logs_strategy": "First IN Last OUT",
            "min_overtime_minutes": 30,
            "max_overtime_hours_per_shift": 4.0,
            "max_total_work_hours_per_shift": 12.0,
            # Overtime
            "allow_pre_shift_overtime": 0,
            "allow_post_shift_overtime": 1,
            "require_overtime_approval": 1,
            "overtime_rounding_method": "No Rounding",
            "overtime_rounding_minutes": 15,
            "allow_ot_compensate_late": 0,
            "allow_ot_compensate_early_leave": 0,
            "max_overtime_hours_per_day": 4.0,
            "minimum_rest_hours_between_shifts": 8.0,
            # Night & penalty
            "night_start_time": "22:00:00",
            "night_end_time": "06:00:00",
            "missing_checkin_action": "Need Review",
            "missing_checkout_action": "Need Review",
            "auto_mark_absent": 0,
        }
    )
    doc.flags.ignore_permissions = True
    doc.insert(ignore_permissions=True)
    return doc.name


def _default_steps() -> list[dict]:
    """Line Manager (step 1) → HR Manager (step 2) — the canonical 2-tier flow."""
    return [
        {"step_no": 1, "approver_type": "Line Manager"},
        {"step_no": 2, "approver_type": "HR Manager"},
    ]


def _seed_approval_matrices(company: str) -> None:
    """Create one default approval matrix per transaction type if absent."""
    if not _table_ready("VN Approval Matrix") or not _table_ready("VN Approval Step"):
        return

    for transaction_type, matrix_name in _MATRIX_NAMES.items():
        if _exists("VN Approval Matrix", matrix_name):
            continue
        try:
            doc = frappe.get_doc(
                {
                    "doctype": "VN Approval Matrix",
                    "matrix_name": matrix_name,
                    "company": company,
                    "transaction_type": transaction_type,
                    "apply_to": "All",
                    "is_active": 1,
                    "steps": _default_steps(),
                }
            )
            doc.flags.ignore_permissions = True
            doc.insert(ignore_permissions=True)
        except Exception:
            # One bad matrix shouldn't abort the whole seed batch.
            frappe.log_error(f"gege_hr seed: failed to create {matrix_name}")


def _default_leave_type() -> str | None:
    """Return the first active Leave Type, or ``None`` when none/unavailable.

    Some ERPNext builds have no ``disabled`` column on ``Leave Type`` (added in a
    later version), so the filtered query raises ``Unknown column`` and we fall
    back to an unfiltered lookup — same defensive pattern as ``_default_company``.
    """
    if not _table_ready("Leave Type"):
        return None
    rows = None
    try:
        rows = frappe.get_all("Leave Type", filters={"disabled": 0}, pluck="name", order_by="name", limit=1)
    except Exception:
        rows = None
    if rows is None:
        try:
            rows = frappe.get_all("Leave Type", pluck="name", order_by="name", limit=1)
        except Exception:
            return None
    return rows[0] if rows else None


def _seed_leave_policy_extension(company: str) -> bool:
    """Create a default VN Leave Policy Extension if absent.

    Attaches to the first available Leave Type for the company with a sensible
    VN office policy (24h notice, manager approval, allow cancel, paid). The
    leave type is skipped silently when none exists — the manager wires one
    afterwards via the doctype form. Returns ``True`` when the default row now
    exists (created or already present), else ``False``.
    """
    if not _table_ready("VN Leave Policy Extension"):
        return False
    if _exists("VN Leave Policy Extension", DEFAULT_LEAVE_POLICY_NAME):
        return True
    leave_type = _default_leave_type()
    if not leave_type:
        return False

    try:
        doc = frappe.get_doc(
            {
                "doctype": "VN Leave Policy Extension",
                "policy_name": DEFAULT_LEAVE_POLICY_NAME,
                "company": company,
                "leave_type": leave_type,
                "apply_to": "All",
                "is_active": 1,
                "min_notice_hours": 24,
                "max_consecutive_shifts": 3,
                "allow_half_shift": 1,
                "allow_custom_hours": 0,
                "require_attachment": 0,
                "require_handover": 0,
                "require_manager_approval": 1,
                "require_hr_approval": 0,
                "allow_cancel_after_approved": 1,
                "cancellation_requires_approval": 1,
                "block_if_period_locked": 1,
                "salary_impact_type": "Paid",
            }
        )
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
    except Exception:
        frappe.log_error("gege_hr seed: failed to create Default Leave Policy Extension")
    return _exists("VN Leave Policy Extension", DEFAULT_LEAVE_POLICY_NAME)


def _seed_leave_staffing_rule(company: str) -> bool:
    """Create a default VN Leave Staffing Rule if absent.

    A conservative company-wide guardrail: at least 1 employee must remain on
    shift, surfacing as a *Warning* (not a hard block) so HR can still override
    when the whole team legitimately needs the day off. Returns ``True`` when
    the default row now exists, else ``False``.
    """
    if not _table_ready("VN Leave Staffing Rule"):
        return False
    if _exists("VN Leave Staffing Rule", DEFAULT_LEAVE_STAFFING_RULE_NAME):
        return True

    try:
        doc = frappe.get_doc(
            {
                "doctype": "VN Leave Staffing Rule",
                "rule_name": DEFAULT_LEAVE_STAFFING_RULE_NAME,
                "company": company,
                "is_active": 1,
                "min_required_employees": 1,
                "max_leave_allowed": 0,
                "rule_action": "Warning",
            }
        )
        doc.flags.ignore_permissions = True
        doc.insert(ignore_permissions=True)
    except Exception:
        frappe.log_error("gege_hr seed: failed to create Default Leave Staffing Rule")
    return _exists("VN Leave Staffing Rule", DEFAULT_LEAVE_STAFFING_RULE_NAME)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def create_seed_data() -> dict:
    """Create the default portal setting, attendance policy & approval matrices.

    Idempotent: re-running only fills gaps. Safe to call from ``after_install``
    or manually via ``bench execute``. Returns a small summary dict.
    """
    created = []

    # Fresh headless deploys: bootstrap the first Company (idempotent) so the
    # company-scoped seeds below wire to a real company, not the sentinel.
    try:
        _seed_company()
    except Exception:
        frappe.log_error("gege_hr seed: company bootstrap failed")

    try:
        company = _default_company()
    except Exception:
        company = None

    # Attendance policy + matrices need a company to hang off.
    if company:
        try:
            policy_name = _seed_attendance_policy(company)
            if policy_name:
                created.append(f"policy:{policy_name}")
        except Exception:
            frappe.log_error("gege_hr seed: failed to create Default Attendance Policy")
            policy_name = None

        try:
            _seed_approval_matrices(company)
            created.append("matrices")
        except Exception:
            frappe.log_error("gege_hr seed: failed to create approval matrices")

        # Leave management defaults (doctype-design §27/§30).
        try:
            if _seed_leave_policy_extension(company):
                created.append("leave_policy_extension")
        except Exception:
            frappe.log_error("gege_hr seed: failed to create leave policy extension")

        try:
            if _seed_leave_staffing_rule(company):
                created.append("leave_staffing_rule")
        except Exception:
            frappe.log_error("gege_hr seed: failed to create leave staffing rule")
    else:
        policy_name = None

    # Portal setting is a singleton independent of company — seed it regardless.
    try:
        _seed_portal_setting(policy_name)
        created.append("portal_setting")
    except Exception:
        frappe.log_error("gege_hr seed: failed to update VN HR Portal Setting")

    # Frappe Workflows for the request DocTypes (driven by the VN Approval
    # Matrix — see setup_workflows.py). Independent of company; safe to seed
    # unconditionally (bench-guarded internally).
    try:
        from gege_hr.gege_hr.setup_workflows import seed_workflows

        wf = seed_workflows()
        if wf.get("seeded"):
            created.append("workflows")
    except Exception:
        frappe.log_error("gege_hr seed: failed to create workflows")

    return {"seeded": created, "company": company}
