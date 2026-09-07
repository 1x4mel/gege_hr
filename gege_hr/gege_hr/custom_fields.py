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
    {"fieldname": "vn_payroll_section", "fieldtype": "Section Break", "label": "VN Payroll Settings"},
    {
        "fieldname": "vn_payroll_mode",
        "fieldtype": "Select",
        "label": "Payroll Mode",
        "options": "\nHourly\nMonthly",
        "default": "Hourly",
        "description": "Kiểu tính lương: Hourly (lương giờ) hoặc Monthly (lương tháng theo SSA.base).",
    },
    {
        "fieldname": "vn_hourly_rate",
        "fieldtype": "Currency",
        "label": "Lương giờ riêng (VND)",
        "default": "0",
        "description": "Mức lương giờ riêng của nhân viên. 0 = dùng lương giờ phòng ban → default portal.",
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
        "options": "\nMobile\nApp\nDevice\nManual\nImport\nAuto",
    },
    {
        "fieldname": "vn_auto_generated",
        "fieldtype": "Check",
        "label": "Auto Generated",
        "default": "0",
        "read_only": 1,
        "description": "1 = log do hệ thống sinh (OUT giả cho ca quên checkout).",
    },
    {
        "fieldname": "vn_checkout_miss",
        "fieldtype": "Link",
        "label": "Checkout Miss",
        "options": "VN Checkout Miss",
        "read_only": 1,
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
    # ---- Payslip ack & QR payout (plans/payslip-ack-qr-payment-plan.md) ----
    {
        "fieldname": "vn_ack_break",
        "fieldtype": "Section Break",
        "label": "VN Ack & Payout",
    },
    {
        "fieldname": "vn_ack_status",
        "fieldtype": "Select",
        "in_list_view": 1,
        "in_standard_filter": 1,
        "label": "VN Ack Status",
        "options": "\nRequested\nAwaiting Payment\nPaid",
        "read_only": 1,
    },
    {
        "fieldname": "vn_ack_note",
        "fieldtype": "Small Text",
        "label": "VN Adjustment Request Note",
        "read_only": 1,
    },
    {
        "fieldname": "vn_ack_at",
        "fieldtype": "Datetime",
        "label": "VN Ack At",
        "read_only": 1,
    },
    {
        "fieldname": "vn_payment_ref",
        "fieldtype": "Data",
        "label": "VN Payment Reference",
        "read_only": 1,
    },
    {
        "fieldname": "vn_payee_bank_bin",
        "fieldtype": "Data",
        "label": "VN Payee Bank BIN",
        "read_only": 1,
    },
    {
        "fieldname": "vn_payee_bank_name",
        "fieldtype": "Data",
        "label": "VN Payee Bank Name",
        "read_only": 1,
    },
    {
        "fieldname": "vn_payee_account_no",
        "fieldtype": "Data",
        "label": "VN Payee Account No",
        "read_only": 1,
    },
    {
        "fieldname": "vn_payee_account_name",
        "fieldtype": "Data",
        "label": "VN Payee Account Name",
        "read_only": 1,
    },
    {
        "fieldname": "vn_payee_qr_text",
        "fieldtype": "Long Text",
        "label": "VN Payee VietQR Text",
        "read_only": 1,
    },
    {
        "fieldname": "vn_payment_proof",
        "fieldtype": "Attach Image",
        "label": "VN Payment Proof",
        "read_only": 1,
    },
    {
        "fieldname": "vn_paid_at",
        "fieldtype": "Datetime",
        "label": "VN Paid At",
        "read_only": 1,
    },
    {
        "fieldname": "vn_paid_by",
        "fieldtype": "Link",
        "label": "VN Paid By",
        "options": "User",
        "read_only": 1,
    },
    # ---- Plan v2 (confirm-early & auto-lock): who/when confirmed, when the
    # slip became employee-visible (auto-confirm deadline anchor), and the
    # FINAL rejection of an adjustment request (Employee may not re-request). --
    {
        "fieldname": "vn_ack_source",
        "fieldtype": "Select",
        "in_standard_filter": 1,
        "label": "VN Ack Source",
        "options": "Employee\nAuto",
        "read_only": 1,
    },
    {
        "fieldname": "vn_visible_at",
        "fieldtype": "Datetime",
        "label": "VN Employee Visible At",
        "read_only": 1,
    },
    {
        "fieldname": "vn_ack_rejected_at",
        "fieldtype": "Datetime",
        "label": "VN Adjustment Rejected At",
        "read_only": 1,
    },
    {
        "fieldname": "vn_ack_rejected_reason",
        "fieldtype": "Small Text",
        "label": "VN Adjustment Rejected Reason",
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
        # Editable after submit (alongside native end_date/status) so HR can
        # re-pin a check-in geofence without a full cancel+amend round-trip
        # (plans/shift-assignment-frontend-crud.md §2.7).
        "allow_on_submit": 1,
    },
]


# A.6 Attendance (core) — back-link to the VN Work Session that produced it
# (FIX-1 / hr-gap-audit I-1). attendance_sync.upsert writes this so the round-trip
# between the portal's Work Session and Frappe HR's standard Attendance is traceable.
attendance_fields = [
    {
        "fieldname": "vn_work_session",
        "fieldtype": "Link",
        "label": "VN Work Session",
        "options": "VN Attendance Work Session",
        "read_only": 1,
    },
]


# --------------------------------------------------------------------------- #
# A.7 Department — hourly rate per department for the time-bracket payroll model
# --------------------------------------------------------------------------- #
department_fields = [
    {"fieldname": "vn_payroll_break", "fieldtype": "Section Break", "label": "VN Payroll"},
    {
        "fieldname": "vn_hourly_rate",
        "fieldtype": "Currency",
        "label": "Lương giờ (VND)",
        "default": "20000",
        "description": "Mức lương cơ bản theo giờ cho nhân viên thuộc phòng ban này",
    },
]

# --------------------------------------------------------------------------- #
# A.8 VN HR Portal Setting — configurable time brackets + deduction rates
# --------------------------------------------------------------------------- #
# VN Attendance Work Session — flag a session auto-closed by the checkout-miss
# engine so the UI can badge it and payroll knows OT was intentionally excluded.
work_session_checkout_miss_fields = [
    {
        "fieldname": "vn_auto_checkout",
        "fieldtype": "Check",
        "label": "Auto Checkout",
        "default": "0",
        "read_only": 1,
        "description": "1 = checkout tự sinh @ planned_end (nhân viên quên checkout).",
    },
    {
        "fieldname": "vn_checkout_miss",
        "fieldtype": "Link",
        "label": "Checkout Miss",
        "options": "VN Checkout Miss",
        "read_only": 1,
    },
]


portal_payroll_fields = [
    {"fieldname": "vn_payroll_setting_break", "fieldtype": "Section Break", "label": "Payroll Settings"},
    {
        "fieldname": "vn_time_brackets",
        "fieldtype": "Small Text",
        "label": "Time Brackets (JSON)",
        "default": '[{"from":8,"to":16,"coeff":1.0},{"from":16,"to":24,"coeff":1.2},{"from":0,"to":8,"coeff":1.5}]',
        "description": "Khung giờ + hệ số. from/to = giờ (0-24). Mỗi giờ làm việc nhân hệ số khung giờ tương ứng.",
    },
    {"fieldname": "vn_ded_bhxh", "fieldtype": "Float", "label": "BHXH (%)", "default": "8"},
    {"fieldname": "vn_ded_bhyt", "fieldtype": "Float", "label": "BHYT (%)", "default": "1.5"},
    {"fieldname": "vn_ded_bhtn", "fieldtype": "Float", "label": "BHTN (%)", "default": "1"},
    {"fieldname": "vn_ded_tncn", "fieldtype": "Float", "label": "Thuế TNCN (%)", "default": "10"},
    {
        "fieldname": "vn_default_hourly_rate",
        "fieldtype": "Currency",
        "label": "Default lương giờ (VND)",
        "default": "20000",
        "description": "Lương giờ mặc định cho NV chưa có phòng ban",
    },
    {
        "fieldname": "vn_cm_break",
        "fieldtype": "Section Break",
        "label": "Quên Checkout (Auto-close)",
    },
    {
        "fieldname": "vn_cm_enabled",
        "fieldtype": "Check",
        "label": "Bật auto-close quên checkout",
        "default": "1",
        "description": "Tự đóng ca thiếu checkout @ planned_end + tạo ticket giải trình.",
    },
    {
        "fieldname": "vn_cm_grace_hours",
        "fieldtype": "Int",
        "label": "Grace giải trình (giờ)",
        "default": "24",
        "description": "Hạn giải trình; quá hạn tự Penalised.",
    },
    {
        "fieldname": "vn_cm_free_first_n",
        "fieldtype": "Int",
        "label": "Miễn phạt N lần đầu",
        "default": "2",
    },
    {
        "fieldname": "vn_cm_penalty_amount",
        "fieldtype": "Currency",
        "label": "Phạt mỗi lần (VND)",
        "default": "100000",
    },
    {
        "fieldname": "vn_cm_window_days",
        "fieldtype": "Int",
        "label": "Cửa sổ đếm (ngày)",
        "default": "90",
        "description": "Reset bộ đếm occurrence sau khoảng này.",
    },
    {
        "fieldname": "vn_cm_buffer_minutes",
        "fieldtype": "Int",
        "label": "Buffer sau planned_end (phút)",
        "default": "360",
        "description": "Sau planned_end + buffer mới tự đóng. 360ph (6h) cho NV kịp checkout muộn/OT mà không bị đóng oan.",
    },
    # Desk-free COMPLETE (B1/B6/C4) — email toggle, evidence caps, auto-assign.
    {
        "fieldname": "vn_cm_email_enabled",
        "fieldtype": "Check",
        "label": "Gửi email thông báo checkout-miss",
        "default": "0",
        "description": "Email kết quả giải trình / miễn phạt / phạt tới NV (best-effort, không bao giờ chặn flow).",
    },
    {
        "fieldname": "vn_cm_max_evidence_files",
        "fieldtype": "Int",
        "label": "Số ảnh minh chứng tối đa",
        "default": "5",
        "description": "0 = không giới hạn số tệp.",
    },
    {
        "fieldname": "vn_cm_max_evidence_mb",
        "fieldtype": "Float",
        "label": "Tổng dung lượng minh chứng (MB)",
        "default": "10",
        "description": "0 = không giới hạn dung lượng.",
    },
    {
        "fieldname": "vn_cm_auto_assign_enabled",
        "fieldtype": "Check",
        "label": "Tự phân công ticket Explained cho HR",
        "default": "0",
        "description": "Bật Assignment Rule seed 'CM — Phân công xử lý Explained' (round robin HR Manager).",
    },
    {
        "fieldname": "vn_adjustment_presets",
        "fieldtype": "Small Text",
        "label": "Adjustment Presets (JSON)",
        "default": "[]",
        "description": "Danh sách mẫu điều chỉnh thủ công [{adjustment_type, description, amount}] — dropdown 'Chọn mẫu' ở popup Tính lương.",
    },
]

# --------------------------------------------------------------------------- #
# A.11 HRMS portal-lifecycle fields — Leave Encashment / Compensatory Leave
# Request / Travel Request (plan-test-complete-hr-extra G4/P0bis).
#
# The stock HRMS schemas do NOT carry a portal-review status the leave_extra /
# employee_services APIs need ("Rejected" is not a valid Leave Encashment
# status option; Compensatory Leave Request & Travel Request have no ``status``
# column at all — Travel dates live in the ``itinerary`` child table). The
# ``vn_status`` Select is the single source of truth for the portal lifecycle
# (Draft → Approved / Rejected), ``vn_note`` stores the reject reason, and the
# Travel-specific ``vn_from_date`` / ``vn_to_date`` / ``vn_purpose`` /
# ``vn_total_cost`` fields mirror the portal submission contract onto the doc.
# --------------------------------------------------------------------------- #
_PORTAL_LIFECYCLE_FIELDS = [
    {
        "fieldname": "vn_status",
        "fieldtype": "Select",
        "label": "VN Portal Status",
        "options": "Draft\nApproved\nRejected",
        "default": "Draft",
        "read_only": 1,
        "description": "Trạng thái portal (gege_hr); trạng thái gốc của HRMS nằm ở docstatus.",
    },
    {
        "fieldname": "vn_note",
        "fieldtype": "Small Text",
        "label": "VN Portal Note",
        "read_only": 1,
        "description": "Ghi chú portal — lý do từ chối, ...",
    },
]

travel_request_portal_fields = _PORTAL_LIFECYCLE_FIELDS + [
    {"fieldname": "vn_travel_col1", "fieldtype": "Column Break"},
    {"fieldname": "vn_from_date", "fieldtype": "Date", "label": "VN From Date", "read_only": 1},
    {"fieldname": "vn_to_date", "fieldtype": "Date", "label": "VN To Date", "read_only": 1},
    {
        "fieldname": "vn_purpose",
        "fieldtype": "Small Text",
        "label": "VN Purpose of Travel",
        "read_only": 1,
    },
    {
        "fieldname": "vn_total_cost",
        "fieldtype": "Currency",
        "label": "VN Estimated Cost",
        "read_only": 1,
    },
]

# services-deskfree P0 (§2.11 / F7) — grievance portal note. Native ``status``
# HRMS (Open/Investigated/Resolved/Invalid) LÀ nguồn sự thật lifecycle của
# grievance nên KHÔNG thêm ``vn_status`` — chỉ cần ``vn_note`` cho lý do
# rút đơn / đóng đơn (withdraw_grievance / invalidate_grievance).
grievance_portal_fields = [
    {
        "fieldname": "vn_note",
        "fieldtype": "Small Text",
        "label": "VN Portal Note",
        "read_only": 1,
        "description": "Ghi chú portal — lý do rút đơn / đóng đơn, ...",
    },
]


# Desk-free B3 (plans/approvals-deskfree-complete §3.6) — approval follow-up
# knobs on the portal setting (digest toggle + SLA/escalate hours).
approval_followup_fields = [
    {
        "fieldname": "vn_approval_followup_break",
        "fieldtype": "Section Break",
        "label": "Theo dõi phê duyệt",
    },
    {
        "fieldname": "vn_approval_digest_enabled",
        "fieldtype": "Check",
        "label": "Mail tổng hợp chờ duyệt hằng ngày",
        "default": "1",
    },
    {"fieldname": "vn_approval_followup_col1", "fieldtype": "Column Break"},
    {
        "fieldname": "vn_approval_stale_hours",
        "fieldtype": "Int",
        "label": "SLA nhắc duyệt (giờ)",
        "default": "72",
        "description": "Quá số giờ này job hằng ngày nhắc người duyệt bước hiện tại.",
    },
    {
        "fieldname": "vn_approval_escalate_hours",
        "fieldtype": "Int",
        "label": "Escalate HR sau (giờ)",
        "default": "120",
        "description": "Quá số giờ này job hằng ngày gửi thêm mail cho HR Manager.",
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
        "Attendance": attendance_fields,
        "Department": department_fields,
        "VN Attendance Work Session": work_session_checkout_miss_fields,
        "VN HR Portal Setting": portal_payroll_fields + approval_followup_fields,
        # HRMS portal-lifecycle fields (leave_extra / employee_services APIs)
        "Leave Encashment": _PORTAL_LIFECYCLE_FIELDS,
        "Compensatory Leave Request": _PORTAL_LIFECYCLE_FIELDS,
        "Travel Request": travel_request_portal_fields,
        # services-deskfree §2.11 — grievance portal note (thêm key mới thôi).
        "Employee Grievance": grievance_portal_fields,
    }
