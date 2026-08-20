from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.model.document import Document

_BANK_BIN_LEN = 6
_ACCOUNT_MIN, _ACCOUNT_MAX = 6, 19


class VNEmployeeBankAccount(Document):
    """An employee's VND bank account for payroll payout (2026-08 ack/pay plan).

    Created/edited by the employee from the SPA profile (``/hr/profile``) —
    either by pasting a VietQR image (decoded client-side into BIN + account
    number + holder name) or by typing the details. One account per employee
    is flagged ``is_default`` and pre-selected when confirming a payslip;
    the confirm flow SNAPSHOTS the chosen account onto the Salary Slip so a
    later profile edit can never silently change a pending payout.
    """

    def validate(self):
        self._normalize_employee_name()
        self._validate_bin()
        self._validate_account_no()
        self._validate_unique()
        self._enforce_single_default()

    # ------------------------------------------------------------------ #
    def _normalize_employee_name(self):
        if self.employee and not self.employee_name:
            self.employee_name = frappe.db.get_value("Employee", self.employee, "employee_name")
        if self.employee and not self.company:
            self.company = frappe.db.get_value("Employee", self.employee, "company")

    def _validate_bin(self):
        bin_ = str(self.bank_bin or "").strip()
        if not (bin_.isdigit() and len(bin_) == _BANK_BIN_LEN):
            frappe.throw(_("Bank BIN phải gồm đúng {0} chữ số.").format(_BANK_BIN_LEN))
        self.bank_bin = bin_

    def _validate_account_no(self):
        acc = str(self.account_no or "").strip()
        if not (acc.isdigit() and _ACCOUNT_MIN <= len(acc) <= _ACCOUNT_MAX):
            frappe.throw(
                _("Số tài khoản phải gồm {0}–{1} chữ số.").format(_ACCOUNT_MIN, _ACCOUNT_MAX)
            )
        self.account_no = acc
        if not (self.account_name or "").strip():
            frappe.throw(_("Thiếu tên chủ tài khoản."))

    def _validate_unique(self):
        dup = frappe.db.get_value(
            self.doctype,
            {"employee": self.employee, "account_no": self.account_no, "name": ["!=", self.name or ""]},
            "name",
        )
        if dup:
            frappe.throw(_("Tài khoản {0} đã tồn tại trong hồ sơ của bạn.").format(self.account_no))

    def _enforce_single_default(self):
        """Exactly one default account per employee (best-effort reset of the
        previous default; guarded so a missing meta never blocks saving)."""
        if not self.is_default:
            # If this employee has NO default yet, make this one the default so
            # the payslip confirm flow always has a pre-selected account.
            has_default = frappe.db.get_value(
                self.doctype,
                {"employee": self.employee, "is_default": 1, "name": ["!=", self.name or ""]},
                "name",
            )
            if not has_default:
                self.is_default = 1
            return
        try:
            others = frappe.get_all(
                self.doctype,
                filters={"employee": self.employee, "is_default": 1, "name": ["!=", self.name or ""]},
                pluck="name",
            )
            for name in others or []:
                frappe.db.set_value(self.doctype, name, "is_default", 0)
        except Exception:
            pass
