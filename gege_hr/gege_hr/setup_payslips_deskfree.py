"""Payslips desk-free COMPLETE fixtures (plan plans/payslips-deskfree-complete.md WP1).

Seeds the standard Frappe artifacts that keep the employee payslip feature fully
operable from the SPA (``/hr/payslips`` + ``/hr/payslips/:name``):

* **Print Format** — "Phiếu lương VN" for ``Salary Slip``, used by
  ``api/payroll.py::download_payslip_pdf`` (and the email attachment built by
  ``email_payslip`` / ``bulk_email_payslips`` via ``frappe.attach_print``).

Doctrine (parity ``setup_checkout_miss_deskfree``): idempotent — only creates
missing records, a manager's manual tweaks survive re-runs; bench-guarded —
every failure is logged and never aborts the seed::

    bench --site <site> execute gege_hr.gege_hr.setup_payslips_deskfree.seed

Called from ``after_install`` / ``after_migrate`` (hooks.py).
"""

from __future__ import annotations

import frappe

SLIP_DOCTYPE = "Salary Slip"
PRINT_FORMAT_NAME = "Phiếu lương VN"

# Must stay in lockstep with api/payroll.py::PAYSLIP_PRINT_FORMAT.
PRINT_FORMAT_HTML = """
<div class="print-format" style="font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; font-size: 12px;">
  <div style="text-align:center; margin-bottom: 4px;">
    <div style="font-size: 15px; font-weight: bold;">{{ doc.company }}</div>
    <h2 style="margin: 8px 0 2px 0; letter-spacing: 2px;">PHIẾU LƯƠNG</h2>
    <div>Kỳ lương: {{ doc.start_date }} → {{ doc.end_date }}</div>
    <div style="color:#666;">Mã phiếu: {{ doc.name }} · Ngày lập: {{ doc.posting_date }}</div>
  </div>
  <hr style="border: none; border-top: 1.5px solid #333; margin: 10px 0;"/>

  <table class="table table-bordered" style="width:100%; border-collapse: collapse; margin-top: 12px;">
    <tr>
      <td style="width:25%; border:1px solid #999; padding:5px;"><b>Nhân viên</b></td>
      <td style="border:1px solid #999; padding:5px;">{{ doc.employee_name }} ({{ doc.employee }})</td>
      <td style="width:22%; border:1px solid #999; padding:5px;"><b>Phòng ban</b></td>
      <td style="border:1px solid #999; padding:5px;">{{ frappe.db.get_value('Employee', doc.employee, 'department') or '' }}</td>
    </tr>
    <tr>
      <td style="border:1px solid #999; padding:5px;"><b>Vị trí</b></td>
      <td style="border:1px solid #999; padding:5px;">{{ frappe.db.get_value('Employee', doc.employee, 'designation') or '' }}</td>
      <td style="border:1px solid #999; padding:5px;"><b>Trạng thái</b></td>
      <td style="border:1px solid #999; padding:5px;">{{ doc.status }}</td>
    </tr>
  </table>

  {% set vn_hours = doc.get('vn_regular_hours') or 0 %}
  {% set vn_ot = doc.get('vn_overtime_hours') or 0 %}
  {% if vn_hours or vn_ot %}
  <p style="margin: 12px 0 4px 0; font-weight: bold;">Công tác chi tiết</p>
  <table class="table table-bordered" style="width:100%; border-collapse: collapse;">
    <tr style="background:#f2f2f2;">
      <th style="border:1px solid #999; padding:5px; text-align:left;">Ngày công tính lương</th>
      <th style="border:1px solid #999; padding:5px; text-align:right;">Giờ làm việc</th>
      <th style="border:1px solid #999; padding:5px; text-align:right;">Giờ tăng ca</th>
      <th style="border:1px solid #999; padding:5px; text-align:right;">Tiền tăng ca</th>
      <th style="border:1px solid #999; padding:5px; text-align:right;">Phụ cấp ca đêm</th>
    </tr>
    <tr>
      <td style="border:1px solid #999; padding:5px;">{{ doc.get('vn_payable_days') or 0 }}</td>
      <td style="border:1px solid #999; padding:5px; text-align:right;">{{ vn_hours }}</td>
      <td style="border:1px solid #999; padding:5px; text-align:right;">{{ vn_ot }}</td>
      <td style="border:1px solid #999; padding:5px; text-align:right;">{{ doc.get('vn_overtime_amount') or 0 }}</td>
      <td style="border:1px solid #999; padding:5px; text-align:right;">{{ doc.get('vn_night_allowance_amount') or 0 }}</td>
    </tr>
  </table>
  {% endif %}

  <p style="margin: 14px 0 4px 0; font-weight: bold;">Các khoản thu nhập</p>
  <table class="table table-bordered" style="width:100%; border-collapse: collapse;">
    <tr style="background:#f2f2f2;">
      <th style="border:1px solid #999; padding:5px; text-align:left;">Khoản</th>
      <th style="border:1px solid #999; padding:5px; text-align:right;">Số tiền</th>
    </tr>
    {% for e in doc.earnings or [] %}
    <tr>
      <td style="border:1px solid #999; padding:5px;">{{ e.salary_component }}</td>
      <td style="border:1px solid #999; padding:5px; text-align:right;">{{ e.amount }}</td>
    </tr>
    {% endfor %}
    <tr style="font-weight:bold; background:#f8fff8;">
      <td style="border:1px solid #999; padding:5px;">Tổng thu nhập</td>
      <td style="border:1px solid #999; padding:5px; text-align:right;">{{ doc.gross_pay }}</td>
    </tr>
  </table>

  <p style="margin: 14px 0 4px 0; font-weight: bold;">Các khoản khấu trừ</p>
  <table class="table table-bordered" style="width:100%; border-collapse: collapse;">
    <tr style="background:#f2f2f2;">
      <th style="border:1px solid #999; padding:5px; text-align:left;">Khoản</th>
      <th style="border:1px solid #999; padding:5px; text-align:right;">Số tiền</th>
    </tr>
    {% for d in doc.deductions or [] %}
    <tr>
      <td style="border:1px solid #999; padding:5px;">{{ d.salary_component }}</td>
      <td style="border:1px solid #999; padding:5px; text-align:right;">{{ d.amount }}</td>
    </tr>
    {% endfor %}
    <tr style="font-weight:bold; background:#fff8f8;">
      <td style="border:1px solid #999; padding:5px;">Tổng khấu trừ</td>
      <td style="border:1px solid #999; padding:5px; text-align:right;">{{ doc.total_deduction }}</td>
    </tr>
  </table>

  <div style="margin-top: 16px; border: 2px solid #333; border-radius: 6px; padding: 10px 14px; display:flex; justify-content:space-between; align-items:center;">
    <div style="font-size: 14px; font-weight: bold;">THỰC LÃNH</div>
    <div style="font-size: 20px; font-weight: bold;">{{ doc.net_pay }} {{ doc.currency or 'VND' }}</div>
  </div>

  {% if doc.get('vn_payee_account_no') %}
  <p style="margin: 12px 0 4px 0; font-weight: bold;">Thông tin chuyển khoản</p>
  <table class="table table-bordered" style="width:100%; border-collapse: collapse;">
    <tr>
      <td style="width:30%; border:1px solid #999; padding:5px;"><b>Ngân hàng</b></td>
      <td style="border:1px solid #999; padding:5px;">{{ doc.get('vn_payee_bank_name') or '' }}</td>
    </tr>
    <tr>
      <td style="border:1px solid #999; padding:5px;"><b>Số tài khoản</b></td>
      <td style="border:1px solid #999; padding:5px;">{{ doc.get('vn_payee_account_no') or '' }}</td>
    </tr>
    <tr>
      <td style="border:1px solid #999; padding:5px;"><b>Nội dung chuyển khoản</b></td>
      <td style="border:1px solid #999; padding:5px;">{{ doc.get('vn_payment_ref') or '' }}</td>
    </tr>
  </table>
  {% endif %}

  <div style="margin-top: 48px; display:flex; justify-content:space-between;">
    <div style="text-align:center;">Người nhận lương<br/><br/><br/>_______________________</div>
    <div style="text-align:center;">Đại diện công ty<br/><br/><br/>_______________________</div>
  </div>
</div>
"""


def _exists(doctype: str, name: str) -> bool:
    try:
        return bool(frappe.db.exists(doctype, name))
    except Exception:
        return False


def _seed_print_format() -> str | None:
    if _exists("Print Format", PRINT_FORMAT_NAME):
        return None
    try:
        frappe.get_doc(
            {
                "doctype": "Print Format",
                "name": PRINT_FORMAT_NAME,
                "print_format_name": PRINT_FORMAT_NAME,
                "doc_type": SLIP_DOCTYPE,
                "standard": "No",
                "custom_format": 0,
                "html": PRINT_FORMAT_HTML,
            }
        ).insert(ignore_permissions=True)
        return PRINT_FORMAT_NAME
    except Exception:
        frappe.log_error("gege_hr payslips-deskfree seed: print format failed")
        return None


def seed() -> dict:
    """Idempotent + bench-guarded seed (after_install / after_migrate)."""
    out: dict = {"print_format": None}
    try:
        out["print_format"] = _seed_print_format()
    except Exception:
        frappe.log_error("gege_hr payslips-deskfree seed failed")
    return out
