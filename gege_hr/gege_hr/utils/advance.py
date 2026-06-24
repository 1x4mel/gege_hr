"""
Salary-advance eligibility helpers — pure functions (plan v5 §16 /
doctype-design §22).

Bench-free and side-effect free so the eligibility maths can be unit-tested
outside a Frappe site (mirroring the ``utils/calc.py`` convention). All
persistence / ``frappe.db`` lookups live in the VN Salary Advance Request
DocType and :mod:`gege_hr.gege_hr.api.advance`.
"""

from __future__ import annotations

from datetime import date

try:  # pragma: no cover — Frappe is optional for these pure helpers.
    import frappe  # type: ignore
    from frappe.utils import getdate  # type: ignore
except Exception:  # pragma: no cover
    frappe = None

    def getdate(value):  # type: ignore[misc]
        """Minimal stand-in for ``frappe.utils.getdate`` (bench-free tests)."""
        if value is None:
            return date.today()
        if isinstance(value, date) and not isinstance(value, type):
            return value
        if isinstance(value, date):
            return value
        s = str(value).strip()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                return date.fromisoformat(s) if fmt == "%Y-%m-%d" else _strptime(s, fmt)
            except Exception:
                continue
        # Last resort: ISO parse.
        return date.fromisoformat(s[:10])


def _strptime(s: str, fmt: str):  # pragma: no cover
    from datetime import datetime as _dt

    return _dt.strptime(s, fmt).date()


def compute_eligible_amount(base_salary: float, policy: dict | None) -> float:
    """Largest advance the policy permits against ``base_salary`` (plan §22).

    Capped by the smaller of ``max_percentage``% of base or ``max_fixed_amount``
    (when the latter is set and positive). Returns ``0`` when there is no
    policy / no base. Never negative.
    """
    if not policy:
        return 0.0
    try:
        base = float(base_salary or 0)
    except (TypeError, ValueError):
        base = 0.0
    if base <= 0:
        return 0.0

    try:
        pct = float(policy.get("max_percentage") or 0)
    except (TypeError, ValueError):
        pct = 0.0
    eligible = base * (pct / 100.0)

    fixed = policy.get("max_fixed_amount")
    try:
        fixed = float(fixed or 0)
    except (TypeError, ValueError):
        fixed = 0.0
    if fixed > 0:
        eligible = min(eligible, fixed)
    return round(max(eligible, 0.0), 2)


def pick_advance_policy(policies: list[dict], attrs: dict) -> dict | None:
    """Most specific active policy applying to the employee.

    Specificity: an employee-group match is the strongest signal, then a branch
    match, then company-only. Only ``is_active`` policies are considered. Ties
    break on ``modified`` desc so the latest config wins.
    """
    candidates = [p for p in (policies or []) if p.get("is_active")]
    if not candidates:
        return None

    def specificity(p: dict) -> tuple:
        score = 0
        if p.get("employee_group") and p.get("employee_group") == attrs.get("employee_group"):
            score += 4
        if p.get("branch") and p.get("branch") == attrs.get("branch"):
            score += 2
        if p.get("company") and p.get("company") == attrs.get("company"):
            score += 1
        return (score, str(p.get("modified") or ""))

    candidates.sort(key=specificity, reverse=True)
    return candidates[0]


def is_past_cutoff(posting_date, cutoff_day) -> bool:
    """True when ``posting_date``'s day-of-month is strictly after ``cutoff_day``."""
    try:
        day = int(cutoff_day or 0)
        if day <= 0:
            return False
        d = getdate(posting_date)
        return d.day > day
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Additional Salary (deduction) materialisation — plan v5 §16 / doctype-design
# §23. When a Salary Advance Request reaches ``Paid`` the approved amount must
# flow onto the Salary Slip. ERPNext's payroll engine picks this up from an
# ``Additional Salary`` row (type=Deduction), so we build that row here.
# --------------------------------------------------------------------------- #
DEFAULT_ADVANCE_DEDUCTION_COMPONENT = "Salary Advance"


def _as_amount(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _iso_date(value) -> str | None:
    """Coerce a date/datetime/str into a ``YYYY-MM-DD`` string (bench-free)."""
    if not value:
        return None
    try:
        d = getdate(value)
    except Exception:
        return None
    return d.isoformat() if hasattr(d, "isoformat") else str(d)


def advance_deduction_amount(sar: dict) -> float:
    """The amount to deduct: ``approved_amount`` (fallback ``requested_amount``)."""
    amount = _as_amount(sar.get("approved_amount"))
    if amount <= 0:
        amount = _as_amount(sar.get("requested_amount"))
    return round(max(amount, 0.0), 2)


def build_additional_salary_payload(
    sar: dict, salary_component: str | None = None, payroll_date=None
) -> dict:
    """Build an ERPNext ``Additional Salary`` (deduction) payload from a SAR.

    Pure / bench-free so it is unit-testable outside a Frappe site. ``sar`` is a
    mapping (e.g. a SAR ``.as_dict()``) carrying at least ``name``, ``employee``,
    ``company``, ``approved_amount``/``requested_amount`` and ``posting_date``.

    The payload is shaped for ``frappe.new_doc("Additional Salary")``:
    ``type=Deduction`` (an advance is recovered, not an earning),
    ``overwrite_salary_structure_amount=0`` so it is *added* to the slip, and the
    ``ref_doctype``/``ref_docname`` link back to the request for traceability.
    """
    component = (salary_component or "").strip() or DEFAULT_ADVANCE_DEDUCTION_COMPONENT
    date_value = payroll_date or sar.get("posting_date")
    return {
        "employee": sar.get("employee"),
        "employee_name": sar.get("employee_name"),
        "company": sar.get("company"),
        "salary_component": component,
        "amount": advance_deduction_amount(sar),
        "payroll_date": _iso_date(date_value),
        "type": "Deduction",
        "overwrite_salary_structure_amount": 0,
        "ref_doctype": "VN Salary Advance Request",
        "ref_docname": sar.get("name"),
    }


# --------------------------------------------------------------------------- #
# Additional Salary reversal — when a ``Paid`` Salary Advance Request is
# cancelled/reversed (plan v5 §16 / doctype-design §23), the deduction it
# materialised must be cancelled too so it stops hitting the Salary Slip. The
# pure predicate below decides whether reversal is needed; the bench-guarded
# cancellation itself lives in the DocType ``on_cancel`` hook + the API
# ``reverse_advance_payment`` endpoint.
# --------------------------------------------------------------------------- #
def linked_deduction_should_reverse(sar: dict) -> bool:
    """True when the SAR's linked ``Additional Salary`` must be cancelled.

    Reversal is required when:
      * the request carries a non-empty ``linked_additional_salary`` (i.e. a
        deduction row was actually created during the ``Paid`` transition), and
      * the request is leaving the ``Paid`` state — either ``workflow_state`` is
        no longer ``Paid`` or the request itself has been cancelled
        (``docstatus >= 2``).

    Pure / bench-free so the decision tree is unit-testable outside a site.
    """
    if not sar:
        return False
    if not (sar.get("linked_additional_salary") or "").strip():
        return False
    try:
        if int(sar.get("docstatus") or 0) >= 2:
            return True
    except (TypeError, ValueError):
        pass
    return (sar.get("workflow_state") or "") != "Paid"


def reset_after_reversal(sar: dict) -> dict:
    """Return the field deltas to apply after reversing a ``Paid`` advance.

    Clears ``linked_additional_salary`` (the deduction is gone) and flips
    ``payment_status`` back to ``Unpaid``. The caller (DocType hook / API)
    merges these onto the row. Pure / bench-free.
    """
    return {
        "linked_additional_salary": None,
        "payment_status": "Unpaid",
        "linked_payment_entry": None,
    }
