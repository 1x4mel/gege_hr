"""utils.print_pdf.send_pdf — bỏ qua kiểm quyền in của Frappe (API đã tự kiểm)."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "gege_hr" / "gege_hr" / "utils" / "print_pdf.py"


@pytest.fixture
def mod(monkeypatch):
    seen = {}
    stub = types.ModuleType("frappe")
    stub.flags = types.SimpleNamespace(ignore_print_permissions=False)
    stub.local = types.SimpleNamespace(response=types.SimpleNamespace())
    stub.get_doc = lambda dt, name: types.SimpleNamespace(doctype=dt, name=name)

    def get_print(doctype, name, fmt, doc=None, as_pdf=False):
        seen["flag"] = stub.flags.ignore_print_permissions
        seen["args"] = (doctype, name, fmt, as_pdf)
        if name == "BOOM":
            raise RuntimeError("x")
        return b"%PDF"

    stub.get_print = get_print
    monkeypatch.setitem(sys.modules, "frappe", stub)
    spec = importlib.util.spec_from_file_location("print_pdf_under_test", SRC)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m, stub, seen


def test_sets_flag_only_during_render_and_fills_response(mod):
    m, stub, seen = mod
    m.send_pdf("Salary Slip", "Sal/Slip 1", "Phiếu lương VN")
    assert seen == {"flag": True, "args": ("Salary Slip", "Sal/Slip 1", "Phiếu lương VN", True)}
    assert stub.flags.ignore_print_permissions is False
    r = stub.local.response
    assert (r.filename, r.filecontent, r.type) == ("Sal-Slip-1.pdf", b"%PDF", "pdf")


def test_flag_restored_on_error(mod):
    m, stub, _ = mod
    with pytest.raises(RuntimeError):
        m.send_pdf("Salary Slip", "BOOM")
    assert stub.flags.ignore_print_permissions is False
