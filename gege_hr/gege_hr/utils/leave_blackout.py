"""
Leave blackout period helpers — plan v5 §11.4 / doctype-design §31.

A **VN Leave Blackout Period** marks a date window during which leave of a
given type (or all types) is restricted: ``Warning`` (allow but flag),
``Block`` (hard reject) or ``Require HR Approval`` (route to HR). The leave
preview/apply flow consults :func:`evaluate_blackout` to decide whether a
requested leave window is actionable.

Design (mirrors ``utils/leave.py``): pure, bench-free helpers + an
``evaluate_blackout`` decision function the api layer calls against DB rows.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime
from typing import Any

try:
    import frappe  # type: ignore
except Exception:  # pragma: no cover - outside bench
    frappe = None


# --------------------------------------------------------------------------- #
# Vocabulary (matches VN Leave Blackout Period.action options)
# --------------------------------------------------------------------------- #
BLACKOUT_ACTIONS = ("Warning", "Block", "Require HR Approval")

# Strongest → weakest precedence (Block wins over Require HR Approval wins over Warning).
_ACTION_RANK = {"Block": 3, "Require HR Approval": 2, "Warning": 1}


# --------------------------------------------------------------------------- #
# Date helpers
# --------------------------------------------------------------------------- #
def _coerce_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    if "T" in text or " " in text:
        text = text.replace("T", " ").split(" ")[0]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def is_valid_date_range(from_date: Any, to_date: Any) -> bool:
    """True when both dates resolve and ``from_date <= to_date``."""
    frm = _coerce_date(from_date)
    to = _coerce_date(to_date)
    if frm is None or to is None:
        return False
    return frm <= to


def date_overlaps(a_from: Any, a_to: Any, b_from: Any, b_to: Any) -> bool:
    """Inclusive overlap test between two ``[from, to]`` date windows."""
    af, at = _coerce_date(a_from), _coerce_date(a_to)
    bf, bt = _coerce_date(b_from), _coerce_date(b_to)
    if None in (af, at, bf, bt):
        return False
    return af <= bt and bf <= at


def window_days(from_date: Any, to_date: Any) -> list[date]:
    """Inclusive list of dates spanned by a ``[from, to]`` window."""
    frm = _coerce_date(from_date)
    to = _coerce_date(to_date)
    if frm is None or to is None or frm > to:
        return []
    from datetime import timedelta

    days: list[date] = []
    cur = frm
    while cur <= to:
        days.append(cur)
        cur += timedelta(days=1)
    return days


# --------------------------------------------------------------------------- #
# Payload / row shapers
# --------------------------------------------------------------------------- #
def blackout_payload(
    *,
    blackout_name: str,
    company: str,
    from_date: Any,
    to_date: Any,
    reason: str,
    branch: str | None = None,
    department: str | None = None,
    applies_to_leave_type: str | None = None,
    is_active: bool = True,
    action: str = "Warning",
) -> dict:
    """Assemble the field dict for a new VN Leave Blackout Period.

    Coerces dates, defaults ``action``/``is_active``, strips blanks, and
    validates the required fields + date order. Raises ``ValueError`` on bad
    input (the api layer maps that to ``frappe.throw``).
    """
    if not (blackout_name or "").strip():
        raise ValueError("blackout_name is required")
    if not company:
        raise ValueError("company is required")
    if not (reason or "").strip():
        raise ValueError("reason is required")
    if not is_valid_date_range(from_date, to_date):
        raise ValueError("from_date must be on/before to_date")
    if action not in BLACKOUT_ACTIONS:
        action = "Warning"
    frm = _coerce_date(from_date)
    to = _coerce_date(to_date)
    doc: dict[str, Any] = {
        "doctype": "VN Leave Blackout Period",
        "blackout_name": str(blackout_name).strip(),
        "company": str(company).strip(),
        "from_date": frm.isoformat() if frm else None,
        "to_date": to.isoformat() if to else None,
        "reason": str(reason).strip(),
        "action": action,
        "is_active": 1 if is_active else 0,
    }
    if branch:
        doc["branch"] = str(branch).strip()
    if department:
        doc["department"] = str(department).strip()
    if applies_to_leave_type:
        doc["applies_to_leave_type"] = str(applies_to_leave_type).strip()
    return doc


BLACKOUT_ROW_FIELDS = (
    "name",
    "blackout_name",
    "company",
    "branch",
    "department",
    "from_date",
    "to_date",
    "applies_to_leave_type",
    "is_active",
    "action",
    "reason",
    "modified",
    "modified_by",
    "owner",
)


def blackout_row(row: Any) -> dict:
    """Normalise a DB row / dict into the SPA blackout shape."""
    if not isinstance(row, dict):
        return {}
    out: dict[str, Any] = {key: row.get(key) for key in BLACKOUT_ROW_FIELDS}
    # Normalise is_active → bool.
    out["is_active"] = bool(out.get("is_active"))
    for key in ("from_date", "to_date"):
        value = out.get(key)
        if isinstance(value, (datetime, date)):
            out[key] = (
                value.isoformat()
                if isinstance(value, date) and not isinstance(value, datetime)
                else value.date().isoformat()
            )
    return out


# --------------------------------------------------------------------------- #
# Overlap guard (plan blackout desk-free §B7) — warn before minting a rule
# that duplicates an existing active restriction window.
# --------------------------------------------------------------------------- #
def overlapping_rules(
    rules: Iterable[dict],
    *,
    from_date: Any,
    to_date: Any,
    branch: str | None = None,
    department: str | None = None,
    leave_type: str | None = None,
    exclude: str | None = None,
) -> list[dict]:
    """Active same-scope rules whose window overlaps ``[from_date, to_date]``.

    Scope-match mirrors the runtime precedence in :func:`_rule_matches`: a rule
    with a blank ``branch`` / ``department`` / ``applies_to_leave_type`` is the
    *generic* one and covers every value, so it overlaps any same-window
    request; a rule scoped narrower only overlaps an equal-or-broader request.
    ``exclude`` drops the rule currently being edited so it never flags
    itself. Pure / bench-free.
    """
    out: list[dict] = []
    for rule in rules or ():
        if not isinstance(rule, dict):
            continue
        if not rule.get("is_active", True):
            continue
        if exclude and rule.get("name") == exclude:
            continue
        rule_branch = str(rule.get("branch") or "").strip()
        rule_department = str(rule.get("department") or "").strip()
        rule_leave_type = str(rule.get("applies_to_leave_type") or "").strip()
        if rule_branch and branch and rule_branch != str(branch).strip():
            continue
        if rule_department and department and rule_department != str(department).strip():
            continue
        if rule_leave_type and leave_type and rule_leave_type != str(leave_type).strip():
            continue
        if not date_overlaps(rule.get("from_date"), rule.get("to_date"), from_date, to_date):
            continue
        out.append(blackout_row(rule))
    return out


# --------------------------------------------------------------------------- #
# CSV export serialiser (plan blackout desk-free §B6) — Excel-safe UTF-8 BOM.
# --------------------------------------------------------------------------- #
BLACKOUT_CSV_HEADERS = (
    "Tên kỳ cấm",
    "Công ty",
    "Chi nhánh",
    "Phòng ban",
    "Từ ngày",
    "Đến ngày",
    "Loại nghỉ",
    "Hành động",
    "Đang áp dụng",
    "Lý do",
    "Người tạo",
    "Sửa lần cuối",
)


def build_blackout_csv(rows: Iterable[dict]) -> str:
    """Serialise normalised blackout rows to CSV with a UTF-8 BOM.

    Parity ``utils/audit.build_audit_csv`` — the BOM keeps Excel from mangling
    the Vietnamese headers. Pure / bench-free.
    """
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(BLACKOUT_CSV_HEADERS)
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        writer.writerow(
            [
                row.get("blackout_name"),
                row.get("company"),
                row.get("branch"),
                row.get("department"),
                row.get("from_date"),
                row.get("to_date"),
                row.get("applies_to_leave_type"),
                row.get("action"),
                "Có" if row.get("is_active") else "Không",
                row.get("reason"),
                row.get("owner"),
                row.get("modified_by"),
            ]
        )
    return "\ufeff" + buf.getvalue()


# --------------------------------------------------------------------------- #
# Decision function — the heart of the leave/blackout integration
# --------------------------------------------------------------------------- #
def _rule_matches(
    rule: dict,
    *,
    target_date: date,
    leave_type: str | None,
) -> bool:
    """Whether a blackout rule applies to ``target_date`` for ``leave_type``."""
    if not rule.get("is_active", True):
        return False
    applies = rule.get("applies_to_leave_type")
    if applies and leave_type and applies != leave_type:
        return False
    frm = _coerce_date(rule.get("from_date"))
    to = _coerce_date(rule.get("to_date"))
    if frm is None or to is None:
        return False
    return frm <= target_date <= to


def strongest_action(actions: Iterable[str | None]) -> str | None:
    """Pick the most restrictive action among a set (None if empty)."""
    best: str | None = None
    for action in actions:
        if not action:
            continue
        if best is None or _ACTION_RANK.get(action, 0) > _ACTION_RANK.get(best, 0):
            best = action
    return best


def evaluate_blackout(
    *,
    from_date: Any,
    to_date: Any,
    leave_type: str | None = None,
    rules: Iterable[dict] = (),
) -> dict:
    """Evaluate blackout rules against a leave window.

    Returns ``{ blocked, requires_approval, warnings, matched: [...] }`` where
    ``matched`` is the list of triggering rules (normalised). A single ``Block``
    anywhere in the window makes ``blocked=True``; a ``Require HR Approval``
    sets ``requires_approval=True``; ``Warning`` only adds a warning string.
    """
    matched: list[dict] = []
    actions: list[str] = []
    days = window_days(from_date, to_date)
    if not days:
        return {"blocked": False, "requires_approval": False, "warnings": [], "matched": []}
    for rule in rules or ():
        if not isinstance(rule, dict):
            continue
        hit = any(_rule_matches(rule, target_date=d, leave_type=leave_type) for d in days)
        if not hit:
            continue
        action = rule.get("action") or "Warning"
        actions.append(action)
        matched.append(blackout_row(rule))
    blocked = "Block" in actions
    requires_approval = "Require HR Approval" in actions
    warnings: list[str] = []
    if blocked:
        warnings.append("Khoảng thời gian này nằm trong kỳ cấm nghỉ (Block).")
    if requires_approval:
        warnings.append("Khoảng thời gian này yêu cầu HR phê duyệt đặc biệt.")
    if "Warning" in actions:
        warnings.append("Khoảng thời gian này có cảnh báo nghỉ phép hạn chế.")
    return {
        "blocked": blocked,
        "requires_approval": requires_approval,
        "warnings": warnings,
        "matched": matched,
    }


def blackout_decision_fields(decision: Any) -> dict:
    """Map a blackout ``decision`` dict → the VN custom-field values to stamp on
    a Leave Application at apply time.

    Returns ``{"vn_requires_blackout_approval": 1, "vn_blackout_decision": <action>}``
    when the decision flagged ``requires_approval`` or ``blocked`` (the two cases
    where HR needs to know the leave overlapped a restriction window). For a
    plain ``Warning`` decision — or no match at all — returns ``{}`` (nothing to
    stamp), so ordinary leave applications stay clean. Pure / bench-free.
    """
    if not isinstance(decision, dict):
        return {}
    blocked = bool(decision.get("blocked"))
    requires = bool(decision.get("requires_approval"))
    if not blocked and not requires:
        return {}
    matched = decision.get("matched") or []
    actions: list[str | None] = []
    if isinstance(matched, list):
        actions = [r.get("action") for r in matched if isinstance(r, dict)]
    action = strongest_action(actions) or ("Block" if blocked else "Require HR Approval")
    return {
        "vn_requires_blackout_approval": 1,
        "vn_blackout_decision": action,
    }
