"""
Custom-field fixtures — plan v5 / doctype-design.md Phần A.

These dicts are returned from the ``custom_fields`` hook in ``hooks.py``.
Frappe installs them on the core DocTypes (Shift Type, Employee, Employee
Checkin) so the calculation engine can read the VN settings at runtime.

Keep fieldnames aligned with ``doctype-design.md`` Phần A exactly — the engine
in ``utils/calc.py`` reads ``vn_allow_overtime_*`` / ``vn_max_*`` from these.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# A.1 Shift Type — 11 custom fields (doctype-design §A.1)
# --------------------------------------------------------------------------- #
shift_type_fields = [
    {"fieldname": "vn_shift_settings", "fieldtype": "Section Break", "label": "VN Shift Settings"},
    {
        "fieldname": "vn_is_overnight_shift",
        "fieldtype": "Check",
        "label": "Is Overnight Shift",
        "default": "0",
    },
    {"fieldname": "vn_shift_duration_hours", "fieldtype": "Float", "label": "Shift Duration (Hours)"},
    {
        "fieldname": "vn_earliest_checkin_minutes",
        "fieldtype": "Int",
        "label": "Earliest Check-in (min before)",
        "default": "60",
    },
    {
        "fieldname": "vn_latest_checkin_minutes",
        "fieldtype": "Int",
        "label": "Latest Check-in (min after)",
        "default": "30",
    },
    {
        "fieldname": "vn_earliest_checkout_minutes",
        "fieldtype": "Int",
        "label": "Earliest Check-out (min before)",
        "default": "30",
    },
    {
        "fieldname": "vn_latest_checkout_minutes",
        "fieldtype": "Int",
        "label": "Latest Check-out (min after)",
        "default": "60",
    },
    {
        "fieldname": "vn_max_checkout_after_end_minutes",
        "fieldtype": "Int",
        "label": "Max Checkout After End (min)",
        "default": "360",
    },
    {"fieldname": "vn_col1", "fieldtype": "Column Break"},
    {
        "fieldname": "vn_allow_overtime_after_shift",
        "fieldtype": "Check",
        "label": "Allow OT After Shift",
        "default": "0",
    },
    {
        "fieldname": "vn_allow_overtime_before_shift",
        "fieldtype": "Check",
        "label": "Allow OT Before Shift",
        "default": "0",
    },
    {"fieldname": "vn_max_overtime_hours", "fieldtype": "Float", "label": "Max OT Hours", "default": "4.0"},
    {
        "fieldname": "vn_max_total_work_hours",
        "fieldtype": "Float",
        "label": "Max Total Work Hours",
        "default": "20.0",
    },
]

# --------------------------------------------------------------------------- #
# A.2 Employee — 9 custom fields (doctype-design §A.2)
# --------------------------------------------------------------------------- #
employee_fields = [
    {"fieldname": "vn_hr_settings", "fieldtype": "Section Break", "label": "VN HR Settings"},
    {"fieldname": "vn_employee_code", "fieldtype": "Data", "label": "Employee Code"},
    {"fieldname": "biometric_id", "fieldtype": "Data", "label": "Biometric ID"},
    {
        "fieldname": "default_work_location",
        "fieldtype": "Link",
        "label": "Default Work Location",
        "options": "VN Work Location",
    },
    {
        "fieldname": "default_attendance_policy",
        "fieldtype": "Link",
        "label": "Default Attendance Policy",
        "options": "VN Attendance Policy",
    },
    {"fieldname": "line_manager", "fieldtype": "Link", "label": "Line Manager", "options": "Employee"},
    {"fieldname": "vn_emp_col1", "fieldtype": "Column Break"},
    {
        "fieldname": "allow_mobile_checkin",
        "fieldtype": "Check",
        "label": "Allow Mobile Check-in",
        "default": "1",
    },
    {
        "fieldname": "allow_remote_checkin",
        "fieldtype": "Check",
        "label": "Allow Remote Check-in",
        "default": "0",
    },
    {
        "fieldname": "payroll_group",
        "fieldtype": "Link",
        "label": "Payroll Group",
        "options": "VN Payroll Component Mapping",
    },
]

# --------------------------------------------------------------------------- #
# A.3 Employee Checkin — 9 custom fields (doctype-design §A.3)
# --------------------------------------------------------------------------- #
employee_checkin_fields = [
    {"fieldname": "vn_checkin_details", "fieldtype": "Section Break", "label": "VN Check-in Details"},
    {
        "fieldname": "vn_source_type",
        "fieldtype": "Select",
        "label": "Source Type",
        "options": "\nMobile\nApp\nDevice\nManual\nImport",
    },
    {"fieldname": "vn_raw_log", "fieldtype": "Link", "label": "Raw Log", "options": "VN Attendance Raw Log"},
    {
        "fieldname": "vn_mobile_attempt",
        "fieldtype": "Link",
        "label": "Mobile Attempt",
        "options": "VN Mobile Checkin Attempt",
    },
    {"fieldname": "vn_device", "fieldtype": "Link", "label": "Device", "options": "VN Attendance Device"},
    {"fieldname": "vn_ck_col1", "fieldtype": "Column Break"},
    {
        "fieldname": "vn_work_location",
        "fieldtype": "Link",
        "label": "Work Location",
        "options": "VN Work Location",
    },
    {"fieldname": "vn_distance_meters", "fieldtype": "Float", "label": "Distance (meters)"},
    {"fieldname": "vn_gps_accuracy", "fieldtype": "Float", "label": "GPS Accuracy (meters)"},
    {
        "fieldname": "vn_validation_status",
        "fieldtype": "Select",
        "label": "Validation Status",
        "options": "Pending\nValid\nInvalid\nDuplicate",
        "default": "Pending",
    },
]

# --------------------------------------------------------------------------- #
# A.4 Salary Slip — 8 custom fields (plan v5 §10.8 / doctype-design Phần A.4)
# Stores the VN payroll breakdown on each Frappe Salary Slip so the employee
# portal payslip view can render it without re-querying the review line. The
# ``vn_employee_visible`` flag gates which slips are published to employees.
# --------------------------------------------------------------------------- #
salary_slip_fields = [
    {"fieldname": "vn_breakdown", "fieldtype": "Section Break", "label": "VN Payroll Breakdown"},
    {"fieldname": "vn_employee_visible", "fieldtype": "Check", "label": "Employee Visible", "default": "0"},
    {
        "fieldname": "vn_payable_days",
        "fieldtype": "Float",
        "label": "Payable Days",
        "precision": "2",
        "read_only": 1,
    },
    {
        "fieldname": "vn_regular_hours",
        "fieldtype": "Float",
        "label": "Regular Hours",
        "precision": "2",
        "read_only": 1,
    },
    {
        "fieldname": "vn_overtime_hours",
        "fieldtype": "Float",
        "label": "Overtime Hours",
        "precision": "2",
        "read_only": 1,
    },
    {
        "fieldname": "vn_night_overtime_hours",
        "fieldtype": "Float",
        "label": "Night Overtime Hours",
        "precision": "2",
        "read_only": 1,
    },
    {"fieldname": "vn_slip_col1", "fieldtype": "Column Break"},
    {
        "fieldname": "vn_overtime_amount",
        "fieldtype": "Currency",
        "label": "Overtime Amount",
        "options": "Company:company:default_currency",
        "read_only": 1,
    },
    {
        "fieldname": "vn_late_penalty_amount",
        "fieldtype": "Currency",
        "label": "Late Penalty Amount",
        "options": "Company:company:default_currency",
        "read_only": 1,
    },
    {
        "fieldname": "vn_salary_advance_deduction",
        "fieldtype": "Currency",
        "label": "Salary Advance Deduction",
        "options": "Company:company:default_currency",
        "read_only": 1,
    },
    {
        "fieldname": "vn_payroll_review_period",
        "fieldtype": "Link",
        "label": "Payroll Review Period",
        "options": "VN Payroll Review Period",
        "read_only": 1,
    },
]

# --------------------------------------------------------------------------- #
# A.6 Leave Application — 20 custom fields (doctype-design §A.6)
# Inserted after the core ``leave_type`` field. The leading Section Break
# ``vn_leave_details`` opens the "VN Leave Details" section so all VN fields
# are grouped together in the Frappe form. Several fields store data the
# portal/FE reads (vn_work_date, vn_shift_instance, vn_leave_hours,
# vn_leave_days_equivalent, vn_salary_impact_type) plus the cancellation /
# approval wiring (vn_cancellation_requested, vn_cancellation_request,
# vn_approval_matrix, vn_workflow_state).
# Note: ``vn_cancellation_request`` / ``vn_leave_policy_extension`` link to
# post-MVP DocTypes — Frappe allows defining the Link field before the target
# DocType exists; the engine/leave API only touches them when present.
# --------------------------------------------------------------------------- #
leave_application_fields = [
    {"fieldname": "vn_leave_details", "fieldtype": "Section Break", "label": "VN Leave Details"},
    {"fieldname": "vn_work_date", "fieldtype": "Date", "label": "Work Date"},
    {
        "fieldname": "vn_shift_instance",
        "fieldtype": "Link",
        "label": "Shift Instance",
        "options": "VN Employee Shift Instance",
    },
    {
        "fieldname": "vn_leave_duration_type",
        "fieldtype": "Select",
        "label": "Duration Type",
        "options": "\nFull Shift\nFirst Half\nSecond Half\nCustom Hours\nMulti Day",
        "default": "Full Shift",
    },
    {"fieldname": "vn_from_datetime", "fieldtype": "Datetime", "label": "From Datetime"},
    {"fieldname": "vn_to_datetime", "fieldtype": "Datetime", "label": "To Datetime"},
    {
        "fieldname": "vn_leave_hours",
        "fieldtype": "Float",
        "label": "Leave Hours",
        "default": "0",
        "precision": "2",
    },
    {
        "fieldname": "vn_leave_days_equivalent",
        "fieldtype": "Float",
        "label": "Leave Days Equivalent",
        "default": "0",
        "precision": "2",
    },
    {
        "fieldname": "vn_reason_type",
        "fieldtype": "Select",
        "label": "Reason Type",
        "options": "\nPersonal\nSick\nBusiness\nStudy\nMaternity\nOther",
    },
    {"fieldname": "vn_handover_employee", "fieldtype": "Link", "label": "Handover To", "options": "Employee"},
    {"fieldname": "vn_leave_col1", "fieldtype": "Column Break"},
    {"fieldname": "vn_handover_note", "fieldtype": "Small Text", "label": "Handover Note"},
    {"fieldname": "vn_attachment", "fieldtype": "Attach", "label": "Attachment"},
    {
        "fieldname": "vn_workflow_state",
        "fieldtype": "Select",
        "label": "Workflow State",
        "options": "\nDraft\nPending Manager\nPending HR\nApproved\nRejected\nCancelled",
        "default": "Draft",
        "read_only": 1,
    },
    {
        "fieldname": "vn_cancellation_requested",
        "fieldtype": "Check",
        "label": "Cancellation Requested",
        "default": "0",
    },
    {
        "fieldname": "vn_cancellation_request",
        "fieldtype": "Link",
        "label": "Cancellation Request",
        "options": "VN Leave Cancellation Request",
        "read_only": 1,
    },
    {
        "fieldname": "vn_approval_matrix",
        "fieldtype": "Link",
        "label": "Approval Matrix",
        "options": "VN Approval Matrix",
        "read_only": 1,
    },
    {
        "fieldname": "vn_affects_attendance",
        "fieldtype": "Check",
        "label": "Affects Attendance",
        "default": "1",
    },
    {
        "fieldname": "vn_attendance_recalculated",
        "fieldtype": "Check",
        "label": "Attendance Recalculated",
        "default": "0",
        "read_only": 1,
    },
    {
        "fieldname": "vn_locked_period",
        "fieldtype": "Check",
        "label": "Locked Period",
        "default": "0",
        "read_only": 1,
    },
    {
        "fieldname": "vn_leave_policy_extension",
        "fieldtype": "Link",
        "label": "Leave Policy Extension",
        "options": "VN Leave Policy Extension",
        "read_only": 1,
    },
    {
        "fieldname": "vn_salary_impact_type",
        "fieldtype": "Select",
        "label": "Salary Impact Type",
        "options": "\nPaid\nUnpaid\nHalf Paid",
        "default": "Paid",
    },
    {
        "fieldname": "vn_requires_blackout_approval",
        "fieldtype": "Check",
        "label": "Requires Blackout Approval",
        "default": "0",
        "read_only": 1,
    },
    {
        "fieldname": "vn_blackout_decision",
        "fieldtype": "Select",
        "label": "Blackout Decision",
        "options": "\nWarning\nRequire HR Approval\nBlock",
        "read_only": 1,
    },
]


# --------------------------------------------------------------------------- #
# A.5 Shift Assignment — geofence work location for per-shift check-in (plan v5
# §8.5 / doctype-design §A.5). Frappe HR ships a native ``shift_location`` (Link
# to "Shift Location") but that doctype carries no GPS/radius metadata, so the
# gege_hr chấm công engine cannot use it. We add our own geofence-aware Link to
# ``VN Work Location`` so an HR manager can pin a *different* check-in site per
# shift assignment (field staff / multi-site rosters). When set, it overrides
# ``Employee.default_work_location`` for every day the assignment is active.
# --------------------------------------------------------------------------- #
shift_assignment_fields = [
    {
        "fieldname": "vn_shift_assignment_location",
        "fieldtype": "Section Break",
        "label": "VN Check-in Location",
    },
    {
        "fieldname": "vn_work_location",
        "fieldtype": "Link",
        "label": "Work Location (Check-in)",
        "options": "VN Work Location",
        "description": "Địa điểm chấm công cho ca này. Để trống để dùng địa điểm mặc định của nhân viên.",
    },
]


def get_custom_fields() -> dict:
    """Return the ``{doctype: [fields]}`` map consumed by the ``custom_fields`` hook."""
    return {
        "Shift Type": shift_type_fields,
        "Employee": employee_fields,
        "Employee Checkin": employee_checkin_fields,
        "Shift Assignment": shift_assignment_fields,
        "Salary Slip": salary_slip_fields,
        "Leave Application": leave_application_fields,
    }
