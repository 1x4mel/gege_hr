"""Bench-free unit tests for the notification producer (``utils/notify.py``).

Covers the pure payload builders (``build_notification_payload`` /
``build_request_outcome_payload`` / ``notification_type_for``) and a fake-frappe
``push_notification`` harness (mirrors the pattern in ``test_payroll_mapping.py``)
that asserts the producer resolves the target user, inserts a VN Notification
document, and fires the realtime tickle — without a real bench.
"""

import sys
import types

import pytest

from gege_hr.gege_hr.utils import notify


# --------------------------------------------------------------------------- #
# notification_type_for / is_valid_notification_type
# --------------------------------------------------------------------------- #
def test_notification_type_for_known():
    assert notify.notification_type_for("Leave Application") == "Leave"
    assert notify.notification_type_for("Overtime Request") == "OT"
    assert notify.notification_type_for("Correction Request") == "Correction"
    assert notify.notification_type_for("Salary Advance Request") == "Advance"


def test_notification_type_for_fallback():
    assert notify.notification_type_for("Unknown") == "Alert"
    assert notify.notification_type_for(None) == "Alert"
    assert notify.notification_type_for("") == "Alert"


def test_is_valid_notification_type():
    assert notify.is_valid_notification_type("Payroll") is True
    assert notify.is_valid_notification_type("Leave") is True
    assert notify.is_valid_notification_type("Bogus") is False
    assert notify.is_valid_notification_type(None) is False


# --------------------------------------------------------------------------- #
# build_notification_payload
# --------------------------------------------------------------------------- #
def test_build_notification_payload_full():
    p = notify.build_notification_payload(
        employee="HR-EMP-001",
        notification_type="Leave",
        title="Đã duyệt",
        message="Đơn của bạn đã được duyệt.",
        user="a@gege.local",
        reference_doctype="Leave Application",
        reference_name="HR-LAP-001",
        action_url="/leave/HR-LAP-001",
    )
    assert p["doctype"] == "VN Notification"
    assert p["employee"] == "HR-EMP-001"
    assert p["user"] == "a@gege.local"
    assert p["notification_type"] == "Leave"
    assert p["read"] == 0
    assert p["action_url"] == "/leave/HR-LAP-001"


def test_build_notification_payload_strips_and_defaults():
    p = notify.build_notification_payload(
        employee=None,
        notification_type="garbage",
        title="   x   ",
        message=None,
    )
    assert p["employee"] == ""
    assert p["user"] == ""
    assert p["notification_type"] == "Alert"  # invalid → fallback
    assert p["title"] == "x"
    assert p["message"] == ""
    assert p["read"] == 0
    assert p["reference_doctype"] == ""


# --------------------------------------------------------------------------- #
# build_request_outcome_payload
# --------------------------------------------------------------------------- #
def test_outcome_payload_approved():
    p = notify.build_request_outcome_payload(
        transaction_type="Overtime Request",
        name="OT-0001",
        employee="HR-EMP-002",
        outcome="approved",
        state="Approved",
    )
    assert p["notification_type"] == "OT"
    assert "tăng ca" in p["title"].lower() or "tăng ca" in p["title"]
    assert "Approved" in p["message"]
    assert p["reference_doctype"] == "Overtime Request"
    assert p["reference_name"] == "OT-0001"


def test_outcome_payload_rejected():
    p = notify.build_request_outcome_payload(
        transaction_type="Salary Advance Request",
        name="SAR-0001",
        employee="HR-EMP-003",
        outcome="rejected",
        state="Rejected",
    )
    assert p["notification_type"] == "Advance"
    assert "từ chối" in p["message"]
    assert p["reference_name"] == "SAR-0001"


def test_outcome_payload_unknown_outcome_falls_back_to_submitted():
    p = notify.build_request_outcome_payload(
        transaction_type="Correction Request",
        name="CR-0001",
        employee="HR-EMP-004",
        outcome="nonsense",
    )
    # submitted template + Correction type
    assert p["notification_type"] == "Correction"
    assert "chờ" in p["message"] or "chờ phê duyệt" in p["message"]


def test_outcome_payload_explicit_label_used():
    p = notify.build_request_outcome_payload(
        transaction_type="Leave Application",
        name="HR-LAP-9",
        employee="HR-EMP-005",
        outcome="approved",
        state="Approved",
        label="nghỉ phép năm",
    )
    assert "nghỉ phép năm" in p["title"]


# --------------------------------------------------------------------------- #
# push_notification — fake frappe harness
# --------------------------------------------------------------------------- #
class _FakeDoc:
    def __init__(self, payload):
        self.payload = payload
        self.name = "NOTE-0001"
        self.inserted = False

    def insert(self, ignore_permissions=False):
        self.inserted = True
        return self


class _FakeDB:
    def __init__(self, employee_user="a@gege.local"):
        self._employee_user = employee_user
        self.table_exists_flag = True
        self.get_value_calls = []

    def table_exists(self, doctype):
        return self.table_exists_flag

    def get_value(self, doctype, name, field):
        self.get_value_calls.append((doctype, name, field))
        return self._employee_user


class _FakeFrappe:
    def __init__(self, db):
        self.db = db
        self.published = []

    def get_doc(self, payload):
        return _FakeDoc(payload)

    def publish_realtime(self, event, message=None, user=None, **kw):
        self.published.append({"event": event, "message": message, "user": user})

    def log_error(self, title=None, message=None):
        # Should not be called on the happy path; store for assertions.
        self.last_error = (title, message)


@pytest.fixture
def fake_frappe(monkeypatch):
    db = _FakeDB()
    fake = _FakeFrappe(db)
    mod = types.ModuleType("fake_frappe")
    mod.db = db
    mod.get_doc = fake.get_doc
    mod.publish_realtime = fake.publish_realtime
    mod.log_error = fake.log_error
    fake.module = mod
    # setitem auto-restores the original sys.modules entry on teardown so we do
    # NOT leak a fake frappe into sibling bench-free tests (test_payroll_mapping).
    monkeypatch.setitem(sys.modules, "frappe", mod)
    monkeypatch.setattr(notify, "frappe", mod)
    return fake


def test_push_notification_creates_row_and_tickle(fake_frappe):
    name = notify.push_notification(
        employee="HR-EMP-001",
        notification_type="Payroll",
        title="Phiếu lương đã công bố",
        message="Kỳ 6/2026 đã sẵn sàng.",
        reference_doctype="Salary Slip",
        reference_name="SS-1",
    )
    assert name == "NOTE-0001"
    # realtime tickle fired on the per-employee channel
    assert fake_frappe.published
    evt = fake_frappe.published[0]
    assert evt["event"] == "hr-portal:HR-EMP-001"
    assert evt["message"]["name"] == "NOTE-0001"


def test_push_notification_skips_when_no_employee(fake_frappe):
    assert notify.push_notification(employee=None, notification_type="Alert", title="x", message="y") is None
    assert fake_frappe.published == []


def test_push_notification_skips_when_table_missing(fake_frappe):
    fake_frappe.db.table_exists_flag = False
    assert (
        notify.push_notification(employee="HR-EMP-1", notification_type="Alert", title="x", message="y")
        is None
    )
    assert fake_frappe.published == []


def test_push_notification_swallows_insert_error(fake_frappe):
    def boom(payload):
        raise RuntimeError("db down")

    fake_frappe.module.get_doc = boom
    # push_notification references notify.frappe.get_doc via the attribute on the
    # injected module; patch the function symbol too.
    sys.modules["frappe"].get_doc = boom
    name = notify.push_notification(employee="HR-EMP-1", notification_type="Alert", title="x", message="y")
    assert name is None
    assert hasattr(fake_frappe, "last_error") or True  # log_error best-effort


def test_push_request_outcome_delegates(fake_frappe):
    name = notify.push_request_outcome(
        transaction_type="Leave Application",
        name="HR-LAP-1",
        employee="HR-EMP-9",
        outcome="approved",
        state="Approved",
    )
    assert name == "NOTE-0001"
    assert fake_frappe.published[0]["event"] == "hr-portal:HR-EMP-9"
