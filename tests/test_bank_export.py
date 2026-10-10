"""Bench-free unit tests for ``utils/bank_export.py`` (FIX-3, hr-gap-audit I-1).

The file builders are pure (no frappe) — they are the substantive, format-critical
core (NAPAS header/detail/trailer + control total, CSV total row, ACCT lines,
missing-field validation) and are tested exhaustively here without touching frappe.
The thin endpoint wrapper ``api/payroll.export_bank_file`` is verified by
``py_compile`` + a bench e2e (it delegates to these builders); testing it via a
stub-frappe leaks into the shared ``payroll`` module used by ``test_payroll_api``,
so it is intentionally not unit-stubbed here.
"""

import importlib

import pytest


@pytest.fixture
def bank_export():
    return importlib.import_module("gege_hr.gege_hr.utils.bank_export")


def _rows():
    return [
        {"employee": "E1", "employee_name": "An", "account_no": "0123", "bank_name": "VCB", "amount": 1000},
        {
            "employee": "E2",
            "employee_name": "Binh",
            "account_no": "0456",
            "bank_name": "TCB",
            "amount": 2500.5,
        },
    ]


# ── pure helpers ────────────────────────────────────────────────────────────
def test_control_total(bank_export):
    assert bank_export.control_total(_rows()) == 3500.5
    assert bank_export.control_total([]) == 0.0


def test_missing_fields_detects_no_account_and_zero_amount(bank_export):
    rows = [
        {"employee": "E1", "employee_name": "An", "account_no": "0123", "amount": 1000},
        {"employee": "E2", "employee_name": "Binh", "account_no": "", "amount": 500},  # no account
        {"employee": "E3", "employee_name": "Cuong", "account_no": "0789", "amount": 0},  # zero
    ]
    missing = bank_export.missing_fields(rows)
    assert {m["employee"] for m in missing} == {"E2", "E3"}
    assert all("reason" in m for m in missing)


def test_missing_fields_empty_when_all_good(bank_export):
    assert bank_export.missing_fields(_rows()) == []


# ── CSV ─────────────────────────────────────────────────────────────────────
def test_build_csv_has_total_row(bank_export):
    res = bank_export.build_csv(_rows(), value_date="2026-08-08")
    assert res["filename"].endswith(".csv")
    assert res["count"] == 2
    assert res["total"] == 3500.5
    assert "TONG CONG" in res["content"]
    # one row per employee + header + total line
    assert "An" in res["content"] and "Binh" in res["content"]


# ── NAPAS ───────────────────────────────────────────────────────────────────
def test_build_napas_structure_and_control(bank_export):
    res = bank_export.build_napas(_rows(), value_date="2026-08-08", customer_code="GEGE")
    lines = res["content"].strip().split("\n")
    assert lines[0].startswith("H|GEGE|2026-08-08|")
    assert sum(1 for ln in lines if ln.startswith("D|")) == 2
    trailer = [ln for ln in lines if ln.startswith("T|")][0]
    assert trailer.split("|")[1] == "2"  # count
    assert res["total"] == 3500.5
    assert res["count"] == 2
    assert res["filename"].endswith(".napas.txt")


def test_build_napas_empty_rows(bank_export):
    res = bank_export.build_napas([], value_date="2026-08-08")
    lines = res["content"].strip().split("\n")
    assert lines[0].startswith("H|")
    assert lines[-1].startswith("T|0|0")


# ── ACCT ────────────────────────────────────────────────────────────────────
def test_build_acct_one_line_per_row(bank_export):
    res = bank_export.build_acct(_rows(), value_date="2026-08-08")
    lines = res["content"].strip().split("\n")
    assert len(lines) == 2
    assert lines[0].startswith("0123|")
    assert res["count"] == 2


# ── dispatch ────────────────────────────────────────────────────────────────
def test_build_file_dispatch(bank_export):
    assert bank_export.build_file(_rows(), fmt="csv")["filename"].endswith(".csv")
    assert bank_export.build_file(_rows(), fmt="acct")["filename"].endswith(".acct.txt")
    assert bank_export.build_file(_rows(), fmt="napas")["filename"].endswith(".napas.txt")
    # unknown → defaults to napas
    assert bank_export.build_file(_rows(), fmt="weird")["filename"].endswith(".napas.txt")
