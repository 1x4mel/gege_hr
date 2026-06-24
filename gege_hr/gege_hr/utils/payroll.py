"""
Payroll Review calculation helpers — plan v5 §9.6 / doctype-design §20-21.

These pure functions turn a month of VN Attendance Work Session rows into the
salary breakdown stored on a VN Payroll Review Line. They are bench-free (no
``frappe`` import at module top-level) so they can be unit-tested exactly like
:mod:`gege_hr.gege_hr.utils.calc`. Bench-only loaders live at the bottom, each
wrapped in ``try/except`` so the module imports cleanly outside a bench.

Gross/net model (plan §9.6)
--------------------------
    gross_pay          = proportional_base + overtime_amount
                         + night_allowance_amount + allowance_amount
    total_deduction    = late_penalty_amount + unpaid_leave_deduction
                         + salary_advance_deduction + other_deduction
    net_pay            = gross_pay - total_deduction

Where:

    proportional_base  = base_salary * payable_days / standard_days
    overtime_amount    = Σ hours(segment) * hourly_rate * multiplier(segment)
    night_allowance    = night_hours * night_allowance_rate
                         + ot_night_hours * night_allowance_rate * night_ot_mult
    late_penalty       = late_minutes * late_penalty_rate_per_min
    unpaid_leave_deduct= unpaid_days * (base_salary / standard_days)
"""

from __future__ import annotations

try:  # bench-free safe import
    import frappe  # type: ignore
except Exception:  # pragma: no cover
    frappe = None

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

# Segment types that map onto the "OT Hours" column of a review line.
OT_SEGMENT_TYPES = ("OT", "OT Night", "OT Holiday", "OT Holiday Night")

# Standard working days in a month (Vietnamese norm) used to prorate base salary.
DEFAULT_STANDARD_DAYS = 26.0
# Standard working hours in a month, used to derive an hourly rate from base.
DEFAULT_STANDARD_HOURS = 208.0  # 26 days × 8 hours


def _num(v, default=0.0) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return default


def round2(v: float) -> float:
    """Round to 2 dp (currency)."""
    return round(_num(v), 2)


# --------------------------------------------------------------------------- #
# Pure aggregation — Work Session rows → per-employee totals
# --------------------------------------------------------------------------- #

# Columns read off a VN Attendance Work Session row.
_WS_FIELDS = (
    "payable_day",
    "regular_hours",
    "regular_night_hours",
    "raw_overtime_hours",
    "overtime_normal_hours",
    "overtime_night_hours",
    "overtime_holiday_hours",
    "late_minutes",
    "early_leave_minutes",
    "absent",
)


def _ws_get(row: dict, key: str) -> float:
    """Read a numeric Work Session field tolerantly."""
    return _num(row.get(key), 0.0)


def aggregate_work_sessions(work_sessions: list[dict]) -> dict:
    """Fold a list of Work Session rows into a single per-employee summary.

    ``work_sessions`` items are plain dicts (as returned by
    ``frappe.db.get_all(..., as_dict=True)``) carrying the fields in
    :data:`_WS_FIELDS`. Missing keys default to 0.

    Returns a dict with: ``payable_days``, ``regular_hours``,
    ``regular_night_hours``, ``overtime_hours`` (sum of all OT types),
    ``overtime_normal_hours``, ``overtime_night_hours``,
    ``overtime_holiday_hours``, ``late_minutes``, ``early_leave_minutes``,
    ``absent_days``, ``session_count``.
    """
    agg = {
        k: 0.0
        for k in (
            "payable_days",
            "regular_hours",
            "regular_night_hours",
            "overtime_normal_hours",
            "overtime_night_hours",
            "overtime_holiday_hours",
            "late_minutes",
            "early_leave_minutes",
            "absent_days",
        )
    }
    agg["session_count"] = 0

    for row in work_sessions or []:
        agg["session_count"] += 1
        agg["payable_days"] += _ws_get(row, "payable_day")
        agg["regular_hours"] += _ws_get(row, "regular_hours")
        agg["regular_night_hours"] += _ws_get(row, "regular_night_hours")
        # Engine stores raw OT in either raw_overtime_hours (older) or the
        # per-type split; prefer the explicit split when present.
        normal = _ws_get(row, "overtime_normal_hours")
        night = _ws_get(row, "overtime_night_hours")
        holiday = _ws_get(row, "overtime_holiday_hours")
        if normal or night or holiday:
            agg["overtime_normal_hours"] += normal
            agg["overtime_night_hours"] += night
            agg["overtime_holiday_hours"] += holiday
        else:
            agg["overtime_normal_hours"] += _ws_get(row, "raw_overtime_hours")
        agg["late_minutes"] += _ws_get(row, "late_minutes")
        agg["early_leave_minutes"] += _ws_get(row, "early_leave_minutes")
        if _ws_get(row, "absent") >= 1:
            agg["absent_days"] += 1

    # Aggregate totals
    agg["overtime_hours"] = round2(
        agg["overtime_normal_hours"] + agg["overtime_night_hours"] + agg["overtime_holiday_hours"]
    )
    for k in (
        "payable_days",
        "regular_hours",
        "regular_night_hours",
        "overtime_normal_hours",
        "overtime_night_hours",
        "overtime_holiday_hours",
        "late_minutes",
        "early_leave_minutes",
    ):
        agg[k] = round2(agg[k])
    return agg


# --------------------------------------------------------------------------- #
# Pure line computation — summary → review line amounts
# --------------------------------------------------------------------------- #


def default_config() -> dict:
    """Sensible default rates (overridable per company/period)."""
    return {
        "standard_days": DEFAULT_STANDARD_DAYS,
        "standard_hours": DEFAULT_STANDARD_HOURS,
        "ot_multiplier": 1.5,  # × hourly rate, applied to all OT hours
        "night_allowance_rate": 0.0,  # per night hour
        "night_ot_multiplier": 1.5,  # additional factor on OT night hours
        "late_penalty_rate": 0.0,  # currency per late minute
        # Per-segment multipliers resolved from VN Payroll Component Mapping;
        # ``None`` ⇒ fall back to ``ot_multiplier``.
        "segment_multipliers": {},
    }


def hourly_rate(base_salary: float, standard_hours: float | None) -> float:
    """Base salary converted to an hourly wage.

    ``None`` standard hours falls back to the default; ``0`` (or negative)
    returns ``0.0`` to avoid a division-by-zero.
    """
    sh = DEFAULT_STANDARD_HOURS if standard_hours is None else _num(standard_hours)
    if sh <= 0:
        return 0.0
    return _num(base_salary) / sh


def compute_line(agg: dict, base_salary: float, config: dict | None = None) -> dict:
    """Compute a full review-line breakdown from an aggregate summary.

    Parameters
    ----------
    agg : dict
        Output of :func:`aggregate_work_sessions`.
    base_salary : float
        Monthly base salary (from Salary Structure Assignment).
    config : dict, optional
        Rate overrides — see :func:`default_config`. Missing keys fall back
        to the defaults.

    Returns
    -------
    dict
        Every currency/number field stored on a VN Payroll Review Line
        (doctype-design §21): ``base_salary``, ``payable_days``,
        ``regular_hours``, ``overtime_hours``, ``overtime_amount``,
        ``night_allowance_amount``, ``allowance_amount``,
        ``late_penalty_amount``, ``unpaid_leave_deduction``,
        ``salary_advance_deduction``, ``other_deduction``, ``gross_pay``,
        ``total_deduction``, ``net_pay``. ``salary_advance_deduction`` and
        ``other_deduction`` default to 0 and are layered on by the API layer
        (they need bench lookups); they are accepted here for test symmetry.
    """
    cfg = {**default_config(), **(config or {})}
    base = _num(base_salary)
    standard_days = _num(cfg.get("standard_days")) or DEFAULT_STANDARD_DAYS
    rate = hourly_rate(base, cfg.get("standard_hours"))
    seg_mult = cfg.get("segment_multipliers") or {}

    def _mult(seg: str) -> float:
        m = seg_mult.get(seg)
        if m is None or m == "":
            return cfg.get("ot_multiplier", 1.5)
        return _num(m, cfg.get("ot_multiplier", 1.5))

    # --- Base (prorated by payable days) ---------------------------------- #
    daily_rate = base / standard_days if standard_days > 0 else 0.0
    proportional_base = daily_rate * _num(agg.get("payable_days"))

    # --- Overtime amount -------------------------------------------------- #
    ot_normal = _num(agg.get("overtime_normal_hours"))
    ot_night = _num(agg.get("overtime_night_hours"))
    ot_holiday = _num(agg.get("overtime_holiday_hours"))
    overtime_amount = (
        ot_normal * rate * _mult("OT")
        + ot_night * rate * _mult("OT Night")
        + ot_holiday * rate * _mult("OT Holiday")
    )

    # --- Night allowance -------------------------------------------------- #
    night_rate = _num(cfg.get("night_allowance_rate"))
    night_ot_mult = _num(cfg.get("night_ot_multiplier"), 1.0)
    night_allowance_amount = (
        _num(agg.get("regular_night_hours")) * night_rate + ot_night * night_rate * night_ot_mult
    )

    # --- Late penalty ----------------------------------------------------- #
    late_penalty_amount = _num(agg.get("late_minutes")) * _num(cfg.get("late_penalty_rate"))

    # --- Deductions ------------------------------------------------------- #
    unpaid_days = max(0.0, standard_days - _num(agg.get("payable_days")))
    unpaid_leave_deduction = unpaid_days * daily_rate
    salary_advance_deduction = _num(cfg.get("salary_advance_deduction"))
    other_deduction = _num(cfg.get("other_deduction"))

    gross_pay = proportional_base + overtime_amount + night_allowance_amount
    total_deduction = (
        late_penalty_amount + unpaid_leave_deduction + salary_advance_deduction + other_deduction
    )
    net_pay = gross_pay - total_deduction

    return {
        "base_salary": round2(base),
        "payable_days": round2(agg.get("payable_days")),
        "regular_hours": round2(agg.get("regular_hours")),
        "overtime_hours": round2(agg.get("overtime_hours")),
        "overtime_amount": round2(overtime_amount),
        "night_allowance_amount": round2(night_allowance_amount),
        "allowance_amount": 0.0,
        "late_penalty_amount": round2(late_penalty_amount),
        "unpaid_leave_deduction": round2(unpaid_leave_deduction),
        "salary_advance_deduction": round2(salary_advance_deduction),
        "other_deduction": round2(other_deduction),
        "gross_pay": round2(gross_pay),
        "total_deduction": round2(total_deduction),
        "net_pay": round2(net_pay),
    }


def rollup_lines(line_amounts: list[dict]) -> dict:
    """Sum a list of review-line dicts into period totals (§20)."""
    keys = ("total_employees", "total_gross_pay", "total_deductions", "total_net_pay")
    out = {k: 0 for k in keys}
    out["total_employees"] = len(line_amounts)
    for ln in line_amounts:
        out["total_gross_pay"] += _num(ln.get("gross_pay"))
        out["total_deductions"] += _num(ln.get("total_deduction"))
        out["total_net_pay"] += _num(ln.get("net_pay"))
    out["total_gross_pay"] = round2(out["total_gross_pay"])
    out["total_deductions"] = round2(out["total_deductions"])
    out["total_net_pay"] = round2(out["total_net_pay"])
    return out


# --------------------------------------------------------------------------- #
# Bench loaders (guarded — return neutral values outside a bench)
# --------------------------------------------------------------------------- #


def load_segment_multipliers(company: str) -> dict:
    """Resolve ``{segment_type: multiplier}`` from VN Payroll Component Mapping.

    Active rules for the company with ``day_type='All'`` contribute their
    multiplier per segment type. Falls back to ``{}`` when the table is missing
    or in a bench-free context.
    """
    if frappe is None:
        return {}
    try:
        rows = (
            frappe.db.get_all(
                "VN Payroll Component Mapping",
                filters={"company": company, "is_active": 1, "day_type": "All"},
                fields=["segment_type", "multiplier"],
            )
            or []
        )
        return {r["segment_type"]: float(r["multiplier"] or 1.0) for r in rows}
    except Exception:
        return {}


def load_component_map(company: str) -> dict:
    """Resolve ``{segment_type: salary_component}`` for Additional Salary creation.

    Picks the first active mapping per segment type (specific day types take
    precedence over ``All``). Returns ``{}`` when unavailable.
    """
    if frappe is None:
        return {}
    try:
        rows = (
            frappe.db.get_all(
                "VN Payroll Component Mapping",
                filters={"company": company, "is_active": 1},
                fields=["segment_type", "day_type", "salary_component"],
            )
            or []
        )
        # Prefer specific day types over "All": track [is_all, component].
        picked: dict[str, list] = {}
        for r in rows:
            seg = r.get("segment_type")
            comp = r.get("salary_component")
            if not (seg and comp):
                continue
            is_all = r.get("day_type") == "All"
            cur = picked.get(seg)
            if cur is None or (cur[0] and not is_all):
                picked[seg] = [is_all, comp]
        return {seg: v[1] for seg, v in picked.items()}
    except Exception:
        return {}


def resolve_base_salary(employee: str, date=None) -> float:
    """Monthly base salary for ``employee`` from Salary Structure Assignment.

    Uses the assignment whose ``from_date`` is on/before ``date`` (default
    today). Returns ``0.0`` when no assignment exists or outside a bench.
    """
    if frappe is None or not employee:
        return 0.0
    try:
        # Attribute access (not `from frappe.utils import`) so a lightweight
        # fake frappe (SimpleNamespace) works in bench-free tests.
        _utils = getattr(frappe, "utils", None)
        _getdate = getattr(_utils, "getdate", None)
        _today = getattr(_utils, "today", None)
        if _getdate is None or _today is None:
            return 0.0
        as_of = _getdate(date) if date else _getdate(_today())
        row = frappe.db.get_value(
            "Salary Structure Assignment",
            {
                "employee": employee,
                "docstatus": 1,
                "from_date": ["<=", as_of],
            },
            "base",
            order_by="from_date desc",
        )
        return _num(row)
    except Exception:
        return 0.0


def employee_advance_deductions(company: str, employees: list[str], from_date, to_date) -> dict:
    """Sum approved/paid advance amounts per employee for the window.

    Returns ``{employee: amount}``. Used to populate
    ``salary_advance_deduction`` on each review line. Bench-free safe.
    """
    if frappe is None or not employees:
        return {}
    try:
        rows = (
            frappe.db.get_all(
                "VN Salary Advance Request",
                filters={
                    "company": company,
                    "employee": ["in", employees],
                    "workflow_state": ["in", ("Approved", "Paid")],
                    "posting_date": ["between", [from_date, to_date]],
                    "docstatus": ["<", 2],
                },
                fields=["employee", "approved_amount"],
            )
            or []
        )
        out: dict[str, float] = {}
        for r in rows:
            out[r["employee"]] = round2(out.get(r["employee"], 0) + _num(r.get("approved_amount")))
        return out
    except Exception:
        return {}
