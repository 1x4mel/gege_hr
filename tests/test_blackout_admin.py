"""Bench-free unit tests for the desk-free blackout admin endpoints
(``api/leave_blackout.py`` — plans/plan-blackout-desk-free.md §5.1).

Reuses the stub-frappe harness from ``tests/test_blackout_api.py``. Covers:

  * ``blackout_distinct``        → BD1–BD4 (options feed)
  * ``blackout_versions``        → BV1–BV3 (Version timeline)
  * ``blackout_impact``          → BI1–BI3 (rule → leave applications)
  * ``bulk_update/delete``       → BB1–BB4 (multi-select)
  * ``export_blackout_csv``      → BE1–BE3 (CSV + audit stamp)
  * ``on_doc_event``             → BR1–BR2 (realtime broadcast)
"""

import json
import types

import pytest

from tests.test_blackout_api import _rule, make_fake


@pytest.fixture
def fake(monkeypatch):
    return make_fake(monkeypatch)


def _la(name, frm, to, status="Open", flagged=0, company="Gege Demo", posting="2026-06-20"):
    row = {
        "name": name,
        "employee": "HR-EMP-0001",
        "employee_name": "Nguyễn Văn A",
        "leave_type": "Casual Leave",
        "from_date": frm,
        "to_date": to,
        "status": status,
        "posting_date": posting,
        "company": company,
    }
    if flagged:
        row["vn_requires_blackout_approval"] = 1
        row["vn_blackout_decision"] = "Block"
    return row


def _version(data, creation="2026-06-02 10:00:00", owner="hr@test.local", name="VER-1"):
    return {"name": name, "owner": owner, "creation": creation, "modified": creation, "data": data}


# --------------------------------------------------------------------------- #
# blackout_distinct — BD1–BD4
# --------------------------------------------------------------------------- #
def test_bd1_dedup_and_frequency_ranking(fake):
    fake.db.get_all_rows = [{"company": c} for c in ["B", "A", "A", "A", "A", "A"]]
    out = fake.api.blackout_distinct("company")
    assert out == [{"value": "A"}, {"value": "B"}]


def test_bd2_bogus_field_throws(fake):
    with pytest.raises(Exception, match="không hợp lệ"):
        fake.api.blackout_distinct("bogus")


def test_bd3_read_gate_rejects_employee(monkeypatch):
    fake = make_fake(monkeypatch, roles=("Employee",))
    with pytest.raises(Exception, match="Yêu cầu quyền HR"):
        fake.api.blackout_distinct("company")


def test_bd4_limit_clamped_to_100(fake):
    fake.db.get_all_rows = [{"company": f"C{i}"} for i in range(150)]
    out = fake.api.blackout_distinct("company", limit=99999)
    assert len(out) == 100


# --------------------------------------------------------------------------- #
# blackout_versions — BV1–BV3 (+BV4 unknown rule)
# --------------------------------------------------------------------------- #
def test_bv1_parses_changed_fields_newest_first(fake):
    fake.db.get_all_rows = [_rule(name="BLK-1")]
    fake.db.version_rows = [
        _version(
            json.dumps({"changed": [["action", "Warning", "Block"], ["reason", "a", "b"]]}),
            creation="2026-06-02 10:00:00",
            name="VER-2",
        ),
        _version(
            json.dumps({"changed": [["reason", "x", "a"]]}),
            creation="2026-06-01 09:00:00",
            name="VER-1",
        ),
    ]
    out = fake.api.blackout_versions("BLK-1")
    assert out["name"] == "BLK-1"
    assert [v["name"] for v in out["versions"]] == ["VER-2", "VER-1"]
    assert out["versions"][0]["actor"] == "hr@test.local"
    assert out["versions"][0]["changes"] == [
        {"field": "action", "old": "Warning", "new": "Block"},
        {"field": "reason", "old": "a", "new": "b"},
    ]


def test_bv2_no_versions_returns_empty(fake):
    fake.db.get_all_rows = [_rule(name="BLK-1")]
    fake.db.version_rows = []
    out = fake.api.blackout_versions("BLK-1")
    assert out == {"name": "BLK-1", "versions": []}


def test_bv3_corrupt_data_entry_dropped(fake):
    fake.db.get_all_rows = [_rule(name="BLK-1")]
    fake.db.version_rows = [
        _version("not-json{", name="VER-BAD"),
        _version(json.dumps({"changed": [["action", "Warning", "Block"]]}), name="VER-OK"),
    ]
    out = fake.api.blackout_versions("BLK-1")
    assert [v["name"] for v in out["versions"]] == ["VER-OK"]


def test_bv4_unknown_rule_throws(fake):
    with pytest.raises(Exception, match="không tồn tại"):
        fake.api.blackout_versions("GHOST")


# --------------------------------------------------------------------------- #
# blackout_impact — BI1–BI3
# --------------------------------------------------------------------------- #
def test_bi1_only_overlapping_non_cancelled_apps(fake):
    fake.db.get_all_rows = [_rule(name="BLK-1", frm="2026-06-01", to="2026-06-30")]
    fake.db.impact_rows = [
        _la("LA-OVERLAP", "2026-06-10", "2026-06-12"),
        _la("LA-DISJOINT", "2026-07-10", "2026-07-12"),
        _la("LA-CANCELLED", "2026-06-11", "2026-06-12", status="Cancelled"),
    ]
    out = fake.api.blackout_impact("BLK-1")
    # The stub DB ignores filters, so the window/Cancelled exclusion is
    # asserted via the list-form filters pushed down to the real DB below.
    assert "LA-OVERLAP" in [a["name"] for a in out["applications"]]
    assert out["total"] == 3
    la_call = [c for c in fake.db.get_all_calls if c["doctype"] == "Leave Application"][-1]
    assert ["from_date", "<=", "2026-06-30"] in la_call["filters"]
    assert ["to_date", ">=", "2026-06-01"] in la_call["filters"]
    assert ["company", "=", "Gege Demo"] in la_call["filters"]
    assert ["status", "!=", "Cancelled"] in la_call["filters"]
    assert ["docstatus", "<", 2] in la_call["filters"]


def test_bi2_flagged_applications_surface_first(fake):
    fake.db.get_all_rows = [_rule(name="BLK-1", frm="2026-06-01", to="2026-06-30")]
    fake.db.impact_rows = [
        _la("LA-PLAIN", "2026-06-10", "2026-06-12", posting="2026-06-21"),
        _la("LA-FLAGGED", "2026-06-10", "2026-06-12", flagged=1, posting="2026-06-19"),
    ]
    out = fake.api.blackout_impact("BLK-1")
    assert [a["name"] for a in out["applications"]] == ["LA-FLAGGED", "LA-PLAIN"]


def test_bi3_capped_at_200(fake):
    fake.db.get_all_rows = [_rule(name="BLK-1", frm="2026-06-01", to="2026-06-30")]
    fake.db.impact_rows = [_la(f"LA-{i:03d}", "2026-06-10", "2026-06-12") for i in range(205)]
    out = fake.api.blackout_impact("BLK-1")
    assert out["total"] == 205
    assert len(out["applications"]) == 200


# --------------------------------------------------------------------------- #
# bulk_update / bulk_delete — BB1–BB4
# --------------------------------------------------------------------------- #
def test_bb1_bulk_update_two_rules(fake):
    fake.db.get_all_rows = [
        _rule(name="BLK-1", blackout_name="A", is_active=True),
        _rule(name="BLK-2", blackout_name="B", is_active=True),
    ]
    out = fake.api.bulk_update_blackouts(["BLK-1", "BLK-2"], {"is_active": 0})
    assert out["updated"] == ["BLK-1", "BLK-2"]
    assert out["failed"] == []
    assert all(doc["is_active"] == 0 for doc in fake.store["saved"])


def test_bb2_bulk_update_reports_per_name_failure(fake):
    fake.db.get_all_rows = [_rule(name="BLK-1", blackout_name="A")]
    out = fake.api.bulk_update_blackouts(["BLK-1", "GHOST"], {"is_active": 0})
    assert out["updated"] == ["BLK-1"]
    assert len(out["failed"]) == 1
    assert out["failed"][0]["name"] == "GHOST"


def test_bb3_bulk_requires_hr_manager(monkeypatch):
    fake = make_fake(monkeypatch, roles=("HR User",))
    with pytest.raises(Exception, match="HR Manager"):
        fake.api.bulk_update_blackouts(["BLK-1"], {"is_active": 0})
    with pytest.raises(Exception, match="HR Manager"):
        fake.api.bulk_delete_blackouts(["BLK-1"])


def test_bb4_bulk_caps_at_50_names(fake):
    names = [f"BLK-{i}" for i in range(51)]
    with pytest.raises(Exception, match="tối đa"):
        fake.api.bulk_update_blackouts(names, {"is_active": 0})


def test_bb5_bulk_delete(fake):
    fake.db.get_all_rows = [_rule(name="BLK-1", blackout_name="A")]
    out = fake.api.bulk_delete_blackouts('["BLK-1"]')  # JSON-string form also accepted
    assert out == {"deleted": ["BLK-1"], "failed": []}
    assert fake.store["deleted"] == ["BLK-1"]


# --------------------------------------------------------------------------- #
# export_blackout_csv — BE1–BE3
# --------------------------------------------------------------------------- #
@pytest.fixture
def stamp_spy(fake, monkeypatch):
    calls = []

    def _spy(*, company, description, new_value):
        calls.append({"company": company, "description": description, "new_value": new_value})

    monkeypatch.setattr(fake.api, "_stamp_export_audit", _spy)
    return types.SimpleNamespace(fake=fake, calls=calls)


def test_be1_export_rows_csv_and_stamp(fake, stamp_spy):
    fake.db.get_all_rows = [_rule(name="BLK-1"), _rule(name="BLK-2", blackout_name="Hè")]
    out = fake.api.export_blackout_csv()
    assert out["rows"] == 2
    assert out["content"].startswith("\ufeff")
    assert "Tên kỳ cấm" in out["content"]
    assert "Tết" in out["content"]
    assert len(stamp_spy.calls) == 1
    assert stamp_spy.calls[0]["company"] == "Gege Demo"
    assert "Xuất CSV kỳ cấm nghỉ" in stamp_spy.calls[0]["description"]
    assert stamp_spy.calls[0]["new_value"]["rows"] == 2


def test_be2_table_not_migrated_returns_empty(fake, stamp_spy):
    fake.db.table_exists_flag = False
    out = fake.api.export_blackout_csv()
    assert out == {"filename": None, "content": None, "rows": 0, "truncated": False}
    assert stamp_spy.calls == []


def test_be3_zero_rows_no_stamp(fake, stamp_spy):
    fake.db.get_all_rows = []
    out = fake.api.export_blackout_csv()
    assert out["rows"] == 0
    assert stamp_spy.calls == []


# --------------------------------------------------------------------------- #
# on_doc_event — BR1–BR2
# --------------------------------------------------------------------------- #
def test_br1_publishes_blackout_rule_changed(fake):
    doc = types.SimpleNamespace(name="BLK-1", company="Gege Demo")
    fake.api.on_doc_event(doc, "on_update")
    assert fake.store["published"] == [
        ("blackout_rule_changed", {"name": "BLK-1", "event": "on_update", "company": "Gege Demo"})
    ]


def test_br2_publish_failure_swallowed(fake, monkeypatch):
    def _boom(event, payload=None):
        raise RuntimeError("socket down")

    monkeypatch.setattr(fake.stub, "publish_realtime", _boom)
    fake.api.on_doc_event(types.SimpleNamespace(name="BLK-1", company="Gege Demo"), "on_trash")
    # must not raise — the business op never depends on realtime succeeding
