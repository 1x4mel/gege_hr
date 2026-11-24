"""Danh bạ rút gọn cho HR Intern (24/11/2026): không bao giờ trả trường nhạy cảm."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "gege_hr" / "gege_hr" / "api" / "employee_directory.py"

FORBIDDEN = {
    "ctc",
    "salary_currency",
    "salary_mode",
    "bank_name",
    "bank_ac_no",
    "iban",
    "passport_number",
    "date_of_birth",
    "gender",
    "cell_number",
    "personal_email",
    "current_address",
    "permanent_address",
    "payroll_group",
}


def _literal(var: str):
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    for node in tree.body:
        target = getattr(node, "target", None) or (getattr(node, "targets", None) or [None])[0]
        if isinstance(target, ast.Name) and target.id == var:
            return node.value
    raise AssertionError(f"{var} not found")


def test_directory_fields_exclude_sensitive():
    fields = set(ast.literal_eval(_literal("DIRECTORY_FIELDS")))
    assert not (fields & FORBIDDEN), fields & FORBIDDEN
    assert not any("salary" in f or "bank" in f or "address" in f or "id_" in f for f in fields)


def test_search_fields_subset_of_directory_fields():
    fields = set(ast.literal_eval(_literal("DIRECTORY_FIELDS")))
    search = set(ast.literal_eval(_literal("_SEARCH_FIELDS")))
    assert search <= fields


def test_directory_roles_include_intern_not_employee():
    node = _literal("DIRECTORY_ROLES")
    src = ast.unparse(node)
    assert "HR_INTERN_ROLE" in src
    assert "'Employee'" not in src and '"Employee"' not in src
