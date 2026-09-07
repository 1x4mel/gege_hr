"""Checkout-miss desk-free COMPLETE fixtures (plan §3 B1/B3/C2/C3/C4 — S4).

Seeds the standard Frappe artifacts that keep the whole feature operable from
the SPA (``/hr/attendance/checkout-miss`` + ``/hr/admin/checkout-miss``):

* **Email Templates** (B1) — the 6 ``_EMAIL_TEMPLATES`` names referenced by
  ``api/checkout_miss.py::_email_notify``. A missing template only makes the
  best-effort sendmail no-op, but seeding them gives HR editable copy.
* **Notifications** (C3) — standard Notification doctype: ticket created →
  email the employee; status → Explained → system-notify HR Managers. The
  email one honours ``vn_cm_email_enabled`` via its condition; if a bench's
  safe_eval rejects the lookup the record simply never fires (the API-level
  ``_email_notify`` remains the primary channel — plan §7 risk).
* **Print Format** (A4) — "Biên bản giải trình quên checkout" used by
  ``download_checkout_miss_pdf``.
* **Auto Email Report** (B3) — weekly XLSX of the
  "VN Checkout Miss by Employee" Script Report to the HR Manager role.
* **Assignment Rule** (C4) — "CM — Phân công xử lý Explained", round robin
  over HR Managers, seeded DISABLED; ``save_payroll_settings`` flips it via
  ``vn_cm_auto_assign_enabled``.
* **Workflow** (C2) — delegates to
  ``setup_workflows.seed_checkout_miss_workflow`` (permissive; the API's
  ``_ALLOWED_ACTIONS`` stays the real gate).

Doctrine (parity ``setup_workflows``): idempotent — only creates missing
records, a manager's manual tweaks survive re-runs; bench-guarded — every
failure is logged and never aborts the seed::

    bench --site <site> execute gege_hr.gege_hr.setup_checkout_miss_deskfree.seed

Called from ``after_install`` / ``after_migrate`` (hooks.py).
"""

from __future__ import annotations

import frappe

MISS_DOCTYPE = "VN Checkout Miss"

# Must stay in lockstep with api/checkout_miss.py::_EMAIL_TEMPLATES.
CM_ASSIGNMENT_RULE = "CM — Phân công xử lý Explained"
PRINT_FORMAT_NAME = "Biên bản giải trình quên checkout"

_EMAIL_ENABLED_CONDITION = "frappe.db.get_single_value('VN HR Portal Setting', 'vn_cm_email_enabled')"


def _exists(doctype: str, name: str) -> bool:
    try:
        return bool(frappe.db.exists(doctype, name))
    except Exception:
        return False


def _table_ready(doctype: str) -> bool:
    try:
        return bool(frappe.db.table_exists(doctype))
    except Exception:
        return False


def _insert_if_missing(doctype: str, name: str, payload: dict, label: str) -> bool:
    if _exists(doctype, name):
        return False
    try:
        payload = {"doctype": doctype, "name": name, **payload}
        frappe.get_doc(payload).insert(ignore_permissions=True)
        return True
    except Exception:
        frappe.log_error(f"gege_hr cm-deskfree seed: {label} ({name}) failed")
        return False


# --------------------------------------------------------------------------- #
# Email Templates (B1)
# --------------------------------------------------------------------------- #
def _seed_email_templates() -> list[str]:
    created = []
    tpl = [
        (
            "Checkout Miss — Ticket mới",
            "Bạn có ticket quên checkout ngày {doc.work_date}",
            "<p>Chào {doc.employee_name},</p>"
            "<p>Ca làm ngày <b>{doc.work_date}</b> thiếu check-out và đã được tự đóng ở giờ kết thúc ca. "
            "Vui lòng vào HR portal gửi giải trình trước hạn <b>{doc.grace_deadline}</b>.</p>",
        ),
        (
            "Checkout Miss — Đã nhận giải trình",
            "Đã nhận giải trình quên checkout ngày {doc.work_date}",
            "<p>Chào {doc.employee_name},</p>"
            "<p>Hệ thống đã nhận giải trình quên checkout ngày <b>{doc.work_date}</b> "
            "(ticket {ticket}). HR sẽ xem xét và phản hồi.</p>",
        ),
        (
            "Checkout Miss — Miễn phạt",
            "Ticket quên checkout ngày {doc.work_date} đã được miễn phạt",
            "<p>Chào {doc.employee_name},</p>"
            "<p>HR đã <b>miễn phạt</b> ticket quên checkout ngày {doc.work_date} ({ticket}).</p>",
        ),
        (
            "Checkout Miss — Xác nhận phạt",
            "Kết quả xử lý quên checkout ngày {doc.work_date}",
            "<p>Chào {doc.employee_name},</p>"
            "<p>Ticket quên checkout ngày {doc.work_date} ({ticket}) bị xử lý "
            "<b>phạt {doc.penalty_amount}</b> theo chính sách. Liên hệ HR nếu có thắc mắc.</p>",
        ),
        (
            "Checkout Miss — Đóng ticket",
            "Ticket quên checkout ngày {doc.work_date} đã đóng",
            "<p>Chào {doc.employee_name},</p>"
            "<p>Ticket quên checkout ngày {doc.work_date} ({ticket}) đã được đóng.</p>",
        ),
        (
            "Checkout Miss — Gia hạn hạn giải trình",
            "Hạn giải trình quên checkout đã được gia hạn",
            "<p>Chào {doc.employee_name},</p>"
            "<p>Hạn giải trình cho ticket {ticket} (ngày {doc.work_date}) đã được HR gia hạn. "
            "Vui lòng gửi giải trình trước thời hạn mới.</p>",
        ),
        (
            "Checkout Miss — Khiếu nại mới",
            "Có khiếu nại phạt quên checkout cần xem xét",
            "<p>Nhân viên {doc.employee_name} vừa khiếu nại ticket quên checkout "
            "ngày {doc.work_date} ({ticket}). Vui lòng vào HR portal xử lý.</p>",
        ),
    ]
    for name, subject, body in tpl:
        if _insert_if_missing(
            "Email Template",
            name,
            {
                "subject": subject,
                "response": body,
                "enabled": 1,
            },
            "email template",
        ):
            created.append(name)
    return created


# --------------------------------------------------------------------------- #
# Standard Notifications (C3)
# --------------------------------------------------------------------------- #
def _seed_notifications() -> list[str]:
    # NOTE: the "ticket created → email NV" event is NOT a standard Notification
    # here — Frappe validates ``condition`` in a safe_eval sandbox without
    # ``frappe.db``, so the vn_cm_email_enabled gate cannot live there. That
    # event rides the doc_events hook → api.checkout_miss.on_ticket_created →
    # _email_notify (same toggle, same templates). Only the HR-side system
    # notification is seeded below.
    created = []
    specs = [
        {
            "name": "CM — Đã giải trình → HR",
            "document_type": MISS_DOCTYPE,
            "event": "Value Change",
            "value_changed": "status",
            "channel": "System Notification",
            "subject": "Checkout-miss {{ doc.name }} chờ HR xử lý",
            "recipients": [
                {
                    "recipient_by": "Email",
                    "value": "{{ frappe.db.get_list('Has Role', {'role': 'HR Manager', 'parenttype': 'User'}, pluck='parent') | join(', ') }}",
                }
            ],
            "message": "<p>{{ doc.employee_name }} đã giải trình ticket {{ doc.name }} "
            "(ngày {{ doc.work_date }}).</p>",
            "condition": "doc.status == 'Explained'",
            "enabled": 1,
        },
    ]
    for spec in specs:
        name = spec.pop("name")
        if _insert_if_missing("Notification", name, spec, "notification"):
            created.append(name)
    return created


# --------------------------------------------------------------------------- #
# Print Format (A4)
# --------------------------------------------------------------------------- #
def _seed_print_format() -> str | None:
    if _exists("Print Format", PRINT_FORMAT_NAME):
        return None
    html = """
<div class="print-format">
  <h2 style="text-align:center">BIÊN BẢN GIẢI TRÌNH QUÊN CHECKOUT</h2>
  <p style="text-align:center">Mã ticket: {{ doc.name }}</p>
  <table class="table table-bordered" style="margin-top:24px">
    <tr><td style="width:35%"><b>Nhân viên</b></td><td>{{ doc.employee_name }} ({{ doc.employee }})</td></tr>
    <tr><td><b>Ngày làm việc</b></td><td>{{ doc.work_date }}</td></tr>
    <tr><td><b>Ca</b></td><td>{{ doc.shift_type or '—' }}</td></tr>
    <tr><td><b>Tự đóng lúc</b></td><td>{{ doc.auto_checkout_at or '—' }}</td></tr>
    <tr><td><b>Hạn giải trình</b></td><td>{{ doc.grace_deadline or '—' }}</td></tr>
    <tr><td><b>Lần vi phạm</b></td><td>{{ doc.occurrence_no }}</td></tr>
    <tr><td><b>Giải trình của NV</b></td><td>{{ doc.explanation or '—' }}</td></tr>
    <tr><td><b>Khiếu nại</b></td><td>{{ doc.appeal_text or '—' }}</td></tr>
    <tr><td><b>Kết quả</b></td><td>{{ doc.status }}{% if doc.penalty_amount %} — phạt {{ doc.penalty_amount }}{% endif %}</td></tr>
    <tr><td><b>Ghi chú HR</b></td><td>{{ doc.note or '—' }}</td></tr>
  </table>
  <div style="margin-top:48px; display:flex; justify-content:space-between">
    <div>Người giải trình<br><br>_______________________</div>
    <div>Đại diện HR<br><br>_______________________</div>
  </div>
</div>
"""
    try:
        frappe.get_doc(
            {
                "doctype": "Print Format",
                "name": PRINT_FORMAT_NAME,
                "print_format_name": PRINT_FORMAT_NAME,
                "doc_type": MISS_DOCTYPE,
                "standard": "No",
                "custom_format": 0,
                "html": html,
            }
        ).insert(ignore_permissions=True)
        return PRINT_FORMAT_NAME
    except Exception:
        frappe.log_error("gege_hr cm-deskfree seed: print format failed")
        return None


# --------------------------------------------------------------------------- #
# Auto Email Report (B3) + Assignment Rule (C4)
# --------------------------------------------------------------------------- #
def _seed_auto_email_report() -> str | None:
    # NOTE: Auto Email Report is autonamed by its ``report`` link — the record's
    # name IS "VN Checkout Miss by Employee" regardless of our label.
    name = "VN Checkout Miss by Employee"
    if _exists("Auto Email Report", name):
        return None
    if not _exists("Report", "VN Checkout Miss by Employee"):
        return None  # report syncs on migrate; next seed pass picks it up
    try:
        # This bench validates ``email_to`` as REAL emails (no role names), so
        # resolve the HR Manager users' emails at seed time. The recipient list
        # is fixed at seed — a hire/later change needs the record edited once.
        emails = sorted(
            {
                r.get("email")
                for r in frappe.get_all(
                    "User",
                    filters={"enabled": 1},
                    fields=["email", "name"],
                )
                if r.get("email")
                and frappe.db.exists(
                    "Has Role", {"role": "HR Manager", "parenttype": "User", "parent": r.get("name")}
                )
            }
        )
        frappe.get_doc(
            {
                "doctype": "Auto Email Report",
                "user": "Administrator",  # mandatory Link owner on the doctype
                "report": "VN Checkout Miss by Employee",
                "report_type": "Script Report",
                "frequency": "Weekly",
                "format": "XLSX",
                "email_to": ", ".join(emails) or "dev@gege.local",
                "filters": "{}",
                "enabled": 1,
            }
        ).insert(ignore_permissions=True)
        return name
    except Exception:
        frappe.log_error("gege_hr cm-deskfree seed: auto email report failed")
        return None


def _seed_assignment_rule() -> str | None:
    if _exists("Assignment Rule", CM_ASSIGNMENT_RULE):
        return None
    try:
        users = sorted(
            {
                r.get("parent")
                for r in frappe.get_all(
                    "Has Role",
                    filters={"role": "HR Manager", "parenttype": "User"},
                    fields=["parent"],
                )
                if r.get("parent")
            }
        )
        if not users:
            return None  # nothing to round-robin; retry next migrate
        frappe.get_doc(
            {
                "doctype": "Assignment Rule",
                "name": CM_ASSIGNMENT_RULE,
                "description": CM_ASSIGNMENT_RULE,
                "document_type": MISS_DOCTYPE,
                "assignment_rule": "Round Robin",
                # Mandatory on the doctype: the condition lives in
                # ``assign_condition`` (+ per-day priority child rows).
                "assign_condition": "doc.status == 'Explained'",
                "assignment_days": [
                    {"day": d, "priority": 1}
                    for d in (
                        "Monday",
                        "Tuesday",
                        "Wednesday",
                        "Thursday",
                        "Friday",
                        "Saturday",
                        "Sunday",
                    )
                ],
                "disabled": 1,  # enabled via vn_cm_auto_assign_enabled (C4)
                "users": [{"user": u} for u in users[:10]],
            }
        ).insert(ignore_permissions=True)
        return CM_ASSIGNMENT_RULE
    except Exception:
        frappe.log_error("gege_hr cm-deskfree seed: assignment rule failed")
        return None


def sync_assignment_rule_enabled() -> None:
    """Flip the seeded rule with ``vn_cm_auto_assign_enabled`` (C4).

    Called from ``save_payroll_settings``; best-effort (a deleted rule just
    logs). Idempotent by nature — a plain set_value.
    """
    try:
        enabled = bool(frappe.db.get_single_value("VN HR Portal Setting", "vn_cm_auto_assign_enabled"))
        if _exists("Assignment Rule", CM_ASSIGNMENT_RULE):
            frappe.db.set_value("Assignment Rule", CM_ASSIGNMENT_RULE, "disabled", 0 if enabled else 1)
    except Exception:
        frappe.log_error("gege_hr cm-deskfree: assignment rule toggle failed")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def seed() -> dict:
    """Idempotent + bench-guarded seed (after_install / after_migrate)."""
    out: dict = {"templates": [], "notifications": [], "print_format": None, "auto_email_report": None}
    try:
        out["templates"] = _seed_email_templates()
    except Exception:
        frappe.log_error("gege_hr cm-deskfree seed: templates crashed")
    if not _table_ready("Notification"):
        out["notifications_skipped"] = "notification tables not ready"
    else:
        try:
            out["notifications"] = _seed_notifications()
        except Exception:
            frappe.log_error("gege_hr cm-deskfree seed: notifications crashed")
    try:
        out["print_format"] = _seed_print_format()
    except Exception:
        frappe.log_error("gege_hr cm-deskfree seed: print format crashed")
    try:
        out["auto_email_report"] = _seed_auto_email_report()
    except Exception:
        frappe.log_error("gege_hr cm-deskfree seed: auto email report crashed")
    try:
        out["assignment_rule"] = _seed_assignment_rule()
    except Exception:
        frappe.log_error("gege_hr cm-deskfree seed: assignment rule crashed")
    try:
        from gege_hr.gege_hr.setup_workflows import seed_checkout_miss_workflow

        out["workflow"] = seed_checkout_miss_workflow()
    except Exception:
        frappe.log_error("gege_hr cm-deskfree seed: workflow crashed")
        out["workflow"] = None
    return out
