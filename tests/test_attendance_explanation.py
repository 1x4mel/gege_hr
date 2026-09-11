"""Bench-free unit tests: cửa sổ chấm công → phiếu giải trình bắt buộc.

Covers ``api/attendance._window_violation`` (4 hướng lệch + trong cửa sổ) và
``api/explanation.decide_explanations`` validations. Stub-frappe harness theo
mẫu ``test_monthly_self`` (stub vào sys.modules trước khi import).
"""

from __future__ import annotations

import importlib
import sys
import types
from datetime import datetime

import pytest


class _AttrDict(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            return None


class _ValidationErr(Exception):
    pass


class _Thrown(Exception):
    def __init__(self, message, kind="Validation"):
        super().__init__(message)
        self.message = str(message)
        self.kind = kind


def _build_frappe():
    mod = types.ModuleType("frappe")
    mod._ = lambda s, *a, **k: s.format(*a) if a else s
    mod.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)
    mod.PermissionError = type("PermissionError", (Exception,), {})
    mod.ValidationError = _ValidationErr
    mod.throw = lambda message, exc=None: (_ for _ in ()).throw(
        _Thrown(message, "Validation" if exc is _ValidationErr else "Generic")
    )
    mod.only_for = lambda roles: None
    mod.get_doc = lambda *a, **k: None
    mod.log_error = lambda *a, **k: None
    mod.get_roles = lambda user=None: ("HR Manager",)
    mod.session = types.SimpleNamespace(user="hr@test.local")

    utils = types.ModuleType("frappe.utils")
    utils.flt = lambda v, p=None: round(float(v or 0), p) if p is not None else float(v or 0)
    utils.getdate = lambda v=None: datetime.strptime(str(v)[:10], "%Y-%m-%d").date() if v else None
    utils.now_datetime = lambda: datetime(2026, 9, 11, 10, 0, 0)
    utils.cint = lambda v: int(v or 0)

    def _generic(name):
        return lambda *a, **k: None

    utils.__getattr__ = _generic
    mod.utils = utils

    # db double
    class _DB:
        values = {}  # {(doctype, name): dict}
        stores = {}

        @staticmethod
        def get_value(doctype, name, fields=None, as_dict=False):
            if isinstance(name, dict):
                return None
            v = _DB.values.get((doctype, name), {})
            if isinstance(fields, str):
                return v.get(fields)
            return _AttrDict({f: v.get(f) for f in (fields or [])}) if as_dict else v

        @staticmethod
        def exists(doctype, name):
            return name if (doctype, name) in _DB.values else None

        @staticmethod
        def get_all(doctype, filters=None, fields=None, **kw):
            rows = [dict(v, name=k[1]) for k, v in _DB.stores.get(doctype, {}).items()]
            if isinstance(filters, dict):
                rows = [r for r in rows if all(r.get(k) == v for k, v in filters.items())]
            if fields:
                return [_AttrDict({f: r.get(f) for f in fields}) for r in rows]
            return [_AttrDict(r) for r in rows]

        @staticmethod
        def commit():
            pass

    mod.db = _DB
    return mod, utils


@pytest.fixture
def env(monkeypatch):
    mod, utils = _build_frappe()
    monkeypatch.setitem(sys.modules, "frappe", mod)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)

    att = importlib.import_module("gege_hr.gege_hr.api.attendance")
    monkeypatch.setattr(
        att,
        "tz_utils",
        types.SimpleNamespace(
            now_in_portal=lambda: datetime(2026, 9, 11, 12, 0, 0),
            wall=lambda v: v.replace(tzinfo=None) if v.tzinfo else v,
            portal_now_str=lambda: "2026-09-11 12:00:00",
        ),
    )
    return att, mod


SHIFT = {
    "shift_type": "Ca chuẩn TT",
    "planned_start": "2026-09-11T08:00:00",
    "planned_end": "2026-09-11T17:00:00",
    "work_date": "2026-09-11",
}


def _at(h, m=0):
    return datetime(2026, 9, 11, h, m, 0)


# ── _window_violation: 4 hướng + trong cửa sổ ────────────────────────────────
def test_in_within_window_no_violation(env):
    att, _ = env
    assert att._window_violation(SHIFT, "IN", _at(8, 20)) is None  # 8:00–8:30 OK


def test_in_late_beyond_window(env):
    att, _ = env
    v = att._window_violation(SHIFT, "IN", _at(8, 35))
    assert v["type"] == "Late Check-in"
    assert v["minutes"] == 35


def test_in_early_beyond_window(env):
    att, _ = env
    v = att._window_violation(SHIFT, "IN", _at(6, 30))
    assert v["type"] == "Early Check-in (OT)"
    assert v["minutes"] == 90


def test_out_early_beyond_window(env):
    att, _ = env
    v = att._window_violation(SHIFT, "OUT", _at(15, 0))
    assert v["type"] == "Early Check-out"
    assert v["minutes"] == 120


def test_out_late_beyond_warning_limit(env):
    att, _ = env
    v = att._window_violation(SHIFT, "OUT", _at(23, 30))  # 17:00 + 360m = 23:00
    assert v["type"] == "Late Check-out (OT)"
    assert v["minutes"] == 390


def test_out_late_within_warning_no_violation(env):
    att, _ = env
    # 18:30 = trễ 90' nhưng ≤ 360' → checkout trễ cảnh báo, KHÔNG cần giải trình
    assert att._window_violation(SHIFT, "OUT", _at(18, 30)) is None


def test_no_shift_no_violation(env):
    att, _ = env
    assert att._window_violation(None, "IN", _at(8, 35)) is None


# ── explanation API: decide validations ─────────────────────────────────────
@pytest.fixture
def expl(monkeypatch):
    mod, utils = _build_frappe()
    monkeypatch.setitem(sys.modules, "frappe", mod)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    ex = importlib.import_module("gege_hr.gege_hr.api.explanation")
    monkeypatch.setattr(ex, "frappe", mod)  # rebind — module có thể đã cache stub cũ
    created = []

    class _Doc:
        def __init__(self, payload):
            self.__dict__.update(payload)
            self.doctype = payload.get("doctype")

        def insert(self, **kw):
            created.append(self)
            return self

        def save(self, **kw):
            self.saved = True
            return self

    monkeypatch.setattr(mod, "get_doc", lambda payload_or_name, name=None: _Doc(payload_or_name) if isinstance(payload_or_name, dict) else _loaded[(payload_or_name, name)])
    _loaded = {}
    return ex, _loaded, created, mod


def test_decide_rejects_bad_action(expl):
    ex, loaded, _, mod = expl
    with pytest.raises(_Thrown):
        ex.decide_explanation("EX-1", "delete")


def test_decide_missing_ticket(expl):
    ex, loaded, _, mod = expl
    with pytest.raises(_Thrown):
        ex.decide_explanation("EX-404", "approve")


def test_decide_approve_flow(expl):
    ex, loaded, _, mod = expl
    class _Loaded:
        name = "EX-1"
        status = "Open"
        saved = False
        decision_note = ""

        def save(self, **kw):
            self.saved = True
            return self

    doc = _Loaded()
    loaded[("VN Attendance Explanation", "EX-1")] = doc
    # decide_explanation checks frappe.db.exists before get_doc — seed store.
    mod.db.values[("VN Attendance Explanation", "EX-1")] = {"name": "EX-1"}
    out = ex.decide_explanation("EX-1", "approve", note="OK")
    assert doc.status == "Approved"
    assert doc.saved is True
    assert out["status"] == "Approved"
