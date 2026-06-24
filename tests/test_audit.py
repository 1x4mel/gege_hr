"""Bench-free unit tests for the audit event helpers (``utils/audit.py``).

Covers the vocabulary guards, category mapping, payload serialisation and the
row shaper — no Frappe site needed.
"""

from datetime import date, datetime

import pytest

from gege_hr.gege_hr.utils import audit


# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
def test_audit_types_count():
    # doctype-design §26 spec: 19 audit types (Leave Approve/Reject added
    # alongside Leave Submit/Cancel to surface the HR leave-approval flow,
    # symmetric with OT Submit/Approve).
    assert len(audit.AUDIT_TYPES) == 19
    assert "Manual Override" in audit.AUDIT_TYPES
    assert "Payroll Publish" in audit.AUDIT_TYPES
    assert "Leave Approve" in audit.AUDIT_TYPES
    assert "Leave Reject" in audit.AUDIT_TYPES


def test_is_valid_audit_type():
    assert audit.is_valid_audit_type("Leave Submit") is True
    assert audit.is_valid_audit_type("Bogus") is False
    assert audit.is_valid_audit_type(None) is False


def test_category_for():
    assert audit.category_for("Leave Submit") == "leave"
    assert audit.category_for("OT Approve") == "overtime"
    assert audit.category_for("Monthly Lock") == "monthly"
    assert audit.category_for("Payroll Calculate") == "payroll"
    assert audit.category_for("Unknown") is None
    assert audit.category_for(None) is None


def test_categories_cover_all_types():
    covered = set()
    for types in audit.AUDIT_CATEGORIES.values():
        covered.update(types)
    assert covered == set(audit.AUDIT_TYPES)


# --------------------------------------------------------------------------- #
# audit_payload
# --------------------------------------------------------------------------- #
def test_payload_full():
    payload = audit.audit_payload(
        audit_type="Leave Submit",
        company="Gege",
        actor="hr.demo@gege.demo",
        employee="HR-EMP-0001",
        work_date="2026-06-22",
        actor_ip="10.0.0.1",
        reference_doctype="Leave Application",
        reference_name="HR-LAP-2026-0001",
        description="Approved leave",
        old_value={"status": "Open"},
        new_value={"status": "Approved"},
    )
    assert payload["doctype"] == "VN Audit Event"
    assert payload["audit_type"] == "Leave Submit"
    assert payload["work_date"] == "2026-06-22"
    assert '"Open"' in payload["old_value"]
    assert '"Approved"' in payload["new_value"]


def test_payload_invalid_type_falls_back():
    payload = audit.audit_payload(audit_type="Bogus", company="C")
    assert payload["audit_type"] == "Manual Override"


def test_payload_coerces_date_obj():
    payload = audit.audit_payload(audit_type="Check-in", company="C", work_date=date(2026, 6, 22))
    assert payload["work_date"] == "2026-06-22"


def test_payload_requires_company():
    with pytest.raises(ValueError):
        audit.audit_payload(audit_type="Check-in", company="")


def test_payload_drops_blanks():
    payload = audit.audit_payload(
        audit_type="Check-in",
        company="C",
        description="  ",
        old_value=None,
        new_value="",
    )
    assert "description" not in payload
    assert "old_value" not in payload
    assert "new_value" not in payload


def test_payload_jsonable_non_str():
    payload = audit.audit_payload(
        audit_type="Manual Override", company="C", old_value=[1, 2, 3], new_value={"a": 1}
    )
    assert payload["old_value"] == "[1, 2, 3]"
    assert '"a"' in payload["new_value"]


# --------------------------------------------------------------------------- #
# audit_row
# --------------------------------------------------------------------------- #
def test_row_normalises_and_drops_extra():
    row = {
        "name": "AE-260622-0001",
        "audit_type": "Payroll Publish",
        "company": "Gege",
        "created_at": datetime(2026, 6, 22, 10, 0, 0),
        "rogue": "drop",
    }
    out = audit.audit_row(row)
    assert set(out.keys()) == set(audit.AUDIT_ROW_FIELDS)
    assert out["created_at"] == "2026-06-22 10:00:00"
    assert "rogue" not in out


def test_row_non_dict():
    assert audit.audit_row(None) == {}
    assert audit.audit_row("nope") == {}


def test_row_fields_contract_complete():
    for key in ("name", "audit_type", "company", "actor", "created_at"):
        assert key in audit.AUDIT_ROW_FIELDS
