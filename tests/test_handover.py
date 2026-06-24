"""Bench-free unit tests for the leave handover helpers (``utils/handover.py``).

Covers ``can_transition`` / ``handover_payload`` / ``handover_row`` and the
distinct-employee + required-field validation — no Frappe site needed.
"""

from datetime import date, datetime

import pytest

from gege_hr.gege_hr.utils import handover


# --------------------------------------------------------------------------- #
# can_transition
# --------------------------------------------------------------------------- #
def test_can_transition_forward():
    assert handover.can_transition("Pending", "In Progress") is True
    assert handover.can_transition("Pending", "Completed") is True
    assert handover.can_transition("In Progress", "Completed") is True


def test_can_transition_same_state_idempotent():
    assert handover.can_transition("Pending", "Pending") is True
    assert handover.can_transition("Completed", "Completed") is True


def test_can_transition_reopen_terminal():
    assert handover.can_transition("Cancelled", "Pending") is True
    assert handover.can_transition("Completed", "Pending") is True


def test_cannot_transition_invalid():
    # Completed cannot jump straight to In Progress.
    assert handover.can_transition("Completed", "In Progress") is False
    assert handover.can_transition("Pending", "Bogus") is False
    assert handover.can_transition(None, "Completed") is True  # new doc any status
    assert handover.can_transition("Bogus", "Pending") is False


# --------------------------------------------------------------------------- #
# handover_payload
# --------------------------------------------------------------------------- #
def test_payload_full():
    payload = handover.handover_payload(
        leave_application="HR-LAP-2026-0001",
        from_employee="HR-EMP-0001",
        to_employee="HR-EMP-0002",
        handover_date="2026-06-22",
        description="Bàn giao dự án X",
        attachment="/files/handover.pdf",
        note="Ghi chú",
    )
    assert payload["doctype"] == "VN Leave Handover Task"
    assert payload["status"] == "Pending"
    assert payload["handover_date"] == "2026-06-22"
    assert payload["attachment"].endswith("handover.pdf")


def test_payload_coerces_date_obj():
    payload = handover.handover_payload(
        leave_application="LA",
        from_employee="A",
        to_employee="B",
        handover_date=date(2026, 6, 22),
        description="d",
    )
    assert payload["handover_date"] == "2026-06-22"


def test_payload_coerces_datetime_str():
    payload = handover.handover_payload(
        leave_application="LA",
        from_employee="A",
        to_employee="B",
        handover_date="2026-06-22T09:00:00",
        description="d",
    )
    assert payload["handover_date"] == "2026-06-22"


def test_payload_rejects_same_employee():
    with pytest.raises(ValueError):
        handover.handover_payload(
            leave_application="LA",
            from_employee="A",
            to_employee="A",
            handover_date="2026-06-22",
            description="d",
        )


def test_payload_requires_fields():
    with pytest.raises(ValueError):
        handover.handover_payload(
            leave_application="",
            from_employee="A",
            to_employee="B",
            handover_date="2026-06-22",
            description="d",
        )
    with pytest.raises(ValueError):
        handover.handover_payload(
            leave_application="LA",
            from_employee="A",
            to_employee="B",
            handover_date="2026-06-22",
            description="  ",
        )


def test_payload_invalid_status_defaults_pending():
    payload = handover.handover_payload(
        leave_application="LA",
        from_employee="A",
        to_employee="B",
        handover_date="2026-06-22",
        description="d",
        status="Bogus",
    )
    assert payload["status"] == "Pending"


# --------------------------------------------------------------------------- #
# handover_row
# --------------------------------------------------------------------------- #
def test_row_normalises_and_drops_extra():
    row = {
        "name": "ABC",
        "from_employee": "A",
        "to_employee": "B",
        "status": "Completed",
        "completed_at": datetime(2026, 6, 22, 10, 0, 0),
        "rogue": "drop me",
    }
    out = handover.handover_row(row)
    assert set(out.keys()) == set(handover.HANDOVER_ROW_FIELDS)
    assert out["completed_at"] == "2026-06-22T10:00:00"
    assert "rogue" not in out


def test_row_non_dict_returns_empty():
    assert handover.handover_row(None) == {}
    assert handover.handover_row("nope") == {}


def test_row_fields_contract_complete():
    for key in (
        "name",
        "leave_application",
        "from_employee",
        "to_employee",
        "handover_date",
        "status",
        "description",
        "completed_at",
    ):
        assert key in handover.HANDOVER_ROW_FIELDS


# --------------------------------------------------------------------------- #
# suggest_receivers (auto-suggest to_employee from reports_to / team)
# --------------------------------------------------------------------------- #
def _row(name, employee_name=None, reports_to=None, department=None):
    return {
        "name": name,
        "employee_name": employee_name or name,
        "reports_to": reports_to,
        "department": department,
    }


def test_suggest_receivers_empty_when_no_employee():
    assert handover.suggest_receivers(None, None, None, []) == []
    assert handover.suggest_receivers("", None, None, [_row("A")]) == []


def test_suggest_receivers_excludes_self():
    rows = [_row("ME", "Me", "MGR", "Eng"), _row("A", "Alice", "ME", "Eng")]
    out = handover.suggest_receivers("ME", "MGR", "Eng", rows)
    assert all(r["name"] != "ME" for r in out)


def test_suggest_receivers_ranks_direct_reports_first():
    # Alice reports to ME (direct report), Bob is a peer; MGR is the manager.
    rows = [
        _row("BOB", "Bob", "MGR", "Eng"),
        _row("ALICE", "Alice", "ME", "Eng"),
    ]
    out = handover.suggest_receivers("ME", "MGR", "Eng", rows)
    reasons = [r["reason"] for r in out]
    # Direct reports rank before peers, and the manager is appended last.
    assert reasons == ["direct_report", "colleague", "manager"]


def test_suggest_receivers_includes_manager_last():
    # Manager is not in the colleague pool, but should still be suggested.
    rows = [_row("BOB", "Bob", "MGR", "Eng")]
    out = handover.suggest_receivers("ME", "MGR", "Eng", rows)
    reasons = [r["reason"] for r in out]
    assert "manager" in reasons
    assert reasons[-1] == "manager"


def test_suggest_receivers_dedupes_and_capped():
    # Same person appears in both the department pool and direct reports.
    rows = [
        _row("ALICE", "Alice", "ME", "Eng"),
        _row("ALICE", "Alice", "ME", "Eng"),
        _row("BOB", "Bob", "MGR", "Eng"),
    ]
    out = handover.suggest_receivers("ME", "MGR", "Eng", rows)
    names = [r["name"] for r in out]
    assert len(names) == len(set(names))  # no duplicates
    out_capped = handover.suggest_receivers("ME", "MGR", "Eng", rows, limit=1)
    assert len(out_capped) == 1


def test_suggest_receivers_ignores_non_dict_and_blank():
    rows = ["nope", None, {}, _row("", "Blank"), _row("ALICE", "Alice", "ME", "Eng")]
    # reports_to=None → no manager suggestion, only Alice survives.
    out = handover.suggest_receivers("ME", None, "Eng", rows)
    assert [r["name"] for r in out] == ["ALICE"]


def test_suggest_receivers_manager_not_added_when_equals_self():
    # Defensive: a self-referential reports_to must not surface self.
    out = handover.suggest_receivers("ME", "ME", "Eng", [])
    assert out == []


def test_suggest_receivers_label_falls_back_to_name():
    out = handover.suggest_receivers("ME", "MGR", "Eng", [])
    # Only the manager (no display name) → label equals the manager name.
    assert out == [{"name": "MGR", "label": "MGR", "reason": "manager"}]
