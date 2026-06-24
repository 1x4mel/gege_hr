"""Bench-free unit tests for the pure helpers in ``gege_hr.gege_hr.api.notification``.

Covers the row-shaping / flag-coercion logic for the VN Notification inbox
(plan v5 §10.9 / doctype-design §25). The bench-dependent endpoints
(``get_notifications`` / ``mark_read`` / ``mark_all_read``) are exercised on the
bench.
"""

from gege_hr.gege_hr.api import notification as notif


# --------------------------------------------------------------------------- #
# _row
# --------------------------------------------------------------------------- #
def test_row_full_shape():
    doc = {
        "name": "abc123",
        "employee": "HR-EMP-001",
        "employee_name": "Nguyen Van A",
        "user": "a@gege.local",
        "notification_type": "Leave",
        "title": "Đơn nghỉ phép đã duyệt",
        "message": "Đơn của bạn đã được duyệt.",
        "reference_doctype": "Leave Application",
        "reference_name": "HR-LAP-001",
        "read": 1,
        "read_at": "2026-06-21 10:00:00",
        "action_url": "/leave/HR-LAP-001",
        "creation": "2026-06-21 09:00:00",
    }
    out = notif._row(doc)
    assert out["name"] == "abc123"
    assert out["read"] is True
    assert out["notification_type"] == "Leave"
    assert out["action_url"] == "/leave/HR-LAP-001"


def test_row_read_coercion_int():
    assert notif._row({"read": 0})["read"] is False
    assert notif._row({"read": 1})["read"] is True


def test_row_read_coercion_string():
    assert notif._row({"read": "0"})["read"] is False
    assert notif._row({"read": "1"})["read"] is True


def test_row_defaults_missing_fields():
    out = notif._row({"name": "x", "read": 0})
    assert out["employee_name"] == ""
    assert out["user"] == ""
    assert out["title"] == ""
    assert out["message"] == ""
    assert out["reference_doctype"] == ""
    assert out["read_at"] == ""
    assert out["creation"] == ""


def test_row_read_none():
    assert notif._row({"read": None})["read"] is False


# --------------------------------------------------------------------------- #
# _unread_only_flag
# --------------------------------------------------------------------------- #
def test_unread_only_int():
    assert notif._unread_only_flag(1) is True
    assert notif._unread_only_flag(0) is False


def test_unread_only_string_int():
    assert notif._unread_only_flag("1") is True
    assert notif._unread_only_flag("0") is False


def test_unread_only_bool():
    assert notif._unread_only_flag(True) is True
    assert notif._unread_only_flag(False) is False


def test_unread_only_words():
    assert notif._unread_only_flag("true") is True
    assert notif._unread_only_flag("Yes") is True
    assert notif._unread_only_flag("false") is False


def test_unread_only_empty():
    assert notif._unread_only_flag("") is False
    assert notif._unread_only_flag(None) is False


# --------------------------------------------------------------------------- #
# Endpoint marker / import safety
# --------------------------------------------------------------------------- #
def test_module_imports_outside_bench():
    # The whitelist shim must not require frappe at import time.
    for fn in (notif.get_notifications, notif.mark_read, notif.mark_all_read):
        assert callable(fn)
        assert getattr(fn, "whitelisted", False) is True
