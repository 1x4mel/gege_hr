"""
Hooks for gege_hr — the Vietnamese flexible-shift HR backend.

All chấm công (attendance) logic is timezone-aware: server datetimes are stored
as UTC (Frappe default) and converted to the portal timezone (Asia/Ho_Chi_Minh
by default, configured in VN HR Portal Setting) before any night-split /
work_date / late / OT calculation. See plan v5 §2.7.
"""

from __future__ import annotations

app_name = "gege_hr"
app_title = "Gege HR"
app_publisher = "Gege"
app_description = "Vietnamese flexible-shift attendance & payroll on Frappe."
app_email = "dev@gege.local"
app_license = "MIT"

# --------------------------------------------------------------------------- #
# Scheduled jobs — the checkout-miss engine: auto-close forgotten checkouts
# (synthetic OUT at planned end + ticket) and flip tickets past their grace
# deadline to "Penalised". Previously NOT registered, so the engine only ran
# opportunistically on the employee's NEXT check-in and expired tickets stayed
# "Pending" forever (late explanations were still accepted).
# --------------------------------------------------------------------------- #
scheduler_events = {
    "hourly": [
        "gege_hr.gege_hr.utils.checkout_miss.run_hourly",
    ],
}

# --------------------------------------------------------------------------- #
# Doctype permission hooks (inbox-centric migration): the gege_hr approval
# matrix authorises HR Manager / HR User to act on ANY pending request of these
# doctypes (regardless of which employee filed it). Frappe's default per-employee
# User Permission / permission_query_conditions would block the unified inbox
# (approve_request / reject_request load via get_doc + persist via doc.save — both
# permission-checked). These hooks grant the matrix-authorised roles
# access-to-all so the inbox runs through the PROPER Frappe flow (validate +
# on_update/on_submit → logs / ledger), with no ignore_permissions bypass.
# The shared implementation lives in gege_hr.gege_hr.permissions.
# --------------------------------------------------------------------------- #
# F3: single source of truth — the doctype list lives in permissions.py.
# A second hand-maintained copy here drifted silently out of sync.
from gege_hr.gege_hr.permissions import MATRIX_DOCTYPES as _MATRIX_DOCTYPES  # noqa: E402

has_permission = {dt: "gege_hr.gege_hr.permissions.has_permission" for dt in _MATRIX_DOCTYPES}
permission_query_conditions = {
    dt: "gege_hr.gege_hr.permissions.permission_query_conditions" for dt in _MATRIX_DOCTYPES
}

# --------------------------------------------------------------------------- #
# Modules owned by this app (must match modules.txt).
# --------------------------------------------------------------------------- #
app_modules = [
    {"module_name": "gege_hr"},
]

# --------------------------------------------------------------------------- #
# DocTypes owned by this app (registered for the module).
# --------------------------------------------------------------------------- #
app_doctypes = [
    # Core Policy & Setting
    {"doctype": "VN HR Portal Setting"},
    {"doctype": "VN Attendance Policy"},
    {"doctype": "VN Attendance Penalty Rule"},
    {"doctype": "VN Work Location"},
    {"doctype": "VN Attendance Device"},
    {"doctype": "VN Device Employee Mapping"},
    # Shift & Calculation
    {"doctype": "VN Employee Shift Instance"},
    {"doctype": "VN Attendance Raw Log"},
    {"doctype": "VN Mobile Checkin Attempt"},
    {"doctype": "VN Attendance Work Session"},
    {"doctype": "VN Attendance Segment"},
    {"doctype": "VN Attendance Calculation Run"},
    {"doctype": "VN Attendance Exception"},
    # Request & Payroll mapping (plan §12 / §13 / §16 / §19 / §23)
    {"doctype": "VN Attendance Correction Request"},
    {"doctype": "VN Overtime Request"},
    {"doctype": "VN Payroll Component Mapping"},
    {"doctype": "VN Salary Advance Policy"},
    {"doctype": "VN Salary Advance Request"},
    # Monthly attendance closing (plan §16-18)
    {"doctype": "VN Monthly Attendance Period"},
    {"doctype": "VN Monthly Attendance Line"},
    {"doctype": "VN Attendance Lock Log"},
    # Payroll closing (plan §20-21)
    {"doctype": "VN Payroll Review Period"},
    {"doctype": "VN Payroll Review Line"},
    # Approval routing (plan §14 / §14b / §15 — unified inbox)
    {"doctype": "VN Approval Matrix"},
    {"doctype": "VN Approval Step"},
    {"doctype": "VN Approval Log"},
    # Notification inbox (plan §10.9 / doctype-design §25)
    {"doctype": "VN Notification"},
    # Employee portal profile & audit (doctype-design §24 / §26)
    {"doctype": "VN Employee Portal Profile"},
    {"doctype": "VN Audit Event"},
    # Leave management (plan §11 / doctype-design §27-§32)
    {"doctype": "VN Leave Policy Extension"},
    {"doctype": "VN Leave Cancellation Request"},
    {"doctype": "VN Leave Calendar Cache"},
    {"doctype": "VN Leave Staffing Rule"},
    {"doctype": "VN Leave Blackout Period"},
    {"doctype": "VN Leave Handover Task"},
    # Onboarding (FIX-2 / I-2) — custom gege_hr process + template + task child.
    {"doctype": "VN Employee Onboarding"},
    {"doctype": "VN Onboarding Template"},
    {"doctype": "VN Onboarding Task"},
    # Checkout-miss auto-close (Chính sách A) — ticket per forgotten checkout.
    {"doctype": "VN Checkout Miss"},
    {"doctype": "VN Payroll Adjustment"},
]

# --------------------------------------------------------------------------- #
# Custom fields on core DocTypes (Shift Type / Employee / Employee Checkin /
# Salary Slip). Defined in doctype-design.md Phần A.
#
# NOTE: Frappe v15 does *not* auto-process the ``custom_fields`` hook during
# ``bench migrate`` — it only syncs Custom Fields exported as fixtures. So we
# declare the hook (for documentation / future-compat) AND re-apply it from the
# ``after_migrate`` / ``after_install`` hooks below via ``create_custom_fields``
# (idempotent: it upserts existing fields instead of duplicating).
# --------------------------------------------------------------------------- #
from gege_hr.gege_hr.custom_fields import get_custom_fields as _vn_custom_fields

custom_fields = _vn_custom_fields()


def sync_custom_fields():
    """(Re)apply the Phần A custom fields — called by after_migrate/after_install."""
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    create_custom_fields(_vn_custom_fields())
    _seed_checkout_miss_defaults()


def seed_advance_deduction_component():
    """Ensure the default salary-advance deduction Salary Component exists.

    ``utils.advance.DEFAULT_ADVANCE_DEDUCTION_COMPONENT`` ("Salary Advance")
    is the component stamped onto every ``Additional Salary`` row a Paid VN
    Salary Advance Request materialises. Without the component the HRMS
    insert fails validation and the hook logs-and-skips — the advance never
    reaches the Salary Slip. Idempotent; bench-guarded no-op otherwise.
    """
    import frappe

    from gege_hr.gege_hr.utils.advance import DEFAULT_ADVANCE_DEDUCTION_COMPONENT

    try:
        if frappe.db.exists("Salary Component", DEFAULT_ADVANCE_DEDUCTION_COMPONENT):
            return
        doc = frappe.get_doc(
            {
                "doctype": "Salary Component",
                "salary_component": DEFAULT_ADVANCE_DEDUCTION_COMPONENT,
                "name": DEFAULT_ADVANCE_DEDUCTION_COMPONENT,
                "type": "Deduction",
                "description": "Khấu trừ ứng lương — trừ vào lương kỳ chứa ngày xin ứng",
            }
        )
        doc.insert(ignore_permissions=True)
    except Exception:
        pass


def normalize_advance_repayment_plans():
    """Migrate legacy repayment plans to the single supported method.

    Business rule (2026-08): only ``Next Month`` — an advance requested within
    a payroll period is deducted from that period's salary (paid the next
    month). Legacy ``Installment``/``Custom``/blank rows are rewritten.
    Idempotent; best-effort no-op outside a bench or without the doctype.
    """
    import frappe

    from gege_hr.gege_hr.utils.advance import REPAYMENT_PLAN_NEXT_MONTH

    try:
        rows = frappe.get_all(
            "VN Salary Advance Request",
            filters={"docstatus": ["<", 2]},
            fields=["name", "repayment_plan"],
        )
        stale = [
            r["name"] for r in (rows or []) if (r.get("repayment_plan") or "") != REPAYMENT_PLAN_NEXT_MONTH
        ]
        for name in stale:
            frappe.db.set_value(
                "VN Salary Advance Request",
                name,
                "repayment_plan",
                REPAYMENT_PLAN_NEXT_MONTH,
                update_modified=False,
            )
    except Exception:
        pass


def _seed_checkout_miss_defaults():
    """Seed the checkout-miss config on VN HR Portal Setting if unset.

    Custom-field defaults on a Single doctype are NOT auto-written to
    ``tabSingles`` during migrate, so ``get_single_value`` returns 0 for unset
    Check/Int fields (which would wrongly read as 'disabled'). This idempotent
    step writes the documented defaults so a fresh install works out-of-box.
    """
    try:
        import frappe

        defaults = {
            "vn_cm_enabled": "1",
            "vn_cm_grace_hours": "24",
            "vn_cm_free_first_n": "2",
            "vn_cm_penalty_amount": "100000",
            "vn_cm_window_days": "90",
            "vn_cm_buffer_minutes": "360",
        }
        for field, value in defaults.items():
            current = frappe.db.get_single_value("VN HR Portal Setting", field)
            if current in (None, "", 0, "0"):
                frappe.db.set_single_value("VN HR Portal Setting", field, value)
    except Exception:
        pass


def create_seed_data():
    """Create default VN HR Portal Setting + sample policy & approval matrices.

    Idempotent; called by ``after_install`` so a fresh site is usable out of the
    box. See [`gege_hr.gege_hr.setup`](gege_hr/gege_hr/gege_hr/setup.py:1).
    """
    from gege_hr.gege_hr.setup import create_seed_data as _seed

    _seed()


# --------------------------------------------------------------------------- #
# Single-Sign-On / session
# --------------------------------------------------------------------------- #
# `me()` / `login()` / `logout()` live in gege_hr.gege_hr.api.auth.

# --------------------------------------------------------------------------- #
# Scheduled jobs
# --------------------------------------------------------------------------- #
scheduler_events = {
    # Daily, just after midnight portal time: generate shift instances for the
    # configured horizon and auto-mark absentees from the previous day.
    # Plan v2 (confirm-early & auto-lock): daily auto-confirm of overdue
    # unacknowledged payslips (default 3 days — VN HR Portal Setting
    # vn_payslip_autoconfirm_days; 0 disables).
    "daily": [
        "gege_hr.gege_hr.api.shift.generate_daily_shift_instances",
        "gege_hr.gege_hr.api.payslip_ack.run_payslip_autoconfirm",
    ],
    "cron": {
        # 02:00 portal time → auto-mark absent (stubbed; full engine in M2).
        "0 2 * * *": ["gege_hr.gege_hr.api.attendance.auto_mark_absent_job"],
        # Every hour: auto-close forgotten checkouts (employees who didn't
        # return) + flip expired Pending tickets to Penalised. Chính sách A.
        "0 * * * *": ["gege_hr.gege_hr.utils.checkout_miss.run_hourly"],
        # WP4: every 10 minutes — a dead engine must be VISIBLE (Notification
        # to HR Managers + WARN Error Log) within ~2h, not after a week of
        # payroll complaints.
        "*/10 * * * *": ["gege_hr.gege_hr.utils.health.alert_if_unhealthy"],
        # WP8: 07:00 daily — Error Log digest (top-10 titles, last 24h).
        "0 7 * * *": ["gege_hr.gege_hr.utils.health.daily_error_digest"],
        # WP5: 07:10 daily — nudge HR about Active employees missing SSA/bank.
        "10 7 * * *": ["gege_hr.gege_hr.api.onboarding.notify_missing_payroll_profile"],
        # WP6: 07:30 every day — auto-close LAST month's payroll (attempts on
        # days 1-5, alerts daily afterwards while blocked; stops at Draft).
        "30 7 * * *": ["gege_hr.gege_hr.api.payroll.auto_close_payroll"],
    },
}

# --------------------------------------------------------------------------- #
# DocType lifecycle hooks (wired progressively as backends ship).
# --------------------------------------------------------------------------- #
doc_events = {
    # Mirror gege_hr custom check-in/out windows into Frappe-native Shift Type
    # fields on every save, so native auto-attendance pairs overnight shifts
    # the same way the Work-Session engine does (vn_max_checkout_after_end_minutes
    # → allow_check_out_after_shift_end_time, etc.).
    "Shift Type": {
        "validate": [
            "gege_hr.gege_hr.api.shift.sync_native_shift_windows",
            # Keep the per-shift OT review threshold ≤ the policy's OT cap
            # (the global ceiling) — "ca không vượt mức tổng" (plan §11.5).
            "gege_hr.gege_hr.api.overtime_settings.validate_shift_ot_threshold",
            # Keep the two "check-out after shift" knobs consistent: the normal
            # check-out window must end before the late-checkout warning threshold.
            "gege_hr.gege_hr.api.overtime_settings.validate_shift_checkout_window",
        ],
    },
    # Shift instance naming + recalc trigger.
    "VN Employee Shift Instance": {
        "on_submit": "gege_hr.gege_hr.api.shift.on_shift_instance_submit",
    },
    # Raw check-in arrival → enqueue work-session recalculation.
    "Employee Checkin": {
        "after_insert": "gege_hr.gege_hr.api.attendance.on_employee_checkin_create",
    },
    # Leave workflow → refresh leave calendar cache + notify approver.
    "Leave Application": {
        "on_submit": "gege_hr.gege_hr.api.leave.on_leave_submit",
        "on_cancel": "gege_hr.gege_hr.api.leave.on_leave_cancel",
    },
    # FIX-1 (hr-gap-audit I-1): upsert a core ``Attendance`` row whenever a Work
    # Session is saved, so Frappe HR's standard reports/dashboards stay in sync
    # with the portal's Work Session (idempotent; submit only on Locked period).
    "VN Attendance Work Session": {
        "on_update": "gege_hr.gege_hr.api.attendance_sync.on_work_session_update",
    },
    # WP3 (F-LC17 prod-side): block hard-deleting an Employee that still has
    # attendance data (dangling Shift Assignments once broke the whole
    # company's materialisation) + handle Active → Left cleanly.
    "Employee": {
        "on_trash": "gege_hr.gege_hr.api.employee_lifecycle.guard_employee_delete",
        "on_update": "gege_hr.gege_hr.api.employee_lifecycle.handle_employee_status_change",
    },
}

# --------------------------------------------------------------------------- #
# Realtime (Socket.IO) channels the frontend subscribes to.
# --------------------------------------------------------------------------- #
# Frappe broadcasts via frappe.publish_realtime(event, ..., room=...).
# Channels: approvals:{manager_id}, team:{manager_id}, payroll:{company},
#           notifications:{user_id}.

# --------------------------------------------------------------------------- #
# Permissions / roles
# --------------------------------------------------------------------------- #
# Roles are created via fixtures when needed; HR Manager / HR User / Employee
# (Frappe HR) are reused. The portal distinguishes Manager vs Employee through
# standard Frappe roles in `me()`.

# --------------------------------------------------------------------------- #
# Website / REST
# --------------------------------------------------------------------------- #
website_route_rules = []
override_whitelisted_methods = {}

# --------------------------------------------------------------------------- #
# Boot session — expose portal timezone + flags to the frontend on every load.
# --------------------------------------------------------------------------- #
boot_session = "gege_hr.gege_hr.api.auth.get_boot_data"

# --------------------------------------------------------------------------- #
# Custom-field & lifecycle hooks.
# ``after_migrate`` / ``after_install`` (re)apply the Phần A custom fields since
# Frappe v15 doesn't auto-process the ``custom_fields`` hook (see note above).
# --------------------------------------------------------------------------- #
after_migrate = [
    "gege_hr.hooks.sync_custom_fields",
    # Self-heal: re-run the seed (idempotent — fills gaps only) so a site whose
    # after_install was interrupted still gets Company/policy/matrices on the
    # next `bench migrate`.
    "gege_hr.hooks.create_seed_data",
    "gege_hr.hooks.seed_advance_deduction_component",
    "gege_hr.hooks.normalize_advance_repayment_plans",
    "gege_hr.gege_hr.api.setup_permissions.grant_hr_permissions",
]

after_install = [
    "gege_hr.hooks.sync_custom_fields",
    "gege_hr.hooks.create_seed_data",
    "gege_hr.gege_hr.api.setup_permissions.grant_hr_permissions",
]

# Expose whitelisted methods to the JS client (rpc / frappe.call).
#
# Frontend (hr-ui) calls them via `gege_hr.gege_hr.api.<domain>.<fn>`.
