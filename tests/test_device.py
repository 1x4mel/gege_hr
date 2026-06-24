"""Bench-free unit tests for the pure helpers in ``gege_hr.gege_hr.api.device``.

These cover the parsing / normalization / status-derivation logic that drives
the device-sync ingest pipeline (plan v5 §10.10). The bench-dependent endpoint
functions (``list_devices`` / ``upload_logs`` / …) are exercised on the bench.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from gege_hr.gege_hr.api import device
from gege_hr.gege_hr.utils import tz as tz_utils

VN = ZoneInfo("Asia/Ho_Chi_Minh")
UTC = ZoneInfo("UTC")


# --------------------------------------------------------------------------- #
# normalize_log_type
# --------------------------------------------------------------------------- #
def test_log_type_in_variants():
    for v in ["IN", "in", "I", "1", "CheckIn", "Clock In", "enter", ""]:
        assert device.normalize_log_type(v) == "IN", v


def test_log_type_out_variants():
    for v in ["OUT", "out", "O", "0", "CheckOut", "Clock Out", "exit", "Leave"]:
        assert device.normalize_log_type(v) == "OUT", v


def test_log_type_none():
    assert device.normalize_log_type(None) == "IN"


# --------------------------------------------------------------------------- #
# parse_log_time
# --------------------------------------------------------------------------- #
def test_parse_log_time_frappe_display_naive_is_device_local():
    # "08:30" interpreted in device TZ (default portal = VN) stays 08:30 VN.
    dt = device.parse_log_time("2026-06-21 08:30:00")
    assert dt.tzinfo is not None
    assert dt.hour == 8 and dt.minute == 30
    assert dt.tzinfo.key == "Asia/Ho_Chi_Minh"


def test_parse_log_time_iso_with_z_is_utc_then_converted():
    # 01:00 UTC == 08:00 VN
    dt = device.parse_log_time("2026-06-21T01:00:00Z")
    assert dt.hour == 8
    assert dt.tzinfo.key == "Asia/Ho_Chi_Minh"


def test_parse_log_time_epoch():
    # 2026-06-21T01:00:00Z as epoch == 08:00 VN
    epoch = datetime(2026, 6, 21, 1, 0, tzinfo=UTC).timestamp()
    dt = device.parse_log_time(epoch)
    assert dt.hour == 8


def test_parse_log_time_invalid_returns_none():
    for bad in [None, "", "not-a-date", "31/13/2026 25:99"]:
        assert device.parse_log_time(bad) is None


def test_parse_log_time_custom_tz():
    # 08:30 Bangkok (UTC+7) → 10:30 VN (UTC+7 too, but different tz object keeps value)
    dt = device.parse_log_time("2026-06-21 08:30:00", tz="Asia/Bangkok")
    # Stored as VN-equivalent instant.
    assert dt.tzinfo.key == "Asia/Ho_Chi_Minh"
    assert dt.hour == 8  # Bangkok == VN offset, so wall-clock unchanged


# --------------------------------------------------------------------------- #
# to_utc_storage_str
# --------------------------------------------------------------------------- #
def test_to_utc_storage_str():
    dt = datetime(2026, 6, 21, 8, 0, tzinfo=VN)  # 08:00 VN == 01:00 UTC
    assert device.to_utc_storage_str(dt) == "2026-06-21 01:00:00"


# --------------------------------------------------------------------------- #
# derive_device_status
# --------------------------------------------------------------------------- #
def test_status_inactive_wins():
    now = tz_utils.now_in_portal().strftime("%Y-%m-%d %H:%M:%S")
    assert device.derive_device_status(is_active=0, last_sync_at=now) == "Inactive"


def test_status_synced_recent():
    recent = tz_utils.now_in_portal() - timedelta(hours=1)
    assert device.derive_device_status(is_active=1, last_sync_at=recent) == "Synced"


def test_status_offline_stale():
    stale = tz_utils.now_in_portal() - timedelta(hours=48)
    assert device.derive_device_status(is_active=1, last_sync_at=stale) == "Offline"


def test_status_idle_no_sync():
    assert device.derive_device_status(is_active=1, last_sync_at=None) == "Idle"


# --------------------------------------------------------------------------- #
# normalize_upload_log
# --------------------------------------------------------------------------- #
def test_normalize_upload_log_employee_and_defaults():
    norm, err = device.normalize_upload_log(
        {
            "log_time": "2026-06-21 08:30:00",
            "employee": "HR-EMP-0001",
            "punch_type": "out",
            "device_id": "DEV-01",
        }
    )
    assert err is None
    assert norm["employee"] == "HR-EMP-0001"
    assert norm["log_type"] == "OUT"
    assert norm["device_code"] == "DEV-01"
    assert norm["log_time"].hour == 8


def test_normalize_upload_log_user_field():
    norm, err = device.normalize_upload_log({"log_time": "2026-06-21 08:30:00", "user": "nv1@example.com"})
    assert err is None
    assert norm["user"] == "nv1@example.com"
    assert norm["log_type"] == "IN"  # default


def test_normalize_upload_log_uses_default_device_code():
    norm, _ = device.normalize_upload_log(
        {"log_time": "2026-06-21 08:30:00", "raw_employee_code": "1001"},
        default_device_code="DEV-DEFAULT",
    )
    assert norm["device_code"] == "DEV-DEFAULT"
    assert norm["raw_employee_code"] == "1001"


def test_normalize_upload_log_rejects_missing_time():
    norm, err = device.normalize_upload_log({"employee": "HR-EMP-0001"})
    assert norm is None
    assert "thời gian" in err.lower() or "log_time" in err.lower()


def test_normalize_upload_log_rejects_missing_identity():
    norm, err = device.normalize_upload_log({"log_time": "2026-06-21 08:30:00"})
    assert norm is None
    assert err is not None


def test_normalize_upload_log_rejects_empty():
    norm, err = device.normalize_upload_log(None)
    assert norm is None
    assert err


def test_normalize_upload_log_lat_lon_parsed():
    norm, _ = device.normalize_upload_log(
        {"log_time": "2026-06-21 08:30:00", "employee": "1", "latitude": "10.77", "longitude": 106.7}
    )
    assert norm["latitude"] == 10.77
    assert norm["longitude"] == 106.7


def test_normalize_upload_log_bad_lat_ignored():
    norm, _ = device.normalize_upload_log(
        {"log_time": "2026-06-21 08:30:00", "employee": "1", "latitude": "abc"}
    )
    assert norm["latitude"] is None


# --------------------------------------------------------------------------- #
# device_row
# --------------------------------------------------------------------------- #
def test_device_row_shape_matches_fe_contract():
    row = device.device_row(
        {
            "name": "DEV-01",
            "device_code": "DEV-01",
            "device_name": "Cổng chính",
            "company": "GE",
            "device_type": "Face Recognition",
            "work_location": "VN WL HN",
            "ip_address": "10.0.0.1",
            "sync_method": "Scheduled Pull",
            "timezone": "Asia/Ho_Chi_Minh",
            "is_active": 1,
            "last_sync_at": tz_utils.now_in_portal().strftime("%Y-%m-%d %H:%M:%S"),
        },
        last_log_time="2026-06-21 08:30:00",
        employee_count=12,
    )
    # FE keys on device_code || device_id → both must be present.
    assert row["device_code"] == "DEV-01"
    assert row["device_id"] == "DEV-01"
    assert row["device_name"] == "Cổng chính"
    assert row["location"] == "VN WL HN"
    assert row["employee_count"] == 12
    assert row["last_log_time"] == "2026-06-21 08:30:00"
    assert row["is_active"] == 1
    assert row["is_syncing"] == 0
    assert row["status"] == "Synced"


def test_device_row_inactive_status():
    row = device.device_row({"name": "X", "device_code": "X", "is_active": 0})
    assert row["status"] == "Inactive"
    assert row["employee_count"] is None


# --------------------------------------------------------------------------- #
# Module import / decorator sanity (no bench)
# --------------------------------------------------------------------------- #
def test_endpoints_are_whitelisted_markers():
    # Outside a bench the shim sets `.whitelisted = True`.
    for fn in (device.list_devices, device.sync_status, device.sync_device, device.upload_logs):
        assert getattr(fn, "whitelisted", False) is True
