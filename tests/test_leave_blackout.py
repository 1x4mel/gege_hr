"""Bench-free unit tests for the leave blackout helpers
(``utils/leave_blackout.py``).

Covers the date helpers, payload/row shapers, and the ``evaluate_blackout``
decision function (the heart of the leave/blackout integration) — no Frappe
site needed.
"""

from datetime import date

import pytest

from gege_hr.gege_hr.utils import leave_blackout as bk


# --------------------------------------------------------------------------- #
# Date helpers
# --------------------------------------------------------------------------- #
def test_is_valid_date_range():
    assert bk.is_valid_date_range("2026-06-01", "2026-06-30") is True
    assert bk.is_valid_date_range("2026-06-30", "2026-06-01") is False
    assert bk.is_valid_date_range("2026-06-01", "2026-06-01") is True  # same day
    assert bk.is_valid_date_range(None, "2026-06-30") is False


def test_date_overlaps():
    assert bk.date_overlaps("2026-06-01", "2026-06-10", "2026-06-05", "2026-06-15") is True
    assert bk.date_overlaps("2026-06-01", "2026-06-05", "2026-06-06", "2026-06-10") is False
    assert bk.date_overlaps("2026-06-01", "2026-06-05", "2026-06-05", "2026-06-10") is True  # edge


def test_window_days_inclusive():
    days = bk.window_days("2026-06-28", "2026-07-01")
    assert days == [date(2026, 6, 28), date(2026, 6, 29), date(2026, 6, 30), date(2026, 7, 1)]
    assert bk.window_days("2026-06-05", "2026-06-01") == []


# --------------------------------------------------------------------------- #
# blackout_payload
# --------------------------------------------------------------------------- #
def test_payload_full():
    payload = bk.blackout_payload(
        blackout_name="Tết Nguyên Đán",
        company="Gege",
        from_date="2026-02-10",
        to_date="2026-02-16",
        reason="Nghỉ Tết",
        branch="HN",
        department="Sales",
        applies_to_leave_type="Casual Leave",
        action="Block",
    )
    assert payload["doctype"] == "VN Leave Blackout Period"
    assert payload["action"] == "Block"
    assert payload["is_active"] == 1
    assert payload["from_date"] == "2026-02-10"


def test_payload_invalid_action_defaults_warning():
    payload = bk.blackout_payload(
        blackout_name="B",
        company="C",
        from_date="2026-01-01",
        to_date="2026-01-02",
        reason="r",
        action="Nope",
    )
    assert payload["action"] == "Warning"


def test_payload_rejects_bad_range():
    with pytest.raises(ValueError):
        bk.blackout_payload(
            blackout_name="B",
            company="C",
            from_date="2026-01-10",
            to_date="2026-01-01",
            reason="r",
        )


def test_payload_requires_fields():
    with pytest.raises(ValueError):
        bk.blackout_payload(
            blackout_name="",
            company="C",
            from_date="2026-01-01",
            to_date="2026-01-02",
            reason="r",
        )
    with pytest.raises(ValueError):
        bk.blackout_payload(
            blackout_name="B",
            company="C",
            from_date="2026-01-01",
            to_date="2026-01-02",
            reason="",
        )


# --------------------------------------------------------------------------- #
# blackout_row
# --------------------------------------------------------------------------- #
def test_row_normalises():
    row = {
        "name": "X",
        "blackout_name": "B",
        "company": "C",
        "from_date": date(2026, 1, 1),
        "to_date": date(2026, 1, 2),
        "is_active": 1,
        "action": "Block",
        "rogue": "drop",
    }
    out = bk.blackout_row(row)
    assert out["is_active"] is True
    assert out["from_date"] == "2026-01-01"
    assert "rogue" not in out


def test_row_non_dict():
    assert bk.blackout_row(None) == {}


# --------------------------------------------------------------------------- #
# evaluate_blackout — the decision function
# --------------------------------------------------------------------------- #
def test_evaluate_no_rules():
    result = bk.evaluate_blackout(from_date="2026-06-01", to_date="2026-06-05")
    assert result == {"blocked": False, "requires_approval": False, "warnings": [], "matched": []}


def test_evaluate_no_overlap():
    rules = [
        {"from_date": "2026-07-01", "to_date": "2026-07-05", "action": "Block", "is_active": True},
    ]
    result = bk.evaluate_blackout(from_date="2026-06-01", to_date="2026-06-05", rules=rules)
    assert result["blocked"] is False
    assert result["matched"] == []


def test_evaluate_block():
    rules = [
        {
            "from_date": "2026-06-01",
            "to_date": "2026-06-30",
            "action": "Block",
            "is_active": True,
            "blackout_name": "June Freeze",
        },
    ]
    result = bk.evaluate_blackout(from_date="2026-06-10", to_date="2026-06-12", rules=rules)
    assert result["blocked"] is True
    assert result["requires_approval"] is False
    assert any("Block" in w for w in result["warnings"])
    assert len(result["matched"]) == 1


def test_evaluate_require_approval_and_warning():
    rules = [
        {
            "from_date": "2026-06-01",
            "to_date": "2026-06-30",
            "action": "Require HR Approval",
            "is_active": True,
            "applies_to_leave_type": None,
        },
        {"from_date": "2026-06-10", "to_date": "2026-06-12", "action": "Warning", "is_active": True},
    ]
    result = bk.evaluate_blackout(from_date="2026-06-10", to_date="2026-06-11", rules=rules)
    assert result["blocked"] is False
    assert result["requires_approval"] is True
    assert len(result["warnings"]) == 2


def test_evaluate_inactive_rule_ignored():
    rules = [
        {"from_date": "2026-06-01", "to_date": "2026-06-30", "action": "Block", "is_active": False},
    ]
    result = bk.evaluate_blackout(from_date="2026-06-10", to_date="2026-06-12", rules=rules)
    assert result["blocked"] is False


def test_evaluate_leave_type_filter():
    rules = [
        {
            "from_date": "2026-06-01",
            "to_date": "2026-06-30",
            "action": "Block",
            "is_active": True,
            "applies_to_leave_type": "Sick Leave",
        },
    ]
    # Different leave type → rule does not apply.
    result = bk.evaluate_blackout(
        from_date="2026-06-10", to_date="2026-06-12", leave_type="Casual Leave", rules=rules
    )
    assert result["blocked"] is False
    # Matching leave type → applies.
    result = bk.evaluate_blackout(
        from_date="2026-06-10", to_date="2026-06-12", leave_type="Sick Leave", rules=rules
    )
    assert result["blocked"] is True


def test_strongest_action():
    assert bk.strongest_action(["Warning", "Block", "Require HR Approval"]) == "Block"
    assert bk.strongest_action(["Warning"]) == "Warning"
    assert bk.strongest_action([]) is None
    assert bk.strongest_action([None, "Require HR Approval"]) == "Require HR Approval"


def test_evaluate_invalid_window():
    result = bk.evaluate_blackout(from_date="2026-06-05", to_date="2026-06-01", rules=[{}])
    assert result["matched"] == []


# --------------------------------------------------------------------------- #
# blackout_decision_fields — maps a decision → Leave Application custom fields
# --------------------------------------------------------------------------- #
def test_decision_fields_none_input():
    assert bk.blackout_decision_fields(None) == {}
    assert bk.blackout_decision_fields("not-a-dict") == {}


def test_decision_fields_no_match_is_empty():
    decision = {"blocked": False, "requires_approval": False, "warnings": [], "matched": []}
    assert bk.blackout_decision_fields(decision) == {}


def test_decision_fields_warning_only_is_empty():
    # A plain warning (no block / no require-approval) is not stamped — ordinary
    # leave applications stay clean; the warning surfaces only at preview.
    decision = {
        "blocked": False,
        "requires_approval": False,
        "warnings": ["cảnh báo"],
        "matched": [{"action": "Warning"}],
    }
    assert bk.blackout_decision_fields(decision) == {}


def test_decision_fields_require_approval():
    decision = {
        "blocked": False,
        "requires_approval": True,
        "warnings": [],
        "matched": [{"action": "Require HR Approval"}, {"action": "Warning"}],
    }
    out = bk.blackout_decision_fields(decision)
    assert out == {
        "vn_requires_blackout_approval": 1,
        "vn_blackout_decision": "Require HR Approval",
    }


def test_decision_fields_block_dominates():
    decision = {
        "blocked": True,
        "requires_approval": True,
        "warnings": [],
        "matched": [{"action": "Require HR Approval"}, {"action": "Block"}, {"action": "Warning"}],
    }
    out = bk.blackout_decision_fields(decision)
    assert out["vn_requires_blackout_approval"] == 1
    assert out["vn_blackout_decision"] == "Block"


def test_decision_fields_block_without_matched_falls_back():
    decision = {"blocked": True, "requires_approval": False, "warnings": []}
    out = bk.blackout_decision_fields(decision)
    assert out == {"vn_requires_blackout_approval": 1, "vn_blackout_decision": "Block"}


def test_decision_fields_requires_without_matched_falls_back():
    decision = {"blocked": False, "requires_approval": True, "warnings": []}
    out = bk.blackout_decision_fields(decision)
    assert out == {
        "vn_requires_blackout_approval": 1,
        "vn_blackout_decision": "Require HR Approval",
    }


def test_decision_fields_matched_non_dict_ignored():
    decision = {
        "blocked": True,
        "requires_approval": False,
        "warnings": [],
        "matched": ["garbage", None, {"action": "Block"}],
    }
    out = bk.blackout_decision_fields(decision)
    # Non-dict entries ignored; the valid Block dict is the source of the action.
    assert out == {"vn_requires_blackout_approval": 1, "vn_blackout_decision": "Block"}


# --------------------------------------------------------------------------- #
# overlapping_rules — overlap guard (plan blackout desk-free §B7, BC1–BC4)
# --------------------------------------------------------------------------- #
def _rule(**overrides):
    rule = {
        "name": "BLK-1",
        "blackout_name": "Tết",
        "company": "Gege Demo",
        "branch": "",
        "department": "",
        "from_date": "2026-06-01",
        "to_date": "2026-06-30",
        "applies_to_leave_type": "",
        "is_active": True,
        "action": "Block",
        "reason": "cao điểm",
        "modified": "2026-06-01",
        "owner": "hr@test.local",
        "modified_by": "hr@test.local",
    }
    rule.update(overrides)
    return rule


def test_bc1_generic_rule_covers_branch_request():
    rules = [_rule(branch="")]  # generic scope
    out = bk.overlapping_rules(rules, from_date="2026-06-10", to_date="2026-06-15", branch="HNI")
    assert [r["blackout_name"] for r in out] == ["Tết"]


def test_bc2_leave_type_mismatch_does_not_overlap():
    rules = [_rule(applies_to_leave_type="Sick Leave")]
    kwargs = {"from_date": "2026-06-10", "to_date": "2026-06-15"}
    assert bk.overlapping_rules(rules, leave_type="Casual Leave", **kwargs) == []
    assert [r["name"] for r in bk.overlapping_rules(rules, leave_type="Sick Leave", **kwargs)] == ["BLK-1"]


def test_bc3_exclude_drops_the_rule_being_edited():
    rules = [_rule(name="BLK-1"), _rule(name="BLK-2", blackout_name="Hè")]
    out = bk.overlapping_rules(
        rules,
        from_date="2026-06-10",
        to_date="2026-06-15",
        exclude="BLK-1",
    )
    assert [r["name"] for r in out] == ["BLK-2"]


def test_bc4_disjoint_window_returns_empty():
    rules = [_rule(from_date="2026-01-01", to_date="2026-01-31")]
    assert bk.overlapping_rules(rules, from_date="2026-06-10", to_date="2026-06-15") == []


def test_bc4b_inactive_rules_never_overlap():
    rules = [_rule(is_active=False)]
    assert bk.overlapping_rules(rules, from_date="2026-06-10", to_date="2026-06-15") == []


# --------------------------------------------------------------------------- #
# build_blackout_csv — Excel-safe export serialiser (§B6)
# --------------------------------------------------------------------------- #
def test_build_blackout_csv_bom_header_and_row():
    csv_text = bk.build_blackout_csv([_rule()])
    assert csv_text.startswith("\ufeff")  # UTF-8 BOM — Excel-safe Vietnamese
    assert csv_text.lstrip("\ufeff").splitlines()[0].startswith("Tên kỳ cấm")
    assert "Tết" in csv_text
    assert "Gege Demo" in csv_text


def test_build_blackout_csv_empty_and_garbage_rows():
    empty = bk.build_blackout_csv([])
    assert empty.startswith("\ufeff")
    assert len(empty.strip("\ufeff").splitlines()) == 1  # header only
    # non-dict entries are skipped, not crashing
    assert "Tên kỳ cấm" in bk.build_blackout_csv(["junk", _rule()])
