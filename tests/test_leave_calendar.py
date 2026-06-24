"""Bench-free unit tests for the leave calendar cache helpers
(``utils/leave_calendar.py``).

Covers the cache-key composition, month normalisation, expiry logic and the
payload/row shapers — no Frappe site needed.
"""

from datetime import datetime, timedelta

import pytest

from gege_hr.gege_hr.utils import leave_calendar as cal


# --------------------------------------------------------------------------- #
# normalize_month / normalize_year
# --------------------------------------------------------------------------- #
def test_normalize_month():
    assert cal.normalize_month(6) == "06"
    assert cal.normalize_month("06") == "06"
    assert cal.normalize_month("6") == "06"
    assert cal.normalize_month(12) == "12"
    assert cal.normalize_month(0) is None
    assert cal.normalize_month(13) is None
    assert cal.normalize_month("abc") is None
    assert cal.normalize_month(None) is None


def test_normalize_year():
    assert cal.normalize_year(2026) == 2026
    assert cal.normalize_year("2026") == 2026
    assert cal.normalize_year(0) is None
    assert cal.normalize_year(None) is None


# --------------------------------------------------------------------------- #
# build_cache_key
# --------------------------------------------------------------------------- #
def test_cache_key_full_scope():
    key = cal.build_cache_key(company="Gege", branch="HN", department="Sales", year=2026, month=6)
    assert key == "Gege-HN-Sales-2026-06"


def test_cache_key_all_scope():
    key = cal.build_cache_key(company="Gege", year=2026, month=6)
    assert key == "Gege-ALL-ALL-2026-06"


def test_cache_key_invalid_returns_none():
    assert cal.build_cache_key(company="", year=2026, month=6) is None
    assert cal.build_cache_key(company="Gege", year=2026, month=13) is None
    assert cal.build_cache_key(company="Gege", year=None, month=6) is None


# --------------------------------------------------------------------------- #
# month_window
# --------------------------------------------------------------------------- #
def test_month_window_basic():
    assert cal.month_window(2026, 6) == ("2026-06-01", "2026-06-30")


def test_month_window_february_leap():
    assert cal.month_window(2024, 2) == ("2024-02-01", "2024-02-29")


def test_month_window_february_nonleap():
    assert cal.month_window(2026, 2) == ("2026-02-01", "2026-02-28")


def test_month_window_invalid():
    assert cal.month_window(2026, 13) is None


# --------------------------------------------------------------------------- #
# is_expired / compute_expiry
# --------------------------------------------------------------------------- #
def test_is_expired_absent():
    assert cal.is_expired(None) is True
    assert cal.is_expired("") is True


def test_is_expired_past():
    past = (datetime.utcnow() - timedelta(hours=1)).isoformat(sep=" ")
    assert cal.is_expired(past) is True


def test_is_expired_future():
    future = (datetime.utcnow() + timedelta(hours=2)).isoformat(sep=" ")
    assert cal.is_expired(future) is False


def test_is_expired_now_boundary():
    now = datetime.utcnow()
    assert cal.is_expired(now.isoformat(sep=" "), now=now) is True


def test_compute_expiry_adds_ttl():
    base = datetime(2026, 6, 22, 10, 0, 0)
    expiry = cal.compute_expiry(now=base, ttl_hours=6)
    assert expiry == "2026-06-22 16:00:00"


# --------------------------------------------------------------------------- #
# calendar_cache_payload / row
# --------------------------------------------------------------------------- #
def test_payload_shape():
    payload = cal.calendar_cache_payload(
        company="Gege",
        year=2026,
        month=6,
        data={"days": [1, 2, 3]},
        generated_at="2026-06-22 10:00:00",
    )
    assert payload["cache_key"] == "Gege-ALL-ALL-2026-06"
    assert payload["year"] == 2026
    assert payload["month"] == "06"
    assert '"days"' in payload["data_json"]


def test_payload_invalid_scope_returns_none():
    payload = cal.calendar_cache_payload(company="", year=2026, month=6, data={})
    assert payload is None


def test_row_normalises():
    row = {
        "name": "X",
        "cache_key": "Gege-ALL-ALL-2026-06",
        "company": "Gege",
        "month": "06",
        "year": 2026,
        "data_json": "{}",
        "generated_at": datetime(2026, 6, 22, 10, 0, 0),
        "rogue": "drop",
    }
    out = cal.calendar_cache_row(row)
    assert out["generated_at"] == "2026-06-22 10:00:00"
    assert "rogue" not in out


def test_row_non_dict():
    assert cal.calendar_cache_row(None) == {}
