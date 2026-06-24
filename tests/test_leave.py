"""Unit tests for leave-day math + balance impact (plan v5 §10.5).

All tests are bench-free: the pure helpers live in
``gege_hr.gege_hr.utils.leave`` and never import ``frappe`` at module scope.
"""

from datetime import date

import pytest

from gege_hr.gege_hr.utils import leave as lv


# --------------------------------------------------------------------------- #
# Coercion
# --------------------------------------------------------------------------- #
def test_to_date_from_date():
    assert lv.coerce_date(date(2026, 6, 21)) == date(2026, 6, 21)


def test_to_date_from_iso_string():
    assert lv.coerce_date("2026-06-21") == date(2026, 6, 21)


def test_to_date_from_datetime_string():
    assert lv.coerce_date("2026-06-21 14:30:00") == date(2026, 6, 21)


def test_to_date_blank_and_invalid():
    assert lv.coerce_date(None) is None
    assert lv.coerce_date("") is None
    assert lv.coerce_date("not-a-date") is None


def test_to_float_defaults_and_invalid():
    assert lv.to_float(None) == 0.0
    assert lv.to_float("") == 0.0
    assert lv.to_float("abc") == 0.0
    assert lv.to_float("3.5") == 3.5
    assert lv.to_float(7) == 7.0


def test_to_bool_variants():
    assert lv.to_bool(True) is True
    assert lv.to_bool(1) is True
    assert lv.to_bool("1") is True
    assert lv.to_bool("true") is True
    assert lv.to_bool("yes") is True
    assert lv.to_bool(False) is False
    assert lv.to_bool(0) is False
    assert lv.to_bool("0") is False
    assert lv.to_bool("") is False


# --------------------------------------------------------------------------- #
# inclusive_day_count
# --------------------------------------------------------------------------- #
def test_inclusive_day_count_single():
    assert lv.inclusive_day_count(date(2026, 6, 21), date(2026, 6, 21)) == 1


def test_inclusive_day_count_range():
    assert lv.inclusive_day_count(date(2026, 6, 21), date(2026, 6, 25)) == 5


def test_inclusive_day_count_reversed_is_zero():
    assert lv.inclusive_day_count(date(2026, 6, 25), date(2026, 6, 21)) == 0


# --------------------------------------------------------------------------- #
# compute_leave_days
# --------------------------------------------------------------------------- #
def test_leave_days_single_day():
    assert lv.compute_leave_days(date(2026, 6, 21), date(2026, 6, 21)) == 1.0


def test_leave_days_multi_day():
    assert lv.compute_leave_days(date(2026, 6, 21), date(2026, 6, 23)) == 3.0


def test_leave_days_single_half_day():
    assert lv.compute_leave_days(date(2026, 6, 21), date(2026, 6, 21), half_day=True) == 0.5


def test_leave_days_multi_day_half():
    # 3-day span with half_day → 2.5 (one half day).
    assert lv.compute_leave_days(date(2026, 6, 21), date(2026, 6, 23), half_day=True) == 2.5


def test_leave_days_excludes_holidays_when_flagged():
    holidays = [date(2026, 6, 22)]  # Tuesday inside the span
    days = lv.compute_leave_days(
        date(2026, 6, 21),
        date(2026, 6, 23),
        holidays=holidays,
        include_holidays=False,
    )
    assert days == 2.0  # one holiday dropped


def test_leave_days_includes_holidays_by_default():
    holidays = [date(2026, 6, 22)]
    days = lv.compute_leave_days(date(2026, 6, 21), date(2026, 6, 23), holidays=holidays)
    assert days == 3.0


def test_leave_days_invalid_window_is_zero():
    assert lv.compute_leave_days(date(2026, 6, 25), date(2026, 6, 21)) == 0.0


def test_leave_days_accepts_strings():
    assert lv.compute_leave_days("2026-06-21", "2026-06-22") == 2.0


# --------------------------------------------------------------------------- #
# leave_hours
# --------------------------------------------------------------------------- #
def test_leave_hours_default_per_day():
    assert lv.leave_hours(2.0) == 16.0


def test_leave_hours_custom_per_day():
    assert lv.leave_hours(1.5, hours_per_day=12.0) == 18.0


def test_leave_hours_zero_days():
    assert lv.leave_hours(0.0, hours_per_day=8.0) == 0.0


# --------------------------------------------------------------------------- #
# balance_after / build_warnings / is_blocker
# --------------------------------------------------------------------------- #
def test_balance_after_subtracts():
    assert lv.balance_after(10.0, 3.0) == 7.0


def test_balance_after_lwp_does_not_consume():
    assert lv.balance_after(10.0, 3.0, is_lwp=True) == 10.0


def test_balance_after_can_go_negative():
    assert lv.balance_after(2.0, 5.0) == -3.0


def test_warnings_lwp_message():
    w = lv.build_warnings(2.0, 10.0, is_lwp=True)
    assert any("không lương" in m for m in w)


def test_warnings_negative_balance():
    w = lv.build_warnings(5.0, 2.0, is_lwp=False)
    assert any("vượt" in m or "từ chối" in m for m in w)


def test_warnings_no_warning_when_healthy():
    assert lv.build_warnings(2.0, 10.0, is_lwp=False) == []


def test_is_blocker_true_when_negative():
    assert lv.is_blocker(5.0, 2.0) is True


def test_is_blocker_false_lwp():
    assert lv.is_blocker(5.0, 2.0, is_lwp=True) is False


def test_is_blocker_false_healthy():
    assert lv.is_blocker(2.0, 10.0) is False


# --------------------------------------------------------------------------- #
# build_preview (composer)
# --------------------------------------------------------------------------- #
def test_build_preview_full_shape():
    res = lv.build_preview(
        date(2026, 6, 21),
        date(2026, 6, 22),
        balance_before=10.0,
        hours_per_day=8.0,
    )
    assert res["leave_days"] == 2.0
    assert res["leave_hours"] == 16.0
    assert res["balance_before"] == 10.0
    assert res["balance_after"] == 8.0
    assert res["balance_impact"] == 2.0
    assert res["warnings"] == []
    assert res["is_blocker"] is False


def test_build_preview_half_day():
    res = lv.build_preview(
        date(2026, 6, 21),
        date(2026, 6, 21),
        balance_before=10.0,
        half_day=True,
    )
    assert res["leave_days"] == 0.5
    assert res["balance_impact"] == 0.5
    assert res["balance_after"] == 9.5


def test_build_preview_lwp_no_balance_impact():
    res = lv.build_preview(
        date(2026, 6, 21),
        date(2026, 6, 23),
        balance_before=1.0,
        is_lwp=True,
    )
    assert res["leave_days"] == 3.0
    assert res["balance_impact"] == 0.0
    assert res["balance_after"] == 1.0
    assert res["is_blocker"] is False
    assert res["warnings"]  # LWP warning present


def test_build_preview_blocker_when_negative():
    res = lv.build_preview(
        date(2026, 6, 21),
        date(2026, 6, 25),
        balance_before=1.0,  # 5 days requested
    )
    assert res["balance_after"] == -4.0
    assert res["is_blocker"] is True
    assert res["warnings"]


def test_build_preview_invalid_window_zero_days():
    res = lv.build_preview(date(2026, 6, 25), date(2026, 6, 21), balance_before=5.0)
    assert res["leave_days"] == 0.0
    assert res["balance_after"] == 5.0
    assert res["is_blocker"] is False


# --------------------------------------------------------------------------- #
# Leave-cancellation-request helpers (doctype-design §28)
# --------------------------------------------------------------------------- #
def test_can_request_cancellation_allowed_states():
    assert lv.can_request_cancellation("Approved") is True
    assert lv.can_request_cancellation("Open") is True


def test_can_request_cancellation_disallowed_states():
    assert lv.can_request_cancellation("Draft") is False
    assert lv.can_request_cancellation("Rejected") is False
    assert lv.can_request_cancellation("Cancelled") is False
    assert lv.can_request_cancellation(None) is False
    assert lv.can_request_cancellation("") is False


def test_cancellation_request_payload_full():
    payload = lv.cancellation_request_payload(
        "HR-LAP-0001",
        employee="HR-EMP-0001",
        employee_name="Nguyen Van A",
        work_date="2026-06-21",
        shift_instance="SI-001",
        reason="  Sai ngày đặt phép  ",
        requested_by="a@example.com",
    )
    assert payload["leave_application"] == "HR-LAP-0001"
    assert payload["workflow_state"] == "Draft"
    assert payload["employee"] == "HR-EMP-0001"
    assert payload["employee_name"] == "Nguyen Van A"
    assert payload["work_date"] == date(2026, 6, 21)
    assert payload["shift_instance"] == "SI-001"
    assert payload["reason"] == "Sai ngày đặt phép"  # stripped
    assert payload["requested_by"] == "a@example.com"


def test_cancellation_request_payload_minimal_strips_reason():
    payload = lv.cancellation_request_payload("HR-LAP-0001", reason="  ")
    assert payload["leave_application"] == "HR-LAP-0001"
    assert payload["reason"] == ""
    assert "employee" not in payload
    assert "work_date" not in payload


def test_cancellation_request_payload_requires_leave():
    with pytest.raises(ValueError):
        lv.cancellation_request_payload(None)


def test_cancellation_row_normalises_and_defaults():
    row = lv.cancellation_row(
        {
            "name": "CR-001",
            "leave_application": "HR-LAP-0001",
            "employee": "HR-EMP-0001",
            "employee_name": "Nguyen Van A",
            "reason": "Sai ngày",
            # workflow_state intentionally missing → defaults to Draft
        }
    )
    assert row["name"] == "CR-001"
    assert row["workflow_state"] == "Draft"
    # all known columns present even when absent in source
    assert row["approved_by"] is None
    assert row["affected_attendance"] is None
    assert row["attendance_recalculated"] is None
    # department is resolved/stamped by the api layer; absent in source → None
    assert row["department"] is None


def test_cancellation_row_preserves_department():
    row = lv.cancellation_row({"name": "CR-003", "employee": "E1", "department": "Eng"})
    assert row["department"] == "Eng"


def test_cancellation_row_preserves_state_and_extra_keys_dropped():
    row = lv.cancellation_row(
        {
            "name": "CR-002",
            "workflow_state": "Approved",
            "approved_by": "hr@example.com",
            "attendance_recalculated": 1,
            "rogue_key": "should-not-leak",
        }
    )
    assert row["workflow_state"] == "Approved"
    assert row["approved_by"] == "hr@example.com"
    assert row["attendance_recalculated"] == 1
    assert "rogue_key" not in row


def test_cancellation_row_non_dict_returns_empty():
    assert lv.cancellation_row(None) == {}
    assert lv.cancellation_row("CR-001") == {}


def test_cancellation_row_fields_contract_complete():
    # ensure the row exposes every field the SPA cancellation card needs
    base = {k: k for k in lv._CANCELLATION_ROW_FIELDS}
    row = lv.cancellation_row(base)
    assert set(row.keys()) == set(lv._CANCELLATION_ROW_FIELDS)
    assert "rejection_reason" in row
    assert "affected_work_session" in row


# --------------------------------------------------------------------------- #
# Leave-approval queue helpers (session 42 — blackout surfacing)
# --------------------------------------------------------------------------- #
def test_is_blackout_flagged_truthiness_variants():
    assert lv.is_blackout_flagged({"vn_requires_blackout_approval": 1}) is True
    assert lv.is_blackout_flagged({"vn_requires_blackout_approval": "1"}) is True
    assert lv.is_blackout_flagged({"vn_requires_blackout_approval": True}) is True
    assert lv.is_blackout_flagged({"vn_requires_blackout_approval": 0}) is False
    assert lv.is_blackout_flagged({"vn_requires_blackout_approval": None}) is False
    assert lv.is_blackout_flagged({}) is False
    assert lv.is_blackout_flagged(None) is False
    assert lv.is_blackout_flagged("not-a-dict") is False


def test_sort_pending_approvals_blackout_first():
    rows = [
        {"name": "A", "posting_date": "2026-06-22"},
        {"name": "B", "posting_date": "2026-06-21", "vn_requires_blackout_approval": 1},
        {"name": "C", "posting_date": "2026-06-23"},
    ]
    out = lv.sort_pending_approvals(rows)
    # Blackout-flagged B surfaces on top despite an older posting_date.
    assert [r["name"] for r in out] == ["B", "C", "A"]


def test_sort_pending_approvals_posting_desc_within_group():
    rows = [
        {"name": "old", "posting_date": "2026-06-01"},
        {"name": "new", "posting_date": "2026-06-20"},
        {"name": "mid", "posting_date": "2026-06-10"},
    ]
    out = lv.sort_pending_approvals(rows)
    assert [r["name"] for r in out] == ["new", "mid", "old"]


def test_sort_pending_approvals_blackout_group_then_posting():
    rows = [
        {"name": "blk-old", "posting_date": "2026-06-01", "vn_requires_blackout_approval": 1},
        {"name": "blk-new", "posting_date": "2026-06-20", "vn_requires_blackout_approval": 1},
        {"name": "plain-new", "posting_date": "2026-06-25"},
    ]
    out = lv.sort_pending_approvals(rows)
    # Blackout group first (newest within), then plain group.
    assert [r["name"] for r in out] == ["blk-new", "blk-old", "plain-new"]


def test_sort_pending_approvals_none_and_non_dict_safe():
    out = lv.sort_pending_approvals(None)
    assert out == []
    out = lv.sort_pending_approvals([None, "x", {"name": "A", "posting_date": None}])
    assert [r["name"] for r in out] == ["A"]


def test_sort_pending_approvals_empty():
    assert lv.sort_pending_approvals([]) == []


# --------------------------------------------------------------------------- #
# Bulk-action helpers (session 43 — HR leave-approval triage)
# --------------------------------------------------------------------------- #
def test_normalize_name_list_none_and_empty():
    assert lv.normalize_name_list(None) == []
    assert lv.normalize_name_list("") == []
    assert lv.normalize_name_list("   ") == []


def test_normalize_name_list_list_dedup_strips_keeps_order():
    assert lv.normalize_name_list(["A", " B ", "A", "", "C"]) == ["A", "B", "C"]


def test_normalize_name_list_comma_string():
    assert lv.normalize_name_list("A,B , C,A") == ["A", "B", "C"]


def test_normalize_name_list_json_array_string():
    assert lv.normalize_name_list('["HR-LAP-1", "HR-LAP-2"]') == [
        "HR-LAP-1",
        "HR-LAP-2",
    ]


def test_normalize_name_list_scalar_wrapped():
    assert lv.normalize_name_list("SOLO") == ["SOLO"]


def test_normalize_name_list_set_input():
    # Order is non-deterministic for a set, but contents are de-duplicated.
    out = lv.normalize_name_list({"A", "B"})
    assert sorted(out) == ["A", "B"]


def test_merge_bulk_results_counts():
    res = lv.merge_bulk_results(["A", "B"], [{"name": "C", "error": "x"}])
    assert res["total"] == 3
    assert res["counts"] == {"succeeded": 2, "failed": 1}
    assert res["succeeded"] == ["A", "B"]
    assert res["failed"] == [{"name": "C", "error": "x"}]


def test_merge_bulk_results_drops_blanks_and_non_dicts():
    res = lv.merge_bulk_results(["A", "", None], [None, {"name": "", "error": "y"}, {"name": "D"}])
    assert res["counts"] == {"succeeded": 1, "failed": 1}
    assert res["succeeded"] == ["A"]
    assert res["failed"] == [{"name": "D"}]


def test_merge_bulk_results_none_safe():
    assert lv.merge_bulk_results(None, None) == {
        "total": 0,
        "succeeded": [],
        "failed": [],
        "counts": {"succeeded": 0, "failed": 0},
    }
