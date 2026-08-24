"""Pure check-in parity helpers — bench-free, stdlib-only (DNA §6.6 A style).

Extracted from ``api/attendance.py`` so the IN/OUT decision logic that guards
payroll integrity is unit-testable without a bench:

  - :func:`parse_log_dt`     — normalise raw ``Employee Checkin.time`` values
                               (naive-UTC datetime | ISO/SQL string | aware dt)
                               to comparable naive-UTC datetimes.
  - :func:`decide_log_type`  — overnight-safe session parity: what log type
                               (IN/OUT) the employee's next tap must create.
  - :func:`is_duplicate_intent` — at-least-once guard against lost-response
                               re-taps that would emit OUT-seconds-after-IN.
  - :func:`has_in_only` / :func:`has_out_only` — day-shape detectors (open
                               session / orphan OUT).

Fixes two payroll-corrupting scenarios (see tests/test_mobile_checkin_parity.py
for the full matrix):

  R1 Overnight parity — the day-only parity ``"OUT" if has_in_only(today) else
     "IN"`` made today's first tap after a 23:30 IN yesterday become IN, so the
     overnight shift lost its checkout (auto-closed at planned end + a bogus
     ticket). Session parity scans the NEWEST log across yesterday+today: newest
     IN → this tap is its OUT; newest OUT → new session IN.

  R2 Duplicate intent — a request may persist its log yet lose the HTTP
     response; the seconds-later re-tap used to create an OUT right after the
     IN (a 0-hour "completed" day). The guard skips taps whose intent timestamp
     is within ``gap_minutes`` of the last persisted log; offline replay passes
     because its intent timestamp is far in the past.
"""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = [
    "parse_log_dt",
    "decide_log_type",
    "is_duplicate_intent",
    "has_in_only",
    "has_out_only",
]

_IN_TOKENS = ("IN", "CLOCK IN")
_OUT_TOKENS = ("OUT", "CLOCK OUT")


def parse_log_dt(value) -> datetime | None:
    """Normalise a raw checkin ``time`` to a NAIVE UTC datetime (or ``None``).

    Frappe stores datetimes as naive UTC; the mobile client may hand an aware
    ISO string; seeded rows sometimes carry SQL strings. All are converted to
    naive-UTC so mixed sources stay comparable. Unparseable → ``None``.
    """
    if value is None:
        return None
    dt = value
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def has_in_only(checkins: list[dict]) -> bool:
    """Day shape: at least one IN and no OUT → the session is still open."""
    has_in = any(str(c.get("log_type") or "").upper() in _IN_TOKENS for c in checkins)
    has_out = any(str(c.get("log_type") or "").upper() in _OUT_TOKENS for c in checkins)
    return has_in and not has_out


def has_out_only(checkins: list[dict]) -> bool:
    """Day shape: an OUT with no IN — orphan OUT (external device / sync
    artefact). The next tap self-heals as IN, but its timestamp is the tap
    time, so the day still needs a human correction to be payable."""
    has_in = any(str(c.get("log_type") or "").upper() in _IN_TOKENS for c in checkins)
    has_out = any(str(c.get("log_type") or "").upper() in _OUT_TOKENS for c in checkins)
    return has_out and not has_in


def decide_log_type(logs: list[dict]) -> str:
    """Session-based IN/OUT parity (overnight-safe) — pure function.

    ``logs`` carries the check-in rows of YESTERDAY + TODAY (any order, any
    mix of datetimes/strings). Looking at the NEWEST parseable log:

      - newest is an (unpaired) IN → return ``"OUT"`` — this tap closes the
        open session. The overnight case: IN 23:30 yesterday, tap 00:30 today
        → OUT (the old day-only parity wrongly returned IN).
      - newest is an OUT (or no logs at all / nothing parseable) → return
        ``"IN"`` — a new session begins. Covers the empty day, the completed
        day, the auto-closed overnight session (its synthetic OUT is newest),
        and the orphan-OUT self-heal.
    """
    if not logs:
        return "IN"
    parsed = []
    for row in logs:
        t = parse_log_dt(row.get("time"))
        if t is not None:
            parsed.append((t, str(row.get("log_type") or "").upper()))
    if not parsed:
        return "IN"
    newest_type = max(parsed, key=lambda p: p[0])[1]
    if newest_type in _IN_TOKENS:
        return "OUT"
    return "IN"


def is_duplicate_intent(last_log_time, intent_time, gap_minutes: int = 2) -> bool:
    """True when this tap is a RETRY of the immediately preceding log.

    Rule: ``0 <= intent_time - last_log_time < gap_minutes`` (both normalised
    via :func:`parse_log_dt`; ``None`` on either side → not a duplicate).

    - Live re-tap after a lost HTTP response: intent ≈ last log (seconds) →
      duplicate → the caller must SKIP creating a new log.
    - Offline replay: the queued punch's intent timestamp is far OLDER than
      the last persisted log (negative delta) → not a duplicate → replay
      proceeds, preserving the FIFO offline queue semantics.
    - A genuinely new tap minutes later (>= gap) → not a duplicate.
    """
    last = parse_log_dt(last_log_time)
    intent = parse_log_dt(intent_time)
    if last is None or intent is None:
        return False
    delta = (intent - last).total_seconds()
    return 0 <= delta < int(gap_minutes) * 60
