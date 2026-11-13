"""User Permission "Employee = chính mình" cho nhân viên thường (13/11/2026)."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "gege_hr" / "gege_hr" / "api" / "employee_lifecycle.py"


@pytest.fixture
def mod(monkeypatch):
    calls, errors = [], []
    roles = {"emp@x": ["Employee"], "hr@x": ["Employee", "HR Manager"], "pay@x": ["Payroll Manager"]}
    stub = types.ModuleType("frappe")
    stub._ = lambda s: s
    stub.whitelist = lambda *a, **k: a[0] if a and callable(a[0]) else (lambda f: f)
    stub.get_roles = lambda u: roles.get(u, [])
    stub.log_error = lambda *a, **k: errors.append(k.get("title"))
    perms = types.ModuleType("frappe.permissions")

    def add_user_permission(doctype, name, user, ignore_permissions=False):
        if user == "boom@x":
            raise RuntimeError("x")
        calls.append((doctype, name, user, ignore_permissions))

    perms.add_user_permission = add_user_permission
    stub.permissions = perms
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.permissions", perms)
    spec = importlib.util.spec_from_file_location("employee_lifecycle_under_test", SRC)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m, calls, errors


def _doc(user, status="Active", cup=1, name="HR-EMP-1"):
    return types.SimpleNamespace(name=name, user_id=user, status=status, create_user_permission=cup)


@pytest.mark.parametrize(
    ("status", "user", "cup", "roles", "want"),
    [
        ("Active", "emp@x", 1, ["Employee"], True),
        ("Active", "hr@x", 1, ["Employee", "HR Manager"], False),
        ("Active", "pay@x", 1, ["Payroll Manager"], False),
        ("Active", "emp@x", 0, ["Employee"], False),
        ("Left", "emp@x", 1, ["Employee"], False),
        ("Active", "", 1, ["Employee"], False),
        ("Active", "emp@x", None, ["Employee"], False),
    ],
)
def test_needs_self_permission(mod, status, user, cup, roles, want):
    m, _, _ = mod
    assert m.needs_self_permission(status, user, cup, roles) is want


def test_hook_adds_for_regular_employee(mod):
    m, calls, _ = mod
    m.ensure_self_user_permission(_doc("emp@x"))
    assert calls == [("Employee", "HR-EMP-1", "emp@x", True)]


def test_hook_skips_hr_accounts(mod):
    m, calls, _ = mod
    m.ensure_self_user_permission(_doc("hr@x"))
    m.ensure_self_user_permission(_doc("pay@x"))
    assert calls == []


def test_hook_never_breaks_the_save(mod):
    m, calls, errors = mod
    m.ensure_self_user_permission(_doc("boom@x"))
    assert calls == [] and errors == ["ensure_self_user_permission failed"]
