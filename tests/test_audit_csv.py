"""WP11 (prod-readiness-plan) — audit CSV export tests (prefix AU).

  AU1 filters are applied (employee + type reach the DB filters)
  AU2 UTF-8 BOM present (Excel opens Vietnamese correctly) + injection-safe cells
  AU3 10k row cap: oversized sets are truncated (flag set, rows == cap)
  AU4 role gate: non-HR users are thrown out

Bench-free stub-frappe harness.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest


class FrappeError(Exception):
    pass


class StubFrappe:
    def __init__(self, roles=("HR Manager",), rows=None, total=0):
        self.roles = list(roles)
        self.rows = rows if rows is not None else []
        self.get_all_calls = []
        self.response = types.SimpleNamespace()
        self.docs_created = []
        self.published = []
        self.last_error = None

        outer = self

        class _DB:
            def table_exists(inner, doctype):
                return True

            def get_all(inner, doctype, filters=None, or_filters=None, fields=None, **kw):
                outer.get_all_calls.append({"doctype": doctype, "filters": filters, "kw": kw})
                return list(outer.rows)

        self.db = _DB()

    def throw(self, msg, exc=None):
        raise (exc or FrappeError)(msg)

    def get_roles(self, user):
        return list(self.roles)

    def get_all(self, doctype, filters=None, or_filters=None, fields=None, **kw):
        self.get_all_calls.append({"doctype": doctype, "filters": filters, "kw": kw})
        return list(self.rows)

    # Export now stamps an audit row (plan audit-center B5 / AU5-AU6): the
    # stamp goes through record() → get_doc + publish_realtime + log_error.
    def get_doc(self, payload):
        doc = types.SimpleNamespace(
            name=f"AE-STAMP-{len(self.docs_created) + 1}",
            payload=payload,
            inserted=False,
        )
        doc.insert = lambda ignore_permissions=False: setattr(doc, "inserted", True) or doc
        self.docs_created.append(payload)
        return doc

    def publish_realtime(self, event, payload=None):
        self.published.append((event, payload))

    def log_error(self, title=None, message=None):
        self.last_error = title


def _row(n=1, **kw):
    base = {
        "name": f"AUD-{n}",
        "audit_type": "Manual Override",
        "company": "GeGe Esport",
        "employee": "HR-EMP-001",
        "work_date": "2026-08-18",
        "actor": "hr@example.com",
        "reference_doctype": "Employee Checkin",
        "reference_name": "CK-1",
        "description": "=SUM(A1:A9) tấn công công thức",
        "old_value": None,
        "new_value": "5",
        "created_at": "2026-08-18 08:00:00",
        "owner": "hr@example.com",
    }
    base.update(kw)
    return base


@pytest.fixture()
def audit_mod(monkeypatch):
    def _make(roles=("HR Manager",), rows=None):
        stub = StubFrappe(roles=roles, rows=rows)
        frappe_mod = types.ModuleType("frappe")
        frappe_mod._ = lambda s: s
        frappe_mod.whitelist = lambda *a, **k: a[0] if a and callable(a[0]) else (lambda f: f)
        frappe_mod.throw = stub.throw
        frappe_mod.get_roles = stub.get_roles
        frappe_mod.get_all = stub.get_all
        frappe_mod.db = stub.db
        frappe_mod.response = stub.response
        frappe_mod.session = types.SimpleNamespace(user="hr@example.com")
        frappe_mod.PermissionError = FrappeError
        frappe_mod.get_doc = stub.get_doc
        frappe_mod.publish_realtime = stub.publish_realtime
        frappe_mod.log_error = stub.log_error

        utils = types.ModuleType("frappe.utils")
        utils.getdate = lambda v=None: __import__("datetime").date(2026, 8, 18)
        frappe_mod.utils = utils

        monkeypatch.setitem(sys.modules, "frappe", frappe_mod)
        monkeypatch.setitem(sys.modules, "frappe.utils", utils)
        mod = importlib.reload(importlib.import_module("gege_hr.gege_hr.api.audit"))
        return mod, stub

    return _make


# AU1 — filters reach the query
def test_au1_filters_applied(audit_mod):
    mod, stub = audit_mod(rows=[_row()])
    res = mod.export_audit_csv(employee="HR-EMP-001", audit_type="Manual Override")
    assert res["rows"] == 1
    sent = stub.get_all_calls[0]
    assert sent["filters"]["employee"] == "HR-EMP-001"
    assert sent["filters"]["audit_type"] == "Manual Override"


# AU2 — BOM + formula-injection neutralised
def test_au2_bom_and_safe_cells(audit_mod):
    mod, _ = audit_mod(rows=[_row(description="=HYPERLINK('x')")])
    res = mod.export_audit_csv()
    assert res["content"].startswith("\ufeff")  # UTF-8 BOM for Excel
    assert "'=HYPERLINK" in res["content"]  # leading = neutralised


# AU3 — 10k cap
def test_au3_ten_k_cap(audit_mod):
    rows = [_row(n=i) for i in range(10001)]
    mod, _ = audit_mod(rows=rows)
    res = mod.export_audit_csv()
    assert res["rows"] == mod.EXPORT_MAX_ROWS == 10000
    assert res["truncated"] is True


# AU4 — role gate
def test_au4_non_hr_rejected(audit_mod):
    mod, _ = audit_mod(roles=("Employee",))
    with pytest.raises(FrappeError):
        mod.export_audit_csv()


# download=1 switches to a binary file response
def test_download_mode_sets_file_response(audit_mod):
    mod, stub_mod = audit_mod(rows=[_row()])
    import sys as _sys

    frappe_mod = _sys.modules["frappe"]
    res = mod.export_audit_csv(download=1)
    assert res["rows"] == 1
    assert frappe_mod.response.type == "binary"
    assert frappe_mod.response.filecontent.startswith(b"\xef\xbb\xbf")


# AU5 — the export itself stamps a Manual Override audit row (B5)
def test_au5_export_stamps_audit_row(audit_mod):
    mod, stub = audit_mod(rows=[_row()])
    res = mod.export_audit_csv(employee="HR-EMP-001")
    assert res["rows"] == 1
    assert len(stub.docs_created) == 1
    payload = stub.docs_created[0]
    assert payload["audit_type"] == "Manual Override"
    assert "Xuất CSV" in payload["description"]
    assert "1 dòng" in payload["description"]
    # company fell back to the first exported row's company
    assert payload["company"] == "GeGe Esport"
    # the stamp carries the applied filters + row count (JSON-serialised)
    new_value = str(payload.get("new_value"))
    assert "rows" in new_value and "HR-EMP-001" in new_value
    # stamp minted through record() → realtime ping fired too
    assert stub.published and stub.published[0][0] == "audit_event_created"


# AU6 — no rows + no company filter → nothing to attribute, no stamp
def test_au6_export_empty_no_stamp(audit_mod):
    mod, stub = audit_mod(rows=[])
    res = mod.export_audit_csv()
    assert res["rows"] == 0
    assert stub.docs_created == []
    assert stub.published == []
