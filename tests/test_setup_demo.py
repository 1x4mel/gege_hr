"""Bench-free unit tests for the demo-data seeder (``setup_demo.py``).

Only the pure ``*_payload`` builders + ``allocation_year_window`` are exercised
here — the frappe-aware creators are bench-guarded and verified live on the
site (see handoff). Mirrors the pattern of ``test_notify.py`` /
``test_payroll_mapping.py``.
"""

from datetime import date

import pytest

from gege_hr.gege_hr import setup_demo as demo


# --------------------------------------------------------------------------- #
# company_payload
# --------------------------------------------------------------------------- #
def test_company_payload_defaults():
    p = demo.company_payload()
    assert p["doctype"] == "Company"
    assert p["company_name"] == demo.DEMO_COMPANY
    assert p["abbr"] == demo.DEMO_COMPANY_ABBR  # already upper-case
    assert p["default_currency"] == demo.DEMO_CURRENCY
    assert p["country"] == demo.DEMO_COUNTRY
    assert p["is_group"] == 0


def test_company_payload_abbr_uppercased_and_trimmed():
    p = demo.company_payload(abbr="  gd ")
    assert p["abbr"] == "GD"


def test_company_payload_abbr_empty_falls_back():
    p = demo.company_payload(abbr="   ")
    assert p["abbr"] == demo.DEMO_COMPANY_ABBR


def test_company_payload_name_empty_falls_back():
    p = demo.company_payload(name="   ")
    assert p["company_name"] == demo.DEMO_COMPANY


def test_company_payload_overrides():
    p = demo.company_payload(name="ACME", abbr="ac", currency="USD", country="Singapore")
    assert p["company_name"] == "ACME"
    assert p["abbr"] == "AC"
    assert p["default_currency"] == "USD"
    assert p["country"] == "Singapore"


# --------------------------------------------------------------------------- #
# leave_type_payload
# --------------------------------------------------------------------------- #
def test_leave_type_payload_paid():
    p = demo.leave_type_payload("Casual Leave")
    assert p["doctype"] == "Leave Type"
    assert p["leave_type_name"] == "Casual Leave"
    assert p["is_lwp"] == 0


def test_leave_type_payload_lwp():
    p = demo.leave_type_payload("Leave Without Pay", is_lwp=True)
    assert p["is_lwp"] == 1
    assert p["is_ppl"] == 0


def test_leave_type_payload_strips_name():
    p = demo.leave_type_payload("  Sick Leave  ")
    assert p["leave_type_name"] == "Sick Leave"


def test_leave_type_payload_requires_name():
    with pytest.raises(ValueError):
        demo.leave_type_payload("   ")


# --------------------------------------------------------------------------- #
# user_payload
# --------------------------------------------------------------------------- #
def test_user_payload_basic_lowercases_email():
    p = demo.user_payload("HR.Demo@Gege.Demo", "HR")
    assert p["doctype"] == "User"
    assert p["email"] == "hr.demo@gege.demo"
    assert p["first_name"] == "HR"
    assert p["last_name"] == ""
    assert p["send_welcome_email"] == 0
    assert "roles" not in p


def test_user_payload_with_roles():
    p = demo.user_payload("x@y.z", "A", "B", roles=["Employee", "HR Manager"])
    assert p["roles"] == [
        {"role": "Employee", "doctype": "Has Role"},
        {"role": "HR Manager", "doctype": "Has Role"},
    ]


def test_user_payload_requires_email():
    with pytest.raises(ValueError):
        demo.user_payload("   ", "X")


# --------------------------------------------------------------------------- #
# employee_payload
# --------------------------------------------------------------------------- #
def test_employee_payload_full():
    p = demo.employee_payload(
        "Emp",
        "Demo",
        "Gege Demo",
        "emp@demo.io",
        user_id="emp@demo.io",
        reports_to="HR-0001",
        gender="Female",
    )
    assert p["doctype"] == "Employee"
    assert p["first_name"] == "Emp"
    assert p["last_name"] == "Demo"
    assert p["company"] == "Gege Demo"
    assert p["user_id"] == "emp@demo.io"
    assert p["reports_to"] == "HR-0001"
    assert p["gender"] == "Female"
    assert p["status"] == "Active"
    assert p["prefered_email"] == "emp@demo.io"


def test_employee_payload_omits_optional_links():
    p = demo.employee_payload("Emp", "Demo", "Gege Demo", "emp@demo.io")
    assert "user_id" not in p
    assert "reports_to" not in p
    assert p["gender"] == "Other"  # default


def test_employee_payload_lowercases_email():
    p = demo.employee_payload("Emp", "Demo", "C", "EMP@DEMO.IO")
    assert p["prefered_email"] == "emp@demo.io"


def test_employee_payload_requires_first_name():
    with pytest.raises(ValueError):
        demo.employee_payload("   ", "Demo", "C", "emp@demo.io")


# --------------------------------------------------------------------------- #
# leave_allocation_payload
# --------------------------------------------------------------------------- #
def test_leave_allocation_payload_shape():
    p = demo.leave_allocation_payload("EMP-0001", "Casual Leave", date(2026, 1, 1), date(2026, 12, 31), 12.0)
    assert p["doctype"] == "Leave Allocation"
    assert p["employee"] == "EMP-0001"
    assert p["leave_type"] == "Casual Leave"
    assert p["from_date"] == "2026-01-01"
    assert p["to_date"] == "2026-12-31"
    assert p["new_leaves_allocated"] == 12.0
    assert p["docstatus"] == 1


def test_leave_allocation_payload_coerces_float_and_str():
    p = demo.leave_allocation_payload("EMP-0001", "Casual Leave", "2026-01-01", "2026-12-31", "9")
    assert p["new_leaves_allocated"] == 9.0
    assert p["from_date"] == "2026-01-01"


def test_leave_allocation_payload_zero_when_none():
    p = demo.leave_allocation_payload("E", "L", "2026-01-01", "2026-12-31", None)
    assert p["new_leaves_allocated"] == 0.0


def test_leave_allocation_payload_requires_employee_and_type():
    with pytest.raises(ValueError):
        demo.leave_allocation_payload("   ", "L", "2026-01-01", "2026-12-31", 1)
    with pytest.raises(ValueError):
        demo.leave_allocation_payload("E", "   ", "2026-01-01", "2026-12-31", 1)


# --------------------------------------------------------------------------- #
# allocation_year_window
# --------------------------------------------------------------------------- #
def test_allocation_year_window_default_today():
    today = date.today()
    year, frm, to = demo.allocation_year_window(today=today)
    assert year == today.year
    assert frm == date(today.year, 1, 1)
    assert to == date(today.year, 12, 31)


def test_allocation_year_window_explicit_year():
    year, frm, to = demo.allocation_year_window(year=2024)
    assert year == 2024
    assert frm == date(2024, 1, 1)
    assert to == date(2024, 12, 31)


# --------------------------------------------------------------------------- #
# create_demo_data — bench-free guard (frappe is None outside a site)
# --------------------------------------------------------------------------- #
def test_create_demo_data_no_frappe_returns_empty(monkeypatch):
    monkeypatch.setattr(demo, "frappe", None)
    out = demo.create_demo_data()
    assert out == {
        "company": None,
        "leave_types": [],
        "manager": None,
        "employee": None,
        "allocation": False,
        "reseeded": [],
    }


# --------------------------------------------------------------------------- #
# Module import / constants sanity
# --------------------------------------------------------------------------- #
def test_demo_constants_consistent():
    assert demo.DEMO_COMPANY_ABBR.isupper()
    assert demo.DEMO_CASUAL_LEAVE in [n for n, _ in demo.DEMO_LEAVE_TYPES]
    assert any(demo.DEMO_LEAVE_TYPES[i][1] for i in range(len(demo.DEMO_LEAVE_TYPES)))  # at least one LWP
    # Warehouse Type masters pre-created so ERPNext Company.on_update can finish
    assert "Transit" in demo.DEMO_WAREHOUSE_TYPES
    assert all(isinstance(n, str) and n.strip() for n in demo.DEMO_WAREHOUSE_TYPES)
    # Gender masters pre-created so Employee inserts pass link validation
    assert set(["Male", "Female", "Other"]).issubset(set(demo.DEMO_GENDERS))
    # demo people's genders are all pre-created
    assert demo.DEMO_MANAGER[3] in demo.DEMO_GENDERS
    assert demo.DEMO_EMPLOYEE[3] in demo.DEMO_GENDERS
