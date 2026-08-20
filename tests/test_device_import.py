"""WP10 (prod-readiness-plan) — machine push endpoint ``device.device_import``.

Matrix (prefix DV):
  DV1  import sạch           → mọi dòng landed (imported == n)
  DV2  trùng (device,time,type) → duplicates đếm, KHÔNG tạo lại
  DV3  badge không map       → dòng vào invalid[] với lý do, batch vẫn tiếp tục
  DV4  giờ device TZ         → normalize được gọi với device timezone
  DV5  import lại idempotent → batch gửi lần 2 toàn duplicates
  +     secret sai → PermissionError; batch > 500 → ValidationError.

Bench-free stub-frappe harness.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest


class FrappeError(Exception):
    pass


class DuplicateEntryError(Exception):
    pass


class StubFrappe:
    def __init__(self):
        self.devices = {  # device_code → row
            "CAM-01": {
                "name": "DEV-CAM01",
                "device_secret": "s3cret",
                "timezone": "Asia/Ho_Chi_Minh",
            }
        }
        self.existing = set()  # (badge, time, type) đã lưu → DuplicateEntryError
        self.created_logs = []
        self.set_values = []
        self.rate_keys = []

        outer = self

        class _DB:
            def get_value(inner, doctype, filters=None, fieldname=None, as_dict=False, **_kw):
                if doctype == "VN Attendance Device":
                    code = filters.get("device_code") if isinstance(filters, dict) else None
                    row = outer.devices.get(code)
                    if not row:
                        return None
                    if isinstance(fieldname, (list, tuple)):
                        # plain dict — frappe._dict supports .get() the same way
                        return {f: row.get(f) for f in fieldname}
                    return row.get(fieldname)
                return None

            def set_value(inner, doctype, name, *a, **_kw):
                outer.set_values.append((doctype, name, a))
                return None

            def commit(inner):
                return None

        self.db = _DB()

    # -- frappe surface -----------------------------------------------------
    def throw(self, msg, exc=None):
        raise (exc or FrappeError)(msg)

    def log_error(self, *a, **k):
        return None

    def get_traceback(self):
        return "tb"


def _whitelist(*a, **k):
    def deco(fn):
        fn.whitelisted = True  # mirror the real frappe.whitelist semantics
        return fn

    if a and callable(a[0]):
        return deco(a[0])
    return deco


@pytest.fixture()
def dev(monkeypatch):
    stub = StubFrappe()

    frappe_mod = types.ModuleType("frappe")
    frappe_mod._ = lambda s, *a, **k: s
    frappe_mod.whitelist = _whitelist
    frappe_mod.throw = stub.throw
    frappe_mod.log_error = stub.log_error
    frappe_mod.get_traceback = stub.get_traceback
    frappe_mod.db = stub.db
    frappe_mod.PermissionError = FrappeError
    frappe_mod.ValidationError = FrappeError
    frappe_mod.DuplicateEntryError = DuplicateEntryError
    frappe_mod.session = types.SimpleNamespace(user="Guest")

    utils = types.ModuleType("frappe.utils")
    utils.now = lambda: "2026-08-18 08:00:00"
    frappe_mod.utils = utils

    monkeypatch.setitem(sys.modules, "frappe", frappe_mod)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)

    # ratelimit no-op under stub (frappe is None there)
    fake_rl = types.ModuleType("gege_hr.gege_hr.utils.ratelimit")
    fake_rl.rate_limit = lambda key, *a, **k: stub.rate_keys.append(key)
    monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.utils.ratelimit", fake_rl)

    mod = importlib.reload(importlib.import_module("gege_hr.gege_hr.api.device"))

    def fake_create(normalized, source_type="Device"):
        key = (
            normalized.get("raw_employee_code"),
            normalized["log_time"].strftime("%Y-%m-%d %H:%M:%S"),
            normalized["log_type"],
        )
        if key in stub.existing:
            raise DuplicateEntryError(key)
        stub.existing.add(key)
        stub.created_logs.append(normalized)

    monkeypatch.setattr(mod, "_create_raw_log", fake_create)
    return stub, mod


def _payload(*logs):
    return {"device_id": "CAM-01", "logs": list(logs)}


# DV1 — clean import
def test_dv1_clean_import(dev):
    stub, mod = dev
    res = mod.device_import(
        _payload(
            {"badge": "0123", "time": "2026-08-18 08:00:30", "type": "IN"},
            {"badge": "0123", "time": "2026-08-18 17:31:00", "type": "OUT"},
        ),
        device_secret="s3cret",
    )
    assert res["imported"] == 2
    assert res["duplicates"] == 0
    assert res["invalid"] == []
    assert stub.rate_keys == ["device_import:CAM-01"]


# DV2 + DV5 — duplicate rows / re-sent batch are no-ops
def test_dv2_dv5_duplicates_and_idempotent_resend(dev):
    stub, mod = dev
    batch = _payload({"badge": "0123", "time": "2026-08-18 08:00:30", "type": "IN"})
    first = mod.device_import(batch, device_secret="s3cret")
    assert first["imported"] == 1
    second = mod.device_import(batch, device_secret="s3cret")
    assert second["imported"] == 0
    assert second["duplicates"] == 1


# DV3 — unknown badge surfaces in invalid with a reason, batch continues
def test_dv3_unresolvable_row_is_reported_not_fatal(dev):
    stub, mod = dev
    res = mod.device_import(
        _payload(
            {"badge": "0123", "time": "2026-08-18 08:00:30", "type": "IN"},
            {"time": "2026-08-18 08:01:00", "type": "IN"},  # no badge at all
        ),
        device_secret="s3cret",
    )
    assert res["imported"] == 1
    assert len(res["invalid"]) == 1
    assert res["invalid"][0]["reason"]


# DV4 — device timezone reaches the normalizer (wrong-tz hours convert)
def test_dv4_device_timezone_used(dev, monkeypatch):
    stub, mod = dev
    seen = {}
    _orig = mod.normalize_upload_log

    def wrapper(raw, default_device_code=None, default_tz=None):
        seen["tz"] = default_tz
        return _orig(raw, default_device_code=default_device_code, default_tz=default_tz)

    monkeypatch.setattr(mod, "normalize_upload_log", wrapper)
    mod.device_import(
        _payload({"badge": "0123", "time": "2026-08-18 08:00:30", "type": "IN"}),
        device_secret="s3cret",
    )
    assert seen["tz"] == "Asia/Ho_Chi_Minh"


# secret sai → PermissionError
def test_wrong_secret_rejected(dev):
    _, mod = dev
    with pytest.raises(FrappeError) as ei:
        mod.device_import(_payload({"badge": "1", "time": "2026-08-18 08:00:00", "type": "IN"}), device_secret="nope")
    assert "secret" in str(ei.value).lower()


# batch quá lớn → chặn
def test_oversized_batch_rejected(dev):
    _, mod = dev
    big = _payload(*[{"badge": str(i), "time": f"2026-08-18 08:00:00", "type": "IN"} for i in range(501)])
    with pytest.raises(FrappeError) as ei:
        mod.device_import(big, device_secret="s3cret")
    assert "500" in str(ei.value)
