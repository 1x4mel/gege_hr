"""Cầu nối HR ↔ console gege-seat — helper thuần (plans/plan-tai-khoan-hr-cho-console.md)."""

from __future__ import annotations

from gege_hr.gege_hr.utils.seat_checkin import build_accounts, caller_allowed, console_host, parse_qr_code

URL = "http://192.168.2.90:8080"


def test_console_host():
    assert console_host(URL) == "192.168.2.90"
    assert console_host("https://Console.GegeTeam.xyz/") == "console.gegeteam.xyz"
    assert console_host("") == ""
    assert console_host(None) == ""


def test_caller_allowed_needs_ip_and_key():
    assert caller_allowed("192.168.2.90", "K", URL, "K") is True
    assert caller_allowed("192.168.2.49", "K", URL, "K") is False  # máy khác trong LAN
    assert caller_allowed("::1", "K", URL, "K") is False  # qua Cloudflare Tunnel
    assert caller_allowed("192.168.2.90", "sai", URL, "K") is False
    assert caller_allowed("192.168.2.90", "", URL, "K") is False
    assert caller_allowed("192.168.2.90", "K", URL, None) is False  # chưa ghép nối
    assert caller_allowed("192.168.2.90", "K", "", "K") is False


def test_build_accounts_eligibility():
    users = [
        {"name": "an@gegeteam.net", "full_name": "An", "enabled": 1},
        {"name": "nghi@gegeteam.net", "full_name": "Nghỉ", "enabled": 1},
        {"name": "khoa@gegeteam.net", "full_name": "Khoá", "enabled": 0},
        {"name": "sep@gegeteam.net", "full_name": "Sếp", "enabled": 1},  # không có hồ sơ NV
        {"name": "khongphaiemail", "full_name": "x", "enabled": 1},
    ]
    employees = [
        {
            "user_id": "An@GegeTeam.net",
            "status": "Active",
            "employee_name": "An",
            "department": "Trader - GG",
        },
        {
            "user_id": "nghi@gegeteam.net",
            "status": "Left",
            "employee_name": "Nghỉ",
            "department": "Booster - GG",
        },
        {
            "user_id": "khoa@gegeteam.net",
            "status": "Active",
            "employee_name": "Khoá",
            "department": "IT - GG",
        },
    ]
    rows = {r["email"]: r for r in build_accounts(users, employees)}
    assert set(rows) == {"an@gegeteam.net", "nghi@gegeteam.net", "khoa@gegeteam.net", "sep@gegeteam.net"}
    assert rows["an@gegeteam.net"]["eligible"] is True
    assert rows["an@gegeteam.net"]["department"] == "Trader - GG"
    assert (
        rows["nghi@gegeteam.net"]["eligible"] is False
        and rows["nghi@gegeteam.net"]["employee_status"] == "Left"
    )
    assert (
        rows["khoa@gegeteam.net"]["eligible"] is False and rows["khoa@gegeteam.net"]["user_enabled"] is False
    )
    # Không có hồ sơ nhân viên → employee_status None (console không được tự khoá).
    assert (
        rows["sep@gegeteam.net"]["employee_status"] is None and rows["sep@gegeteam.net"]["eligible"] is False
    )


def test_build_accounts_prefers_active_record():
    users = [{"name": "a@x.vn", "full_name": "A", "enabled": 1}]
    employees = [
        {"user_id": "a@x.vn", "status": "Left", "employee_name": "A cũ"},
        {"user_id": "a@x.vn", "status": "Active", "employee_name": "A mới"},
    ]
    assert build_accounts(users, employees)[0]["eligible"] is True


def test_parse_qr_code():
    assert parse_qr_code("AbC_123-xyz0") == "AbC_123-xyz0"
    assert parse_qr_code("  AbC_123-xyz0 ") == "AbC_123-xyz0"
    assert parse_qr_code("ngắn") == ""
    assert parse_qr_code("a" * 65) == ""
    assert parse_qr_code("abc/../../etc") == ""
    assert parse_qr_code(None) == ""
