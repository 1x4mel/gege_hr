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
    "daily": [
        "gege_hr.gege_hr.api.shift.generate_daily_shift_instances",
    ],
    "cron": {
        # 02:00 portal time → auto-mark absent (stubbed; full engine in M2).
        "0 2 * * *": ["gege_hr.gege_hr.api.attendance.auto_mark_absent_job"],
    },
}

# --------------------------------------------------------------------------- #
# DocType lifecycle hooks (wired progressively as backends ship).
# --------------------------------------------------------------------------- #
doc_events = {
    # Shift instance naming + recalc trigger.
    "VN Employee Shift Instance": {
        "before_insert": "gege_hr.gege_hr.utils.naming.set_yymmdd_name",
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
