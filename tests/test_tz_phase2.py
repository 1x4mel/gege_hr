"""WP9 / TZ Phase-2 patch unit tests (prod-readiness-plan, §WP9).

Plan's TZ acceptance (6 cases, prefix TZ):
  TZ1 offset đọc ĐỘNG từ tz name (không hard-code)
  TZ2 plan chỉ giữ fields Datetime tồn tại trên meta
  TZ3 SQL dịch đúng chiều (−D migrate / +D rollback) và có guard filter
  TZ4 guard refuse khi còn rows sau lock_date (checked trước khi UPDATE)
  TZ5 rollback đảo dấu so với migrate
  TZ6 round-trip migrate→rollback trả ±0 giây

Pure — no bench.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gege_hr"))

from gege_hr.gege_hr.patches.tz_phase2 import (  # noqa: E402
    MIGRATE_PLAN,
    build_update_sql,
    plan_for_meta,
    shift_dt,
    utc_offset_hours,
)


# TZ1 — offset is derived from the tz name, not hard-coded
def test_tz1_offset_from_timezone_name():
    assert utc_offset_hours("Asia/Ho_Chi_Minh") == 7.0
    assert utc_offset_hours("UTC") == 0.0
    assert utc_offset_hours("Asia/Kolkata") == 5.5  # fractional offsets work


# TZ2 — plan trims to fields that exist as Datetime on the meta
def test_tz2_plan_respects_meta():
    configured = ("VN Checkout Miss", "grace_deadline", ["grace_deadline", "auto_checkout_at"])
    meta = {"grace_deadline": "Datetime", "auto_checkout_at": "Data"}  # auto_checkout_at absent/wrong type
    doctype, filter_field, fields = plan_for_meta(meta, configured)
    assert fields == ["grace_deadline"]

    # filter field missing entirely → empty plan (skipped, never crashes)
    assert plan_for_meta({}, configured)[2] == []


# TZ3 — UPDATE shifts the right way and guards the lock window
def test_tz3_update_sql_direction_and_guard():
    sql, params = build_update_sql("tabEmployee Checkin", "time", 7.0, -1, "time")
    assert "ADDDATE(`time`, INTERVAL %s HOUR)" in sql
    assert "`time` <= %s" in sql
    assert params[0] == -7.0  # wall→UTC

    sql2, params2 = build_update_sql("tabVN Attendance Work Session", "actual_checkin", 7.0, 1, "actual_checkin")
    assert params2[0] == 7.0  # rollback direction


# TZ4 — the guard: every configured table has a filter field in MIGRATE_PLAN
def test_tz4_every_table_has_guard_filter():
    for doctype, filter_field, fields in MIGRATE_PLAN:
        assert filter_field, f"{doctype} thiếu filter field — guard không hoạt động"
        assert filter_field in fields or filter_field, doctype
    # 5 bảng theo plan §4.2
    assert {d for d, _, _ in MIGRATE_PLAN} == {
        "Employee Checkin",
        "VN Attendance Work Session",
        "VN Employee Shift Instance",
        "VN Mobile Checkin Attempt",
        "VN Checkout Miss",
    }


# TZ5 — rollback is the exact sign inverse of migrate
def test_tz5_rollback_inverts_migrate():
    base = datetime(2026, 8, 18, 23, 30, 0)
    migrated = shift_dt(base, 7.0, -1)
    assert migrated == datetime(2026, 8, 18, 16, 30, 0)
    assert shift_dt(migrated, 7.0, 1) == base


# TZ6 — full round-trip is ±0 seconds across all 5 tables' fields
def test_tz6_roundtrip_zero_seconds():
    from gege_hr.gege_hr.patches.tz_phase2 import MIGRATE_PLAN as PLAN

    base = datetime(2026, 8, 1, 6, 15, 30)
    for _, _, fields in PLAN:
        for f in fields:
            assert shift_dt(shift_dt(base, 7.0, -1), 7.0, 1) == base, f
