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
    # Checkout-miss penalty (layered on by the API layer, like advance/other).
    checkout_miss_penalty = _num(cfg.get("checkout_miss_penalty"))

    gross_pay = proportional_base + overtime_amount + night_allowance_amount
    total_deduction = (
        late_penalty_amount
        + unpaid_leave_deduction
        + salary_advance_deduction
        + other_deduction
        + checkout_miss_penalty
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
        "checkout_miss_penalty": round2(checkout_miss_penalty),
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


# --------------------------------------------------------------------------- #
# Hourly-rate × time-bracket payroll model (plan §payroll-hourly-rate-design)
# --------------------------------------------------------------------------- #
def resolve_hourly_rate(employee: str, date=None) -> float:
    """Hourly rate for ``employee`` from their Department's ``vn_hourly_rate``.

    Falls back to ``VN HR Portal Setting.vn_default_hourly_rate`` (default 20 000 VND)
    when the employee has no department or the department has no rate set.

    M8: ``date`` is honoured — the Department is read as of that date (latest
    Version snapshot of the Department doc at/before ``date``, when any
    exists) so a mid-period transfer prices the earlier days at the OLD
    department's rate instead of silently re-rating the whole period. Without
    a date (or without Frappe versioning for Department) the CURRENT rate is
    used, same as before.
    """
    if frappe is None or not employee:
        return 20000.0
    try:
        dept = None
        if date:
            # Employee department as of ``date``: scan the Version trail
            # (track_changes) for the latest "department" change at/before it.
            # Frappe v15 has no helper API for this — read the versions table
            # directly; any failure falls back to the current department.
            try:
                from frappe.utils import getdate

                as_of = getdate(date)
                vers = frappe.db.get_all(
                    "Version",
                    filters={
                        "ref_doctype": "Employee",
                        "docname": employee,
                        "creation": ["<=", f"{as_of} 23:59:59"],
                    },
                    fields=["data"],
                    order_by="creation desc",
                    limit_page_length=200,
                )
                import json as _json

                for v in vers:
                    try:
                        data = _json.loads(v.data or "{}")
                    except Exception:
                        continue
                    for ch in data.get("changed", []):
                        # ch = [fieldname, old, new]
                        if ch and ch[0] == "department":
                            dept = ch[2] or ch[1] or None
                            break
                    if dept:
                        break
            except Exception:
                dept = None
        if not dept:
            dept = frappe.db.get_value("Employee", employee, "department")
        if dept:
            rate = frappe.db.get_value("Department", dept, "vn_hourly_rate")
            if rate and float(rate) > 0:
                return float(rate)
        # Fallback to portal default.
        default = frappe.db.get_single_value("VN HR Portal Setting", "vn_default_hourly_rate")
        return float(default or 20000)
    except Exception:
        return 20000.0


def load_time_brackets() -> list[dict]:
    """Parse time-bracket config from ``VN HR Portal Setting.vn_time_brackets``.

    Returns ``[{from, to, coeff}, ...]`` (from/to = hour-of-day 0–24).
    """
    if frappe is None:
        return [
            {"from": 8, "to": 16, "coeff": 1.0},
            {"from": 16, "to": 24, "coeff": 1.2},
            {"from": 0, "to": 8, "coeff": 1.5},
        ]
    try:
        import json

        raw = frappe.db.get_single_value("VN HR Portal Setting", "vn_time_brackets")
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return [
        {"from": 8, "to": 16, "coeff": 1.0},
        {"from": 16, "to": 24, "coeff": 1.2},
        {"from": 0, "to": 8, "coeff": 1.5},
    ]


def load_deduction_rates() -> dict[str, float]:
    """BHXH/BHYT/BHTN/TNCN rates (percent) from VN HR Portal Setting."""
    if frappe is None:
        return {"BHXH": 8.0, "BHYT": 1.5, "BHTN": 1.0, "TNCN": 10.0}
    defaults = {"BHXH": 8.0, "BHYT": 1.5, "BHTN": 1.0, "TNCN": 10.0}
    if frappe is None:
        return dict(defaults)

    def _rate(field: str) -> float:
        # M7: ``or`` swallowed a legitimate 0 (e.g. TNCN=0 for an employee
        # under the tax threshold) and re-applied the default rate.
        val = frappe.db.get_single_value("VN HR Portal Setting", field)
        return float(val) if val is not None and val != "" else defaults[field.split("_")[-1]]

    try:
        return {k: _rate(f"vn_ded_{k.lower()}") for k in defaults}
    except Exception:
        return dict(defaults)


def load_penalty_rules(company: str | None = None) -> list[dict]:
    """Penalty rules of the company's active ``VN Attendance Policy``.

    C2 fix: ``VN Attendance Penalty Rule`` is a CHILD table — it has no
    ``company``/``is_active`` columns, so the old direct filter crashed with
    "unknown column" (silently caught → []). Late penalties were ALWAYS zero.
    Query through the parent policy instead (active + matching company).
    """
    if frappe is None:
        return []
    try:
        pf = {"is_active": 1}
        if company:
            pf["company"] = company
        policy = frappe.db.get_value("VN Attendance Policy", pf, "name")
        if not policy:
            return []
        rows = frappe.db.get_all(
            "VN Attendance Penalty Rule",
            filters={"parent": policy, "parenttype": "VN Attendance Policy"},
            fields=["from_minutes", "to_minutes", "penalty_type", "penalty_value"],
            order_by="from_minutes asc",
        )
        return rows or []
    except Exception:
        return []


def load_checkout_miss_penalty(employee: str, start_date, end_date) -> float:
    """Sum ``penalty_amount`` of Penalised, non-waived VN Checkout Miss tickets
    for ``employee`` within ``[start_date, end_date]``.

    Only ``Penalised`` tickets with ``penalty_waived=0`` count — waived ones and
    pending/explained ones are excluded. Returns ``0.0`` outside a bench / error.
    """
    if frappe is None:
        return 0.0
    try:
        rows = frappe.db.get_all(
            "VN Checkout Miss",
            filters={
                "employee": employee,
                "status": "Penalised",
                "penalty_waived": 0,
                "docstatus": ["<", 2],
                "work_date": ["between", [start_date, end_date]],
            },
            fields=["penalty_amount"],
        )
        return float(sum(float((r or {}).get("penalty_amount") or 0) for r in (rows or [])))
    except Exception:
        return 0.0


def split_hours_by_bracket(start_dt, end_dt, brackets: list[dict]) -> dict[float, float]:
    """Split a datetime range into ``{coefficient: hours}`` per time bracket.

    ``brackets`` = ``[{"from": 8, "to": 16, "coeff": 1.0}, ...]`` where from/to
    are hours-of-day (0–24). Iterates hour-by-hour; the last partial hour is
    counted fractionally. Overnight sessions (crossing midnight) are handled by
    matching the hour-of-day to the correct bracket.

    Returns ``{1.0: 120.0, 1.2: 30.5, 1.5: 8.0}`` (coeff → hours).
    """
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    VN = ZoneInfo("Asia/Ho_Chi_Minh")
    # Brackets are defined in PORTAL hours (VN). Callers historically passed
    # UTC datetimes, so every hour-of-day was shifted -7h: a day shift scored
    # the night coefficient (+37.5% gross) and a night shift lost ~30%.
    # Normalise to portal time before splitting.
    if getattr(start_dt, "tzinfo", None) is not None:
        start_dt = start_dt.astimezone(VN)
    if getattr(end_dt, "tzinfo", None) is not None:
        end_dt = end_dt.astimezone(VN)

    if start_dt >= end_dt:
        return {}

    result: dict[float, float] = {}
    cur = start_dt
    while cur < end_dt:
        hod = cur.hour + cur.minute / 60.0 + cur.second / 3600.0  # hour-of-day
        coeff = 1.0  # fallback
        for b in brackets:
            bf, bt = float(b["from"]), float(b["to"])
            if bf < bt:  # same-day bracket (08–16)
                if bf <= hod < bt:
                    coeff = float(b["coeff"])
                    break
            else:  # overnight bracket (22–06) — wraps midnight
                if hod >= bf or hod < bt:
                    coeff = float(b["coeff"])
                    break
        nxt = min(cur + timedelta(hours=1), end_dt)
        actual = (nxt - cur).total_seconds() / 3600.0
        result[coeff] = result.get(coeff, 0.0) + actual
        cur = nxt
    return result


def compute_hourly_line(
    bracket_hours: dict[float, float],
    hourly_rate: float,
    deduction_rates: dict[str, float],
    late_penalty: float = 0.0,
    checkout_miss_penalty: float = 0.0,
    salary_advance_deduction: float = 0.0,
    allowances: list[float] | None = None,
    extra_deductions: list[float] | None = None,
) -> dict:
    """Full payroll breakdown from the hourly-rate × time-bracket model.

    ``bracket_hours`` = ``{coeff: hours}`` (from :func:`split_hours_by_bracket`).
    ``deduction_rates`` = ``{"BHXH": 8.0, ...}`` (percent of gross).
    ``allowances`` = ``[500000, 200000]`` (fixed amounts from Salary Structure
    earnings — e.g. lunch, transport — added to the hourly gross).
    ``extra_deductions`` = ``[100000]`` (fixed amounts from Salary Structure
    deductions — e.g. union fee — subtracted after the % deductions).

    Returns a dict compatible with the existing VN Payroll Review Line shape.
    """
    # 1. Base gross from hourly × hours × coefficient
    base_gross = sum(hours * hourly_rate * coeff for coeff, hours in bracket_hours.items())

    # 2. Allowances (fixed amounts from Salary Structure earnings)
    total_allowances = sum(allowances or [])

    # 3. Total gross = hourly gross + allowances
    gross = base_gross + total_allowances

    # 4. Standard deductions (% of gross)
    total_pct = sum(deduction_rates.values()) / 100.0
    standard_deductions = gross * total_pct

    # 5. Extra deductions (fixed amounts from Salary Structure deductions)
    total_extra_ded = sum(extra_deductions or [])

    # 6. Net = gross - standard_deductions - extra_ded - penalties - advance
    net = (
        gross
        - standard_deductions
        - total_extra_ded
        - late_penalty
        - checkout_miss_penalty
        - salary_advance_deduction
    )

    worked_hours = sum(bracket_hours.values())
    return {
        "base_salary": round2(hourly_rate),
        "gross_pay": round2(gross),
        "total_deduction": round2(
            standard_deductions + total_extra_ded + late_penalty + checkout_miss_penalty + salary_advance_deduction
        ),
        "net_pay": round2(net),
        "hourly_rate": round2(hourly_rate),
        "worked_hours": round2(worked_hours),
        "bracket_hours_1": round2(bracket_hours.get(1.0, 0)),
        "bracket_hours_1_2": round2(bracket_hours.get(1.2, 0)),
        "bracket_hours_1_5": round2(bracket_hours.get(1.5, 0)),
        "late_penalty": round2(late_penalty),
        "checkout_miss_penalty": round2(checkout_miss_penalty),
        "allowance_amount": round2(total_allowances),
    }


def compute_late_penalty(
    late_minutes_list: list[float],
    penalty_rules: list[dict],
    daily_rate: float = 0.0,
) -> float:
    """Total late-occurrence penalty for an employee.

    For each entry in ``late_minutes_list`` (minutes late per occurrence), find
    the first matching ``VN Attendance Penalty Rule`` bracket and apply its amount.

    M4 fix: ``Percentage`` used to add the raw number (5 → 5 VND) and
    ``Half Day``/``Full Day`` silently contributed nothing. Percentage now
    applies to the daily rate; half/full day deduct half/one daily rate.
    """
    total = 0.0
    for late_min in late_minutes_list:
        for rule in penalty_rules:
            from_m = float(rule.get("from_minutes") or 0)
            to_m = float(rule.get("to_minutes") or 9999)
            if from_m <= late_min <= to_m:
                ptype = rule.get("penalty_type", "Fixed Amount")
                pval = float(rule.get("penalty_value") or 0)
                if ptype == "Fixed Amount":
                    total += pval
                elif ptype == "Per Minute":
                    total += late_min * pval
                elif ptype == "Percentage":
                    total += daily_rate * pval / 100.0
                elif ptype == "Half Day":
                    total += daily_rate / 2.0
                elif ptype == "Full Day":
                    total += daily_rate
                break
    return round2(total)
