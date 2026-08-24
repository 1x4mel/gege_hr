"""WP9 / TZ Phase-2 — one-way data migration portal-wall → true UTC.

Implements §4.2 of plans/tz-frame-unification-plan.md (as referenced by
prod-readiness-plan WP9):

    Employee Checkin.time                        -= D
    VN Attendance Work Session actual_checkin/checkout -= D
    VN Employee Shift Instance planned_* windows -= D
    VN Mobile Checkin Attempt server_timestamp   -= D
    VN Checkout Miss auto_checkout_at / grace_deadline -= D

with ``D`` = the portal timezone's UTC offset (read from the site's tz config
via :func:`gege_hr...utils.tz.get_portal_timezone`, NEVER hard-coded). Only
rows dated ``<= lock_date`` are touched; ANY row after the lock boundary
aborts the whole run (an open work period means the timing isn't safe).

Pure planning helpers at the top are bench-free and unit-tested; the ``run``
entry is a bench patch executed via:

    bench --site <site> execute gege_hr.patches.tz_phase2.run --args "['2026-08-31']"
    bench --site <site> execute gege_hr.patches.tz_phase2.run --args "['2026-08-31', True]"   # dry-run
    bench --site <site> execute gege_hr.patches.tz_phase2.rollback --args "['2026-08-31']"

A before/after count log is appended to ``logs/tz_phase2_<stamp>.json``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

# (doctype, filter_field, datetime_fields) — filter_field decides which rows
# belong to the closed period (Date OR Datetime). Fields missing from the
# DocType meta are skipped at plan time, so a partial deploy is safe.
MIGRATE_PLAN: list[tuple[str, str, list[str]]] = [
    ("Employee Checkin", "time", ["time"]),
    (
        "VN Attendance Work Session",
        "actual_checkin",
        ["actual_checkin", "actual_checkout"],
    ),
    (
        "VN Employee Shift Instance",
        "planned_start",
        [
            "planned_start",
            "planned_end",
            "first_half_start",
            "first_half_end",
            "second_half_start",
            "second_half_end",
            "checkin_window_start",
            "checkin_window_end",
            "checkout_window_start",
            "checkout_window_end",
            "max_checkout_time",
        ],
    ),
    ("VN Mobile Checkin Attempt", "server_timestamp", ["server_timestamp"]),
    ("VN Checkout Miss", "grace_deadline", ["grace_deadline", "auto_checkout_at"]),
]


# --------------------------------------------------------------------------- #
# Pure helpers (bench-free — unit tested in tests/test_tz_phase2.py)
# --------------------------------------------------------------------------- #
def utc_offset_hours(tzname: str) -> float:
    """UTC offset (hours, fractional) of a timezone — e.g. VN → 7.0."""
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo(tzname))
    return now.utcoffset().total_seconds() / 3600.0


def plan_for_meta(
    meta_fields: dict[str, str], configured: tuple[str, str, list[str]]
) -> tuple[str, str, list[str]]:
    """Trim a configured plan entry to the fields that actually exist as
    Datetime columns on the DocType. ``meta_fields`` maps fieldname →
    fieldtype. The filter field is kept only when present too."""
    doctype, filter_field, fields = configured
    if filter_field not in meta_fields:
        return (doctype, filter_field, [])
    keep = [f for f in fields if meta_fields.get(f) == "Datetime"]
    return (doctype, filter_field, keep)


def build_update_sql(table: str, field: str, hours: float, sign: int, filter_field: str) -> tuple[str, list]:
    """Parameterised UPDATE shifting ``field`` by ``sign * hours``.

    Uses ``ADDDATE(..., INTERVAL ? HOUR)`` with a bind so the offset comes
    from the tz config, never string-interpolated. ``sign=-1`` for the
    wall→UTC migration, ``+1`` for the rollback.
    """
    sql = (
        f"UPDATE `tab{table}` SET `{field}` = "
        f"ADDDATE(`{field}`, INTERVAL %s HOUR) "
        f"WHERE `{filter_field}` IS NOT NULL AND `{filter_field}` != '' "
        f"AND `{filter_field}` <= %s"
    )
    return sql, [sign * float(hours), "{lock_date}"]


def shift_dt(value: datetime, hours: float, sign: int) -> datetime:
    """Pure datetime shift used to verify ±0-second round trips in tests."""
    return value + timedelta(hours=sign * float(hours))


# --------------------------------------------------------------------------- #
# Bench entry
# --------------------------------------------------------------------------- #
def _meta_fields(doctype: str) -> dict[str, str]:
    meta = __import__("frappe").get_meta(doctype)
    return {df.fieldname: df.fieldtype for df in meta.fields}


def _write_log(payload: dict) -> None:
    import frappe

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = f"{frappe.utils.get_bench_path()}/logs/tz_phase2_{stamp}.json"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)
    return path


def run(lock_date: str, dry_run: bool = False) -> dict:
    """Migrate every closed-period datetime from portal-wall to UTC (−D)."""
    return _run_signed(lock_date, dry_run=dry_run, rollback=False)


def rollback(lock_date: str, dry_run: bool = False) -> dict:
    """Reverse of :func:`run` (+D) — restore wall frame from a botched flip."""
    return _run_signed(lock_date, dry_run=dry_run, rollback=True)


def _run_signed(lock_date: str, dry_run: bool, rollback: bool) -> dict:
    import frappe

    from gege_hr.gege_hr.utils import tz as tz_utils

    sign = 1 if rollback else -1
    direction = "rollback utc→wall" if rollback else "migrate wall→utc"
    hours = utc_offset_hours(tz_utils.get_portal_timezone())
    if hours == 0:
        frappe.throw("Portal timezone is already UTC — nothing to migrate.")

    summary: dict = {"direction": direction, "offset_hours": hours, "lock_date": lock_date, "tables": {}}

    # Plan first — abort on ANY post-lock row BEFORE touching anything.
    plans = []
    for configured in MIGRATE_PLAN:
        doctype, filter_field, fields = plan_for_meta(_meta_fields(configured[0]), configured)
        if not fields:
            summary["tables"][doctype] = {"skipped": "no datetime fields on meta"}
            continue
        table = f"tab{doctype}"
        future = frappe.db.sql(
            f"SELECT COUNT(*) AS n FROM `{table}` "
            f"WHERE `{filter_field}` IS NOT NULL AND `{filter_field}` != '' "
            f"AND `{filter_field}` > %s",
            (lock_date,),
            as_dict=True,
        )[0]["n"]
        if future:
            frappe.throw(
                f"{doctype}: {future} row(s) sau lock_date {lock_date} — chốt kỳ/đóng phiên "
                "trước khi migrate (plans/tz-frame-unification-plan.md §4.1)."
            )
        before = frappe.db.sql(
            f"SELECT COUNT(*) AS n FROM `{table}` "
            f"WHERE `{filter_field}` IS NOT NULL AND `{filter_field}` != '' "
            f"AND `{filter_field}` <= %s",
            (lock_date,),
            as_dict=True,
        )[0]["n"]
        plans.append((doctype, table, filter_field, fields, before))

    # Execute inside one transaction: any error rolls the WHOLE shift back.
    try:
        for doctype, table, filter_field, fields, before in plans:
            touched = {}
            for field in fields:
                sql, params = build_update_sql(table, field, hours, sign, filter_field)
                bound = [params[0], lock_date]
                if dry_run:
                    touched[field] = "dry-run"
                    continue
                changed = frappe.db.sql(sql, tuple(bound))
                touched[field] = changed.rowcount if hasattr(changed, "rowcount") else before
            summary["tables"][doctype] = {"rows_in_window": before, "fields": touched}
        if not dry_run:
            frappe.db.commit()
        summary["ok"] = True
        summary["dry_run"] = bool(dry_run)
        summary["log"] = _write_log(summary)
        return summary
    except Exception:
        try:
            frappe.db.rollback()
        except Exception:
            pass
        raise
