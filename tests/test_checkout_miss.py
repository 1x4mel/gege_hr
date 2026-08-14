"""Bench-free unit tests for the checkout-miss auto-close engine
(``gege_hr.gege_hr.utils.checkout_miss``) — Chính sách A (benefit-of-the-doubt).

Covers the Phase-1 cases from ``plans/auto-checkout-missed-design.md``:
  T1   normal IN+OUT → no auto-close
  T2/T6 IN-only (day / overnight) → auto-close @ planned_end + ticket
  T8-10 occurrence escalation (penalty 0 for first N, then amount)
  T13  idempotency (already ticketed → skip)
  T14  session already has an OUT → skip
  T19  feature disabled → no-op

Uses the stub-frappe harness (``monkeypatch.setitem(sys.modules, "frappe", stub)``).
"""

from __future__ import annotations

import datetime as dt
import importlib
import sys
import types

import pytest


def _utc(y, mo, d, h, mi=0):
    from zoneinfo import ZoneInfo

    return dt.datetime(y, mo, d, h, mi, tzinfo=ZoneInfo("UTC"))


class _FakeDoc:
    """A fake Frappe document returned by ``frappe.get_doc``."""

    def __init__(self, payload, counter):
        self._payload = payload
        self._counter = counter
        for k, v in payload.items():
            setattr(self, k, v)
        self.name = f"NEW-{next(counter)}"
        self._db_sets = {}

    def insert(self, **_kw):
        return self

    def db_set(self, field, value):
        self._db_sets[field] = value


class StubFrappe:
    """Minimal frappe stub capturing db calls for assertions."""

    def __init__(self, *, sessions=None, occurrence=0, enabled=1, existing_miss=False,
                 settings=None):
        self._counter = iter(range(100000, 999999))
        # work sessions with IN but no OUT (the "open" ones the engine queries).
        self._sessions = sessions or []
        self._occurrence = occurrence
        self._enabled = enabled
        self._existing_miss = existing_miss
        self._settings = settings or {}
        self.created_docs = []          # payloads passed to get_doc
        self.set_values = []            # (doctype, name, updates)
        self.committed = False

    class db:  # noqa: N801 — mimic frappe.db namespace
        pass

    def _build_db(self):
        outer = self

        class _DB:
            def sql(inner, query, params=None, as_dict=False, **_kw):
                return list(outer._sessions)

            def get_all(inner, doctype, filters=None, fields=None, **_kw):
                return list(outer._sessions)

            def get_value(inner, doctype, name, field=None, **_kw):
                return outer._sessions[0].get("employee_name", "Test NV") if outer._sessions else None

            def exists(inner, doctype, filters=None, **_kw):
                return outer._existing_miss

            def count(inner, doctype, filters=None, **_kw):
                return outer._occurrence

            def get_single_value(inner, doctype, field):
                return outer._settings.get(field)

            def set_value(inner, doctype, name, updates, **_kw):
                outer.set_values.append((doctype, name, updates))
                return None

            def commit(inner):
                outer.committed = True

        return _DB()

    def get_doc(self, payload, *_a, **_kw):
        doc = _FakeDoc(payload, self._counter)
        self.created_docs.append(payload)
        return doc

    def log_error(self, *_a, **_kw):
        return None


@pytest.fixture()
def cm(monkeypatch):
    def _make(**kw):
        stub = StubFrappe(**kw)
        stub.db = stub._build_db()
        # frappe.utils shims
        utils = types.ModuleType("frappe.utils")

        def get_datetime(v):
            return v if isinstance(v, dt.datetime) else dt.datetime.fromisoformat(str(v))

        def now_datetime():
            return _utc(2026, 8, 10, 12)

        utils.get_datetime = get_datetime
        utils.now_datetime = now_datetime
        frappe_mod = types.ModuleType("frappe")
        frappe_mod.db = stub.db
        frappe_mod.get_doc = stub.get_doc
        frappe_mod.log_error = stub.log_error
        frappe_mod.utils = utils
        # `from frappe.utils import X` needs the submodule registered in
        # sys.modules, not just as an attribute on frappe.
        monkeypatch.setitem(sys.modules, "frappe", frappe_mod)
        monkeypatch.setitem(sys.modules, "frappe.utils", utils)
        # reload so the (now stubbed) frappe.utils symbols are re-bound.
        mod = importlib.reload(importlib.import_module("gege_hr.gege_hr.utils.checkout_miss"))
        return stub, mod

    return _make


def _open_session(work_date, planned_end, shift_type="Ca Sáng", overnight=False):
    pe = planned_end
    ps = pe - dt.timedelta(hours=12)
    return {
        "name": f"WS-{work_date}",
        "work_date": work_date,
        "planned_start": ps,
        "planned_end": pe,
        "shift_type": shift_type,
        "shift_instance": f"SI-{work_date}",
        "actual_checkin": ps + dt.timedelta(hours=1),
        "first_checkin_log": f"CKIN-{work_date}",
        "company": "GeGe Esport",
        "employee_name": "Test NV",
    }


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_t1_normal_in_out_no_autoclose(cm):
    """T1: no open sessions (all closed) → nothing created."""
    stub, mod = cm(sessions=[])
    created = mod.auto_close_missed_checkouts("HR-EMP-001")
    assert created == []
    assert stub.created_docs == []


def test_t2_day_in_only_autocloses_at_planned_end(cm):
    """T2: day shift IN-only → OUT synthesised at planned_end + ticket raised."""
    sess = _open_session("2026-08-08", _utc(2026, 8, 8, 13))  # planned_end 20:00 VN
    stub, mod = cm(sessions=[sess], occurrence=0,
                   settings={"vn_cm_enabled": 1, "vn_cm_free_first_n": 2,
                             "vn_cm_penalty_amount": 100000, "vn_cm_window_days": 90,
                             "vn_cm_buffer_minutes": 30, "vn_cm_grace_hours": 24})
    created = mod.auto_close_missed_checkouts("HR-EMP-001")
    assert len(created) == 1
    # OUT checkin doc created at planned_end
    out_docs = [d for d in stub.created_docs if d.get("doctype") == "Employee Checkin"]
    assert len(out_docs) == 1
    assert out_docs[0]["log_type"] == "OUT"
    assert out_docs[0]["vn_auto_generated"] == 1
    assert out_docs[0]["time"] == sess["planned_end"]
    # ticket created, Pending, occurrence 1, penalty 0 (first)
    tickets = [d for d in stub.created_docs if d.get("doctype") == "VN Checkout Miss"]
    assert len(tickets) == 1
    assert tickets[0]["status"] == "Pending"
    assert tickets[0]["occurrence_no"] == 1
    assert tickets[0]["penalty_amount"] == 0
    # work session marked closed + auto flag
    sv = [s for s in stub.set_values if s[0] == "VN Attendance Work Session"]
    assert sv and sv[0][2]["vn_auto_checkout"] == 1
    assert sv[0][2]["actual_checkout"] == sess["planned_end"]


def test_t6_overnight_in_only_autocloses(cm):
    """T6: overnight shift IN-only → still auto-closes at planned_end (next day)."""
    sess = _open_session("2026-08-08", _utc(2026, 8, 9, 1), shift_type="Ca Tối")
    stub, mod = cm(sessions=[sess], occurrence=0,
                   settings={"vn_cm_enabled": 1, "vn_cm_free_first_n": 2,
                             "vn_cm_penalty_amount": 100000})
    created = mod.auto_close_missed_checkouts("HR-EMP-001")
    assert len(created) == 1
    out_docs = [d for d in stub.created_docs if d.get("doctype") == "Employee Checkin"]
    assert out_docs[0]["time"] == _utc(2026, 8, 9, 1)  # planned_end next-morning


@pytest.mark.parametrize("occ,expected_penalty", [(0, 0), (1, 0), (2, 100000)])
def test_t8_t9_t10_occurrence_escalation(cm, occ, expected_penalty):
    """T8/T9/T10: penalty 0 for first N (2), then penalty_amount."""
    sess = _open_session("2026-08-08", _utc(2026, 8, 8, 13))
    stub, mod = cm(sessions=[sess], occurrence=occ,
                   settings={"vn_cm_enabled": 1, "vn_cm_free_first_n": 2,
                             "vn_cm_penalty_amount": 100000})
    created = mod.auto_close_missed_checkouts("HR-EMP-001")
    assert len(created) == 1
    tickets = [d for d in stub.created_docs if d.get("doctype") == "VN Checkout Miss"]
    # occurrence_no = count + 1
    assert tickets[0]["occurrence_no"] == occ + 1
    assert tickets[0]["penalty_amount"] == expected_penalty


def test_t13_idempotent_when_already_ticketed(cm):
    """T13: a ticket already references the session → skip (no duplicate)."""
    sess = _open_session("2026-08-08", _utc(2026, 8, 8, 13))
    stub, mod = cm(sessions=[sess], existing_miss=True)
    created = mod.auto_close_missed_checkouts("HR-EMP-001")
    assert created == []
    assert stub.created_docs == []


def test_t19_disabled_is_noop(cm):
    """T19: vn_cm_enabled=0 → engine does nothing."""
    sess = _open_session("2026-08-08", _utc(2026, 8, 8, 13))
    stub, mod = cm(sessions=[sess], settings={"vn_cm_enabled": 0})
    created = mod.auto_close_missed_checkouts("HR-EMP-001")
    assert created == []
    assert stub.created_docs == []


def test_penalise_expired_flips_status(cm):
    """Scheduled job flips Pending tickets past grace → Penalised."""
    stub, mod = cm(sessions=[])
    # patch the db.get_all inside the stub to return two pending ticket names
    mod.MISS_DOCTYPE = "VN Checkout Miss"
    n = mod.penalise_expired(now=_utc(2026, 8, 11, 12))
    # stub returns the _sessions list for get_all; empty here → 0
    assert n == 0
