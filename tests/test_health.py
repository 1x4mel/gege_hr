"""WP4 (prod-readiness-plan) — scheduler health-check unit tests.

  HC1  run_job_with_heartbeat records a heartbeat after success
  HC2  stale heartbeat (>max) → status red + alert_if_unhealthy notifies
  HC4  a THROWING job must NOT record a heartbeat
  HC3  check_health lists every registered job + error count

Bench-free stub-frappe harness.
"""

from __future__ import annotations

import datetime as dt
import importlib
import sys
import types

import pytest


class FrappeError(Exception):
    pass


class _Cache:
    def __init__(self):
        self.store = {}

    def set(self, key, value, ex=None, nx=False):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)


class StubFrappe:
    def __init__(self):
        self.cache_obj = _Cache()
        self.heartbeats = {}  # job_key → row dict
        self.notifications = []
        self.error_logs = ["e1", "e2", "e3"]  # 24h Error Log count (HC3)
        self.now = dt.datetime(2026, 8, 18, 12, 0, 0)

        outer = self

        class _DB:
            def get_value(inner, doctype, filters=None, fieldname=None, **_kw):
                if doctype == "VN Scheduler Heartbeat":
                    row = outer.heartbeats.get((filters or {}).get("job_key"))
                    if not row:
                        return None
                    if isinstance(fieldname, (list, tuple)):
                        return {f: row.get(f) for f in fieldname}
                    return row.get(fieldname)
                return None

            def set_value(inner, doctype, name, updates=None, *more, **_kw):
                for _key, row in outer.heartbeats.items():
                    if row.get("name") == name:
                        row.update(updates if isinstance(updates, dict) else {updates: more[0]})
                return None

            def count(inner, doctype, filters=None, **_kw):
                if doctype == "Error Log":
                    return len(outer.error_logs)
                return 0

            def commit(inner):
                return None

        self.db = _DB()

    def get_doc(self, arg, *a, **_kw):
        if isinstance(arg, dict):
            row = dict(arg)
            row.setdefault("name", f"HB-{len(self.heartbeats) + 1}")
            self.heartbeats[row["job_key"]] = row

            class _D:
                def __init__(s, r):
                    s.__dict__.update(r)

                def insert(s, **_kw):
                    return s

            return _D(row)
        raise FrappeError("unused")

    def log_error(self, title=None, message=None, *_a, **_kw):
        self.error_logs.append(title)
        return None


@pytest.fixture()
def health_mod(monkeypatch):
    stub = StubFrappe()
    frappe_mod = types.ModuleType("frappe")
    frappe_mod.db = stub.db
    frappe_mod.get_doc = stub.get_doc
    frappe_mod.log_error = stub.log_error
    frappe_mod.cache = lambda: stub.cache_obj
    utils = types.ModuleType("frappe.utils")
    utils.now = lambda: stub.now.strftime("%Y-%m-%d %H:%M:%S")
    frappe_mod.utils = utils
    frappe_mod.session = types.SimpleNamespace(user="hr@example.com")

    def get_all(doctype, filters=None, pluck=None, **_kw):
        # Has Role → HR Manager users
        if doctype == "Has Role":
            return ["hr@example.com", "admin@example.com"]
        if doctype == "Error Log":
            return [{"name": f"E{i}", "method": "m"} for i in range(3)]
        return []

    frappe_mod.get_all = get_all

    class _NotificationDoc:
        def __init__(self, payload):
            self.payload = payload
            stub.notifications.append(payload)

        def insert(self, **_kw):
            return self

    frappe_mod.get_doc_orig = stub.get_doc

    def get_doc(arg, *a, **kw):
        if isinstance(arg, dict) and arg.get("doctype") in ("Notification Log", "Notification"):
            return _NotificationDoc(arg)
        return stub.get_doc(arg, *a, **kw)

    frappe_mod.get_doc = get_doc

    monkeypatch.setitem(sys.modules, "frappe", frappe_mod)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    mod = importlib.reload(importlib.import_module("gege_hr.gege_hr.utils.health"))
    mod.frappe = frappe_mod
    # Freeze the clock at stub.now so staleness maths is deterministic.
    monkeypatch.setattr(mod, "_now", lambda: stub.now)
    return stub, mod


# --------------------------------------------------------------------------- #
# HC1 — success records heartbeat
# --------------------------------------------------------------------------- #
def test_hc1_success_records_heartbeat(health_mod):
    stub, mod = health_mod
    result = mod.run_job_with_heartbeat("checkout_miss.run_hourly", lambda: {"closed": 2})
    assert result == {"closed": 2}
    row = stub.heartbeats.get("checkout_miss.run_hourly")
    assert row is not None
    assert row["last_run"] == "2026-08-18 12:00:00"
    # cache also stamped
    assert stub.cache_obj.get("gege_hr:hb:checkout_miss.run_hourly")


# --------------------------------------------------------------------------- #
# HC4 — failure does NOT record
# --------------------------------------------------------------------------- #
def test_hc4_failure_leaves_heartbeat_untouched(health_mod):
    stub, mod = health_mod

    def boom():
        raise RuntimeError("worker died")

    with pytest.raises(RuntimeError):
        mod.run_job_with_heartbeat("checkout_miss.run_hourly", boom)
    assert "checkout_miss.run_hourly" not in stub.heartbeats
    snap = mod.check_health()
    job = next(j for j in snap["jobs"] if j["job_key"] == "checkout_miss.run_hourly")
    assert job["status"] == "red"  # never ran → red


# --------------------------------------------------------------------------- #
# HC2 — stale heartbeat → red + alert notification
# --------------------------------------------------------------------------- #
def _stamp_all_jobs(stub, minutes_ago_by_key=None):
    """Stamp every registered job fresh except the ones in minutes_ago_by_key."""
    from datetime import timedelta as _td

    for key in (
        "checkout_miss.run_hourly",
        "shift.generate_daily_shift_instances",
        "attendance.auto_mark_absent_job",
        "payroll.auto_close_payroll",
    ):
        minutes = (minutes_ago_by_key or {}).get(key, 1)
        ts = (stub.now - _td(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
        stub.cache_obj.set("gege_hr:hb:" + key, ts)


def test_hc2_stale_heartbeat_alerts_hr(health_mod):
    stub, mod = health_mod
    # only the hourly job is stale (3h > 120'); the rest are fresh
    _stamp_all_jobs(stub, {"checkout_miss.run_hourly": 180})
    res = mod.alert_if_unhealthy()
    assert res["alerted"] == 1
    assert stub.notifications, "HR Manager users must be notified"
    assert any("quá hạn" in (n.get("subject") or "") for n in stub.notifications)


def test_healthy_no_alert(health_mod):
    stub, mod = health_mod
    _stamp_all_jobs(stub)  # everything fresh
    res = mod.alert_if_unhealthy()
    assert res["alerted"] == 0
    assert not stub.notifications


# --------------------------------------------------------------------------- #
# HC3 — check_health lists all jobs + error count
# --------------------------------------------------------------------------- #
def test_hc3_snapshot_lists_all_jobs(health_mod):
    _, mod = health_mod
    snap = mod.check_health()
    keys = {j["job_key"] for j in snap["jobs"]}
    assert keys == set(mod.HEARTBEATS.keys())
    assert snap["error_log_24h"] == 3


def test_classify_boundaries(health_mod):
    _, mod = health_mod
    assert mod.classify("checkout_miss.run_hourly", None) == "red"  # never ran
    assert mod.classify("checkout_miss.run_hourly", 30) == "green"
    assert mod.classify("checkout_miss.run_hourly", 95) == "amber"  # >75% of 120
    assert mod.classify("checkout_miss.run_hourly", 130) == "red"


def test_ack_alert_marks_acknowledged(health_mod):
    stub, mod = health_mod
    mod.record_heartbeat("shift.generate_daily_shift_instances")
    ok = mod.ack_alert("shift.generate_daily_shift_instances", user="hr@example.com")
    assert ok
    row = stub.heartbeats["shift.generate_daily_shift_instances"]
    assert row["status"] == "Acked"
    assert row["acknowledged_by"] == "hr@example.com"
