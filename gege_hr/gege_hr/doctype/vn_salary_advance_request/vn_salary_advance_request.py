from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, today

from gege_hr.gege_hr.utils.advance import (
    compute_eligible_amount,
    is_past_cutoff,
    pick_advance_policy,
)
from gege_hr.gege_hr.utils.naming import set_yymmdd_name

# Re-export the pure helpers so callers importing them from the DocType module
# (e.g. ``api/advance._preview_eligibility``) keep working; the canonical,
# bench-free home is :mod:`gege_hr.gege_hr.utils.advance`.
__all__ = [
    "VNSalaryAdvanceRequest",
    "compute_eligible_amount",
    "pick_advance_policy",
    "is_past_cutoff",
]


class VNSalaryAdvanceRequest(Document):
    """Employee request to draw part of an upcoming salary early (plan v5 §16 /
    doctype-design §23).

    Lifecycle is workflow-driven (``workflow_state``): Draft → Pending Manager →
    Pending HR → Approved → Paid (or Rejected/Cancelled). The unified approval
    inbox (:mod:`gege_hr.gege_hr.api.approval`) routes the request through a
    :doc:`VN Approval Matrix` (transaction_type = Salary Advance Request).

    Eligibility maths — ``eligible_amount`` — is auto-computed in ``validate``
    from the best-matching :doc:`VN Salary Advance Policy`: the smaller of
    ``max_percentage``% of base salary and ``max_fixed_amount``.
    """

    # ------------------------------------------------------------------ #
    # Lifecycle hooks
    # ------------------------------------------------------------------ #
    def before_insert(self):
        set_yymmdd_name(self, "before_insert")

    def validate(self):
        self._normalize_employee_name()
        self._normalize_company()
        self._normalize_posting_date()
        self._compute_eligible_amount()
        self._validate_amount()
        self._validate_reason()
        self._validate_policy_eligibility()
        self._validate_no_duplicate()

    def on_update(self):
        # When the request reaches ``Paid`` materialise the ERPNext ``Additional
        # Salary`` deduction so the amount flows onto the Salary Slip (plan v5
        # §16 / doctype-design §23). Idempotent: only fires once, guarded so it
        # is a silent no-op when Additional Salary isn't installed.
        if self.workflow_state == "Paid" and not self.linked_additional_salary:
            self._create_advance_deduction()

    def on_submit(self):
        # Only effective once it reaches an approved state; submitting locks it.
        if self.workflow_state not in ("Approved", "Paid"):
            self.workflow_state = "Approved"

    def on_cancel(self):
        # Cancelling a ``Paid`` request must also cancel the ``Additional
        # Salary`` deduction it created so the amount stops flowing onto the
        # Salary Slip (plan v5 §16 / doctype-design §23). Idempotent + best
        # effort: a silent no-op when there is no linked row or when the
        # ``Additional Salary`` doctype isn't installed.
        self._reverse_advance_deduction()

    # ------------------------------------------------------------------ #
    # Additional Salary materialisation (Paid state)
    # ------------------------------------------------------------------ #
    def _create_advance_deduction(self) -> None:
        """Create + link the ``Additional Salary`` deduction row for this advance.

        Best-effort and bench-guarded: it is a silent no-op when the ERPNext
        ``Additional Salary`` doctype/table is not present, and any failure is
        logged rather than aborting the ``Paid`` transition. The row is linked
        via ``linked_additional_salary`` so the step never repeats.
        """
        try:
            if not frappe.db.table_exists("Additional Salary"):
                return
        except Exception:
            return

        try:
            amount = float(self.approved_amount or 0)
        except (TypeError, ValueError):
            amount = 0.0
        if amount <= 0:
            # Nothing to deduct — still flip payment_status so the UI reflects it.
            try:
                frappe.db.set_value(self.doctype, self.name, "payment_status", "Paid")
            except Exception:
                pass
            return

        from gege_hr.gege_hr.utils.advance import build_additional_salary_payload

        payload = build_additional_salary_payload(self.as_dict())
        try:
            ad = frappe.new_doc("Additional Salary")
            ad.update(payload)
            ad.insert(ignore_permissions=True)
            # Additional Salary is submittable — submit so it is picked up by the
            # payroll run; tolerate older/non-submittable definitions gracefully.
            try:
                if getattr(ad.meta, "is_submittable", False):
                    ad.submit()
            except Exception:
                frappe.log_error(
                    title="VN Salary Advance: Additional Salary submit failed",
                    message=f"{self.name} -> {ad.name}",
                )
            link = ad.name
        except Exception:
            frappe.log_error(
                title="VN Salary Advance: Additional Salary create failed",
                message=f"{self.name}",
            )
            return

        try:
            frappe.db.set_value(
                self.doctype,
                self.name,
                {"linked_additional_salary": link, "payment_status": "Paid"},
            )
            self.linked_additional_salary = link
            self.payment_status = "Paid"
        except Exception:
            frappe.log_error(
                title="VN Salary Advance: link Additional Salary failed",
                message=f"{self.name} -> {link}",
            )

    def _reverse_advance_deduction(self) -> bool:
        """Cancel the linked ``Additional Salary`` deduction when reversing a
        ``Paid`` request.

        Returns ``True`` when a deduction was actually cancelled, ``False``
        otherwise (no link, doctype missing, already cancelled, or error —
        all of which are logged but never abort the cancel transition). After a
        successful cancellation the request's link fields are cleared and
        ``payment_status`` reset to ``Unpaid`` via :func:`reset_after_reversal`.
        """
        from gege_hr.gege_hr.utils.advance import (
            linked_deduction_should_reverse,
        )

        if not linked_deduction_should_reverse(self.as_dict()):
            return False

        try:
            if not frappe.db.table_exists("Additional Salary"):
                return False
        except Exception:
            return False

        link = self.linked_additional_salary
        try:
            ad = frappe.get_doc("Additional Salary", link)
        except Exception:
            frappe.log_error(
                title="VN Salary Advance: load Additional Salary for reversal failed",
                message=f"{self.name} -> {link}",
            )
            return False

        # Already cancelled (e.g. manual reversal) — just clear the link.
        if getattr(ad, "docstatus", 0) >= 2:
            self._apply_reversal_reset(link)
            return False

        try:
            if getattr(ad.meta, "is_submittable", False):
                ad.cancel()
            else:
                # Non-submittable: delete the row so it stops applying.
                ad.delete(ignore_permissions=True)
        except Exception:
            frappe.log_error(
                title="VN Salary Advance: cancel Additional Salary failed",
                message=f"{self.name} -> {link}",
            )
            return False

        self._apply_reversal_reset(link)
        return True

    def _apply_reversal_reset(self, link: str) -> None:
        """Clear link/payment fields after reversing the deduction row."""
        from gege_hr.gege_hr.utils.advance import reset_after_reversal

        try:
            frappe.db.set_value(self.doctype, self.name, reset_after_reversal(self.as_dict()))
        except Exception:
            frappe.log_error(
                title="VN Salary Advance: clear deduction link failed",
                message=f"{self.name} -> {link}",
            )
        self.linked_additional_salary = None
        self.payment_status = "Unpaid"
        self.linked_payment_entry = None

    # ------------------------------------------------------------------ #
    # Normalisation & computation
    # ------------------------------------------------------------------ #
    def _normalize_employee_name(self):
        if self.employee and not self.employee_name:
            self.employee_name = frappe.db.get_value("Employee", self.employee, "employee_name")

    def _normalize_company(self):
        if not self.company and self.employee:
            self.company = frappe.db.get_value("Employee", self.employee, "company")

    def _normalize_posting_date(self):
        if not self.posting_date:
            self.posting_date = today()

    def _compute_eligible_amount(self):
        """Auto-calc ``eligible_amount`` from the resolved policy + base salary.

        When the employee explicitly picked a policy we honour it; otherwise we
        resolve the best match for the employee's company/branch/group.
        """
        policy = self._resolved_policy()
        if not policy:
            self.eligible_amount = 0
            return
        base = self._base_salary()
        self.eligible_amount = compute_eligible_amount(base, policy)

    def _resolved_policy(self) -> dict | None:
        if self.salary_advance_policy:
            try:
                doc = frappe.db.get_value(
                    "VN Salary Advance Policy",
                    self.salary_advance_policy,
                    [
                        "name",
                        "company",
                        "branch",
                        "employee_group",
                        "max_percentage",
                        "max_fixed_amount",
                        "min_working_days",
                        "max_requests_per_month",
                        "cutoff_day",
                        "is_active",
                        "modified",
                    ],
                    as_dict=True,
                )
                return doc
            except Exception:
                return None
        # Auto-resolve across active policies for the company.
        try:
            rows = frappe.db.get_all(
                "VN Salary Advance Policy",
                filters={"company": self.company, "is_active": 1},
                fields=[
                    "name",
                    "company",
                    "branch",
                    "employee_group",
                    "max_percentage",
                    "max_fixed_amount",
                    "min_working_days",
                    "max_requests_per_month",
                    "cutoff_day",
                    "modified",
                ],
            )
        except Exception:
            rows = []
        attrs = self._employee_attrs()
        picked = pick_advance_policy(rows, attrs)
        if picked:
            self.salary_advance_policy = picked.get("name")
        return picked

    def _employee_attrs(self) -> dict:
        try:
            return (
                frappe.db.get_value(
                    "Employee",
                    self.employee,
                    ["company", "branch", "employee_group"],
                    as_dict=True,
                )
                or {}
            )
        except Exception:
            return {}

    def _base_salary(self) -> float:
        """Base (monthly) salary for the employee — prefer the custom
        ``vn_base_salary`` field, fall back to ``ctc``/``gross_pay``.
        """
        for field in ("vn_base_salary", "ctc", "gross_pay"):
            try:
                val = frappe.db.get_value("Employee", self.employee, field)
            except Exception:
                val = None
            try:
                val = float(val or 0)
            except (TypeError, ValueError):
                val = 0
            if val > 0:
                return val
        return 0.0

    # ------------------------------------------------------------------ #
    # Validations
    # ------------------------------------------------------------------ #
    def _validate_amount(self):
        try:
            requested = float(self.requested_amount or 0)
        except (TypeError, ValueError):
            requested = 0
        if requested <= 0:
            frappe.throw(_("Requested Amount phải lớn hơn 0."))
        try:
            eligible = float(self.eligible_amount or 0)
        except (TypeError, ValueError):
            eligible = 0
        if eligible > 0 and requested > eligible + 0.01:
            frappe.throw(_("Requested Amount ({0}) vượt Eligible Amount ({1}).").format(requested, eligible))

    def _validate_reason(self):
        if not (self.reason or "").strip():
            frappe.throw(_("Vui lòng nhập lý do ứng lương."))

    def _validate_policy_eligibility(self):
        """Enforce ``max_requests_per_month`` and ``cutoff_day`` (plan §22)."""
        policy = self._resolved_policy()
        if not policy:
            return
        # cutoff day
        cutoff = policy.get("cutoff_day")
        if cutoff and is_past_cutoff(self.posting_date, cutoff):
            frappe.throw(_("Đã quá ngày cutoff ({0}) trong tháng để xin ứng lương.").format(cutoff))
        # max requests per month
        max_per_month = policy.get("max_requests_per_month") or 0
        if max_per_month > 0:
            self._assert_monthly_quota(int(max_per_month))

    def _assert_monthly_quota(self, max_per_month: int) -> None:
        try:
            d = getdate(self.posting_date)
        except Exception:
            return
        count = frappe.db.count(
            "VN Salary Advance Request",
            filters={
                "employee": self.employee,
                "workflow_state": ["not in", ["Rejected", "Cancelled"]],
                "docstatus": ["<", 2],
                "posting_date": ["between", [d.replace(day=1), d]],
                "name": ["!=", self.name or ""],
            },
        )
        if (count or 0) >= max_per_month:
            frappe.throw(_("Đã đạt giới hạn {0} yêu cầu ứng lương trong tháng.").format(max_per_month))

    def _validate_no_duplicate(self):
        """No open duplicate ``(employee, posting_date)`` beyond the monthly
        quota already checked (kept for clarity / future differentiation)."""
        # The monthly quota above is the primary de-dup; nothing else here.
        return
