"""Pure leave-computation helpers — plan v5 §10.5 / doctype-design A.6.

These helpers are intentionally bench-free: they never import ``frappe`` at
module scope so they can be unit-tested outside a bench (same pattern as
``utils/calc.py`` / ``utils/advance.py``). Bench loaders that need Frappe's
Leave Ledger / Holiday List live in ``api/leave.py`` and are guarded there.

The leave-day math mirrors Frappe HR's ``get_number_of_leave_days`` but is
kept dependency-free so the preview engine is deterministic and testable:

    inclusive day count  →  minus holidays (optional)  →  half-day adjust

Leave Application (Frappe HR) stores ``from_date`` / ``to_date`` as calendar
dates. ``half_day`` collapses the span to a single day counted at 0.5; a
multi-day span with ``half_day`` is treated as full-day for every day except
``half_day_date`` (Frappe semantics).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta


# --------------------------------------------------------------------------- #
# Coercion
# --------------------------------------------------------------------------- #
def coerce_date(value) -> date | None:
    """Coerce a ``date``/``datetime``/ISO ``str`` into a ``date``; ``None`` if blank."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.split(" ")[0]).date()
        except ValueError:
            try:
                return datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError:
                return None
    return None


def to_float(value, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


# --------------------------------------------------------------------------- #
# Leave-day math
# --------------------------------------------------------------------------- #
def inclusive_day_count(from_date: date, to_date: date) -> int:
    """Number of calendar days in [from_date, to_date] inclusive (>= 1)."""
    if to_date < from_date:
        return 0
    return (to_date - from_date).days + 1


def _iter_dates(from_date: date, to_date: date) -> Iterable[date]:
    d = from_date
    while d <= to_date:
        yield d
        d += timedelta(days=1)


def compute_leave_days(
    from_date,
    to_date,
    half_day: bool = False,
    half_day_date=None,
    holidays: Sequence[date] | None = None,
    include_holidays: bool = True,
) -> float:
    """Pure leave-day computation (plan §10.5 preview).

    Rules (mirror Frappe HR ``get_number_of_leave_days``):

    * A span is counted inclusively by calendar day.
    * Holidays are subtracted from the count when ``include_holidays`` is
      False *and* a ``holidays`` set is supplied.
    * ``half_day`` on a single-day span → 0.5 days.
    * ``half_day`` on a multi-day span → 0.5 for ``half_day_date`` (default
      ``from_date``), 1.0 for the rest.

    Returns 0.0 when the window is invalid (``to < from``).
    """
    fd = coerce_date(from_date)
    td = coerce_date(to_date)
    if fd is None or td is None or td < fd:
        return 0.0

    holiday_set = {coerce_date(h) for h in (holidays or []) if coerce_date(h) is not None}
    total = 0.0
    for d in _iter_dates(fd, td):
        if not include_holidays and d in holiday_set:
            continue
        total += 1.0

    if half_day and total > 0:
        if total == 1:
            total = 0.5
        else:
            total -= 0.5  # one of the days is half (half_day_date)
    return round(total, 4)


def leave_hours(leave_days: float, hours_per_day: float = 8.0) -> float:
    """Convert leave days → leave hours using the shift's hours-per-day."""
    return round((to_float(leave_days) * to_float(hours_per_day, 8.0)), 4)


# --------------------------------------------------------------------------- #
# Balance impact + warnings
# --------------------------------------------------------------------------- #
def balance_after(balance_before: float, leave_days: float, is_lwp: bool = False) -> float:
    """Remaining balance after taking ``leave_days``.

    Leave Without Pay (``is_lwp``) does NOT consume balance (it docks pay,
    not entitlement) — mirroring Frappe's Leave Type ``is_lwp`` flag.
    """
    if is_lwp:
        return round(to_float(balance_before), 4)
    return round(to_float(balance_before) - to_float(leave_days), 4)


def build_warnings(
    leave_days: float,
    balance_before: float,
    is_lwp: bool = False,
    employee_name: str | None = None,
) -> list[str]:
    """Human-readable warnings for the preview pane."""
    warnings: list[str] = []
    if is_lwp:
        warnings.append("Loại nghỉ này là nghỉ không lương (không trừ phép).")
    if not is_lwp and leave_days > 0:
        after = balance_after(balance_before, leave_days, is_lwp)
        if after < 0:
            warnings.append("Số ngày xin vượt số phép còn lại — đơn có thể bị từ chối.")
    return warnings


def is_blocker(leave_days: float, balance_before: float, is_lwp: bool = False) -> bool:
    """Whether the request must be blocked (negative non-LWP balance)."""
    if is_lwp:
        return False
    return balance_after(balance_before, leave_days, is_lwp) < 0


# --------------------------------------------------------------------------- #
# Preview payload composer
# --------------------------------------------------------------------------- #
def build_preview(
    from_date,
    to_date,
    balance_before: float,
    *,
    half_day: bool = False,
    half_day_date=None,
    holidays: Sequence[date] | None = None,
    include_holidays: bool = True,
    hours_per_day: float = 8.0,
    is_lwp: bool = False,
) -> dict:
    """Compose the ``preview_leave`` payload (plan §10.5 / FE contract).

    Shape (1:1 with ``hr-ui/src/api/index.js`` ``previewLeave``):
    ``{ leave_hours, leave_days, balance_before, balance_after,
        balance_impact, warnings: [...], is_blocker }``
    """
    days = compute_leave_days(
        from_date,
        to_date,
        half_day=half_day,
        half_day_date=half_day_date,
        holidays=holidays,
        include_holidays=include_holidays,
    )
    before = to_float(balance_before)
    after = balance_after(before, days, is_lwp)
    return {
        "leave_days": days,
        "leave_hours": leave_hours(days, hours_per_day),
        "balance_before": round(before, 4),
        "balance_after": after,
        "balance_impact": round(before - after, 4),
        "warnings": build_warnings(days, before, is_lwp),
        "is_blocker": is_blocker(days, before, is_lwp),
    }


# --------------------------------------------------------------------------- #
# Leave-cancellation-request helpers (doctype-design §28 / plan §11.5)
# --------------------------------------------------------------------------- #
# A Leave Application may be cancelled outright by its owner only while it is
# still Open/Draft (``cancel_draft_or_pending``). Once it reaches ``Approved``
# the employee must file a ``VN Leave Cancellation Request`` which routes
# through the matrix-driven approval inbox — HR then approves (cancelling the
# linked leave) or rejects it.
CANCELLABLE_LEAVE_STATES = frozenset({"Approved", "Open"})


def can_request_cancellation(status: str | None) -> bool:
    """Whether a Leave Application in ``status`` may be filed for cancellation.

    ``Approved``/``Open`` ⇒ yes; ``Draft``/``Rejected``/``Cancelled`` ⇒ no
    (Draft/Open should use ``cancel_draft_or_pending`` instead).
    """
    return bool(status) and status in CANCELLABLE_LEAVE_STATES


def cancellation_request_payload(
    leave_application: str,
    *,
    employee: str | None = None,
    employee_name: str | None = None,
    work_date=None,
    shift_instance: str | None = None,
    reason: str | None = None,
    requested_by: str | None = None,
) -> dict:
    """Assemble the field dict for a new ``VN Leave Cancellation Request``.

    Pure (no Frappe): ``api/leave.request_cancellation`` adds the audit
    timestamps at insert-time and lets the controller's ``before_insert``/
    ``validate`` derive any missing employee/work_date from the linked leave.
    """
    if not leave_application:
        raise ValueError("leave_application is required")
    payload: dict = {
        "leave_application": leave_application,
        "workflow_state": "Draft",
        "reason": (reason or "").strip(),
    }
    if employee:
        payload["employee"] = employee
    if employee_name:
        payload["employee_name"] = employee_name
    if work_date:
        payload["work_date"] = coerce_date(work_date)
    if shift_instance:
        payload["shift_instance"] = shift_instance
    if requested_by:
        payload["requested_by"] = requested_by
    return payload


# Columns surfaced to the SPA for a cancellation-request row. Keep this in sync
# with the FE ``requestLeaveCancellation``/``fetchMyLeaveCancellations`` shape.
#
# ``department`` is not a real column on the cancellation DocType — it is
# resolved per employee (one batch query) by ``api/leave._list_cancellation_requests``
# and attached here so HR can see each requester's team on the inbox row (not
# only in the filter select).
_CANCELLATION_ROW_FIELDS = (
    "name",
    "leave_application",
    "employee",
    "employee_name",
    "department",
    "work_date",
    "shift_instance",
    "reason",
    "workflow_state",
    "requested_by",
    "requested_at",
    "approved_by",
    "approved_at",
    "rejection_reason",
    "attendance_recalculated",
    "affected_attendance",
    "affected_work_session",
)


def cancellation_row(doc: dict) -> dict:
    """Normalise a raw DB/doc row into the SPA-facing cancellation shape.

    Missing keys are filled with safe defaults (``""`` / ``None``) so the FE
    never trips over an absent column. ``workflow_state`` defaults to ``Draft``.
    """
    if not isinstance(doc, dict):
        return {}
    row: dict = {}
    for key in _CANCELLATION_ROW_FIELDS:
        if key == "workflow_state":
            row[key] = doc.get(key) or "Draft"
        else:
            row[key] = doc.get(key)
    return row


# --------------------------------------------------------------------------- #
# Leave-approval queue helpers (plan §11.5 / doctype-design A.6 blackout surfacing)
# --------------------------------------------------------------------------- #
def _truthy(value) -> bool:
    """Coerce a Frappe Check-ish value (0/1/"0"/"1"/True/False/None) to bool."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def is_blackout_flagged(row: dict) -> bool:
    """Whether a Leave Application row overlapped a Block/Require-HR-Approval
    blackout window (stamped at apply time). Pure, None-safe."""
    return isinstance(row, dict) and _truthy(row.get("vn_requires_blackout_approval"))


def sort_pending_approvals(rows: list) -> list:
    """Order HR leave-approval rows so blackout-flagged applications surface
    first, then by ``posting_date`` descending (newest request on top) with
    a stable tiebreak on ``name``.

    Pure (no Frappe): ``api/leave.pending_leave_approvals`` loads the rows from
    the DB and calls this so HR sees the highest-attention items at the top of
    the inbox. Non-dict entries are dropped; missing ``posting_date`` sorts as
    an empty string so a malformed row never crashes the sort.

    Implemented as successive stable passes (least significant key first) so
    the three keys can have independent directions within one deterministic
    ordering: flagged-first, then posting desc, then name desc.
    """
    items = [r for r in (rows or []) if isinstance(r, dict)]
    # 1. name desc (tiebreak) — least significant, applied first.
    items.sort(key=lambda r: str(r.get("name") or ""), reverse=True)
    # 2. posting_date desc — stable, keeps name ordering within equal postings.
    items.sort(key=lambda r: str(r.get("posting_date") or ""), reverse=True)
    # 3. flagged-first — stable; blackout rows bubble above non-blackout rows
    #    while preserving the posting/name order within each group.
    items.sort(key=lambda r: not is_blackout_flagged(r))
    return items


def normalize_name_list(names) -> list:
    """Coerce a bulk-action ``names`` payload into a de-duplicated list of
    non-empty name strings.

    Accepts the shapes the SPA / Frappe whitelist may hand a bulk endpoint:
    ``None`` → ``[]``; a JSON string (``'["A","B"]'``) → parsed list; a
    comma-separated string (``"A,B"``) → split list; a list/tuple/set → as-is.
    Whitespace is stripped, blanks dropped, order of first occurrence kept.

    Pure (no Frappe) so the bulk approve/reject endpoints can normalise input
    deterministically and the helper is unit-tested outside the bench.
    """
    if names is None:
        return []
    if isinstance(names, str):
        text = names.strip()
        if not text:
            return []
        # JSON array shape (what the SPA sends as JSON body).
        if text.startswith("["):
            import json

            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
            if isinstance(parsed, (list, tuple)):
                names = parsed
            else:
                names = [text]
        else:
            names = [p for p in text.split(",")]
    if isinstance(names, (list, tuple, set)):
        seen: list[str] = []
        cache: set[str] = set()
        for item in names:
            value = "" if item is None else str(item).strip()
            if not value or value in cache:
                continue
            cache.add(value)
            seen.append(value)
        return seen
    # A single scalar (e.g. one name) → wrap it.
    value = str(names).strip()
    return [value] if value else []


def merge_bulk_results(succeeded, failed) -> dict:
    """Assemble a bulk-action summary payload (pure).

    ``succeeded`` / ``failed`` are lists of names / ``{name, error}`` dicts
    accumulated while looping per-document. Returns ``{total, succeeded,
    failed, counts}`` so the SPA can render a single toast after a bulk run.
    """
    ok = [n for n in (succeeded or []) if n]
    errs = [e for e in (failed or []) if isinstance(e, dict) and e.get("name")]
    return {
        "total": len(ok) + len(errs),
        "succeeded": ok,
        "failed": errs,
        "counts": {"succeeded": len(ok), "failed": len(errs)},
    }
