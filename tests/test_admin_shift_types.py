"""Bench-free unit tests for the Shift Type Frappe-parity endpoints in
``api/admin.py`` — mirrors the stub-frappe harness of ``test_shift_assignment.py``
/ ``test_admin_users.py`` (``monkeypatch.setitem(sys.modules, "frappe", stub)``).

Scope (plans/hr-shifts-frontend-parity.md §4.1):
  * ST-L* — ``list_shift_types`` envelope (search / trait / pagination / gate)
  * ST-G* — ``get_shift_type`` linked-usage counts + ``can`` matrix
  * ST-R* — ``rename_shift_type`` (frappe.rename_doc call, collision, blanks)
  * ST-D* — ``duplicate_shift_type`` (field copy, auto-suffix naming)
"""

from __future__ import annotations

import datetime
import importlib
import json
import sys
import types

import pytest

ADMIN_API = "gege_hr.gege_hr.api.admin"

VN_FIELDS = [
    "vn_is_overnight_shift",
    "vn_shift_duration_hours",
    "vn_earliest_checkin_minutes",
    "vn_latest_checkin_minutes",
    "vn_earliest_checkout_minutes",
    "vn_latest_checkout_minutes",
    "vn_max_checkout_after_end_minutes",
    "vn_allow_overtime_after_shift",
    "vn_allow_overtime_before_shift",
    "vn_max_overtime_hours",
    "vn_max_total_work_hours",
]


class _DotDict(dict):
    """Mirrors frappe._dict — attribute access on a dict."""

    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            return None

    def __setattr__(self, k, v):
        self[k] = v


class _FakeDoc:
    def __init__(self, payload):
        if isinstance(payload, dict):
            self.__dict__.update(payload)
        self.inserted = self.saved = False

    def insert(self, **k):
        self.inserted = True
        return self

    def save(self, **k):
        self.saved = True
        return self


class _FakeMeta:
    def __init__(self, fields):
        self._f = set(fields or [])
        self.allow_rename = 0  # HRMS default for Shift Type

    def has_field(self, name):
        return name in self._f


class _FakeDB:
    """Dict-backed DB: equality ``filters`` (AND) + ``or_filters`` (any-of,
    ``=`` / ``like`` substring), limit slicing and dict ``count`` — enough for
    the list/summary/linked-count code paths."""

    def __init__(self):
        self.rows: dict[str, list[dict]] = {}
        self.calls: list[dict] = []

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _eq_from(filters):
        if isinstance(filters, list):
            return {
                f[0]: f[2] for f in filters if isinstance(f, (list, tuple)) and len(f) == 3 and f[1] == "="
            }
        if isinstance(filters, dict):
            eq = {}
            for k, v in filters.items():
                # ["in", [..]] → membership list (handled in the row match).
                if isinstance(v, (list, tuple)) and len(v) == 2 and v[0] == "in":
                    eq[k] = v[1]
                elif not isinstance(v, (list, tuple)):
                    eq[k] = v
            return eq
        return {}

    @staticmethod
    def _clause_match(row, clause) -> bool:
        if not (isinstance(clause, (list, tuple)) and len(clause) == 3):
            return False
        field, op, value = clause
        rv = row.get(field)
        if op == "like":
            needle = str(value).strip("%").replace("\\%", "%").replace("\\_", "_")
            return needle.lower() in str(rv if rv is not None else "").lower()
        return rv == value

    # -- API surface ----------------------------------------------------------
    def get_all(self, doctype, **kw):
        self.calls.append({"doctype": doctype, **kw})
        base = [dict(r) for r in self.rows.get(doctype, [])]
        eq = self._eq_from(kw.get("filters"))
        if eq:
            base = [
                r
                for r in base
                if all((r.get(k) in v if isinstance(v, list) else r.get(k) == v) for k, v in eq.items())
            ]
        or_filters = kw.get("or_filters") or []
        if or_filters:
            base = [r for r in base if any(self._clause_match(r, c) for c in or_filters)]
        # Minimal GROUP BY support (the usage-count grouped query).
        group_by = kw.get("group_by")
        if group_by:
            gfield = str(group_by).split()[0]
            agg: dict = {}
            for r in base:
                agg[r.get(gfield)] = agg.get(r.get(gfield), 0) + 1
            return [_DotDict({gfield: k, "total": v}) for k, v in agg.items()]
        pluck = kw.get("pluck")
        if pluck:
            return [r.get(pluck) for r in base]
        # Faithful to real get_all: project to the REQUESTED fields only (so
        # summary light-rows don't leak unrequested vn_* columns).
        wanted = kw.get("fields")
        if wanted:
            base = [{f: r.get(f) for f in wanted} for r in base]
        limit_page_length = int(kw.get("limit_page_length") or 0)
        limit_start = int(kw.get("limit_start") or 0)
        if limit_page_length:
            base = base[limit_start : limit_start + limit_page_length]
        return [_DotDict(r) for r in base]

    def get_value(self, doctype, name, fields=None, as_dict=False):
        for r in self.rows.get(doctype, []):
            if r.get("name") == name:
                if as_dict:
                    return _DotDict({f: r.get(f) for f in (fields or [])})
                if isinstance(fields, str):
                    return r.get(fields)
                return {f: r.get(f) for f in (fields or [])}
        return None

    def count(self, doctype, filters=None):
        rows = self.rows.get(doctype, [])
        if isinstance(filters, dict) and filters:
            return len([r for r in rows if all(r.get(k) == v for k, v in filters.items())])
        return len(rows)

    def exists(self, doctype, name):
        # Property Setter existence checks pass a dict filter.
        if isinstance(name, dict):
            return any(all(r.get(k) == v for k, v in name.items()) for r in self.rows.get(doctype, []))
        return any(r.get("name") == name for r in self.rows.get(doctype, []))


class _Stub:
    def __init__(self, db, meta_fields=None):
        self._ = lambda s: s
        self.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)
        self.deny = False
        self.PermissionError = type("PermissionError", (Exception,), {})

        def _only_for(roles=None):
            if self.deny:
                raise self.PermissionError("not permitted")

        self.only_for = _only_for
        self.log_error = lambda *a, **k: None
        self.db = db
        self.get_all = db.get_all
        self.get_value = db.get_value
        self._meta = (
            ["start_time", "end_time", "holiday_list", "color", *VN_FIELDS]
            if meta_fields is None
            else list(meta_fields)
        )
        # One stable meta instance so tests can assert/flip allow_rename
        # (ST-L6 swaps it wholesale for the pre-migrate guard case).
        self._meta_obj = _FakeMeta(self._meta)
        self.get_meta = lambda dt: self._meta_obj
        self.clear_cache = lambda *a, **k: None
        self.property_setters: list[dict] = []

        def _make_property_setter(args, **k):
            self.property_setters.append(dict(args))
            self.db.rows.setdefault("Property Setter", []).append(
                {
                    "doc_type": args.get("doctype"),
                    "property": args.get("property"),
                    "value": args.get("value"),
                }
            )
            self._meta_obj.allow_rename = 1  # the setter takes effect

        self.make_property_setter = _make_property_setter

        def _rename(dt, old, new, **k):
            self.rename_calls.append((dt, old, new))
            # Mirror frappe.rename_doc's in-DB effect so chained renames
            # (ST-R4) resolve their exists() checks.
            for r in self.db.rows.get(dt, []):
                if r.get("name") == old:
                    r["name"] = new
                    r["shift_type"] = new

        self.rename_calls: list[tuple] = []
        self.rename_doc = _rename
        self.session = types.SimpleNamespace(user="hr.manager@gege.test")

    def get_doc(self, payload, name=None):
        if isinstance(payload, str):
            payload = {"doctype": payload, "name": name or payload}
        self.last_payload = dict(payload)
        doc = _FakeDoc(payload)
        doc.name = payload.get("shift_type") or payload.get("name") or "NEW-0001"
        self.last_doc = doc
        # Simulate insert + field:shift_type autoname landing in the DB so the
        # duplicate auto-suffix exists() checks resolve on repeat calls.
        if payload.get("doctype") == "Shift Type":
            row = {k: v for k, v in payload.items() if k != "doctype"}
            row["name"] = doc.name
            self.db.rows.setdefault("Shift Type", []).append(row)
        return doc

    def throw(self, msg, exc=Exception, *a, **k):
        raise exc(msg)


def _utils():
    utils = types.ModuleType("frappe.utils")
    utils.getdate = lambda v=None: (
        datetime.date.today() if v in (None, "") else datetime.date.fromisoformat(str(v)[:10])
    )

    def _add_days(v, days):
        return utils.getdate(v) + datetime.timedelta(days=days)

    utils.add_days = _add_days
    return utils


def _shift(name, start="08:00:00", end="17:00:00", overnight=0, pre=0, post=0, **kw):
    row = {
        "name": name,
        "start_time": start,
        "end_time": end,
        "holiday_list": None,
        "color": "Blue",
        "vn_is_overnight_shift": overnight,
        "vn_shift_duration_hours": 8,
        "vn_earliest_checkin_minutes": 60,
        "vn_latest_checkin_minutes": 30,
        "vn_earliest_checkout_minutes": 30,
        "vn_latest_checkout_minutes": 60,
        "vn_max_checkout_after_end_minutes": 360,
        "vn_allow_overtime_after_shift": post,
        "vn_allow_overtime_before_shift": pre,
        "vn_max_overtime_hours": 4,
        "vn_max_total_work_hours": 20,
    }
    row.update(kw)
    return row


@pytest.fixture
def admin(monkeypatch):
    db = _FakeDB()
    stub = _Stub(db)
    utils = _utils()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", utils)
    # Drop any cached binding so the import resolves against our stub.
    sys.modules.pop(ADMIN_API, None)
    mod = importlib.import_module(ADMIN_API)
    monkeypatch.setattr(mod, "frappe", stub)
    # Audit spy — assertions can check reference/new_value without a bench.
    audits: list[tuple] = []
    monkeypatch.setattr(mod, "_audit_admin", lambda *a, **k: audits.append((a, k)))
    return mod, stub, db, audits


def _seed(db):
    db.rows["Shift Type"] = [
        _shift("Ca Sáng"),
        _shift("Ca Đêm", "21:00:00", "09:00:00", overnight=1, post=1),
        _shift("Ca Chiều", "14:00:00", "22:00:00", pre=1),
    ]


# --------------------------------------------------------------------------- #
# ST-L — list_shift_types
# --------------------------------------------------------------------------- #
def test_st_l1_no_filter_returns_envelope(admin):
    mod, _stub, db, _audits = admin
    _seed(db)
    out = mod.list_shift_types(page=1, page_size=20)
    assert isinstance(out, dict)
    assert out["total"] == 3 and len(out["data"]) == 3
    assert out["summary"] == {"total": 3, "overnight": 1, "ot": 2}
    # Bare call (no page_size) keeps the legacy list shape.
    assert len(mod.list_shift_types()) == 3


def test_st_l2_search_filters_server_side(admin):
    mod, _stub, db, _audits = admin
    _seed(db)
    out = mod.list_shift_types(search="đêm", page_size=20)
    assert [r["name"] for r in out["data"]] == ["Ca Đêm"]
    assert out["total"] == 1


def test_st_l3_trait_filters(admin):
    mod, _stub, db, _audits = admin
    _seed(db)
    assert [r["name"] for r in mod.list_shift_types(trait="overnight", page_size=20)["data"]] == ["Ca Đêm"]
    assert sorted(r["name"] for r in mod.list_shift_types(trait="ot", page_size=20)["data"]) == [
        "Ca Chiều",
        "Ca Đêm",
    ]
    assert [r["name"] for r in mod.list_shift_types(trait="normal", page_size=20)["data"]] == ["Ca Sáng"]


def test_st_l4_pagination_slices_but_total_is_full_set(admin):
    mod, _stub, db, _audits = admin
    _seed(db)
    out = mod.list_shift_types(page=2, page_size=1)
    assert out["total"] == 3
    assert len(out["data"]) == 1


def test_st_l5_requires_hr_admin(admin):
    mod, stub, _db, _audits = admin
    stub.deny = True
    with pytest.raises(stub.PermissionError):
        mod.list_shift_types()


def test_st_l6_pre_migrate_meta_guard_drops_vn_fields(admin):
    mod, stub, db, _audits = admin
    stub._meta_obj = _FakeMeta(["start_time", "end_time", "holiday_list", "color"])
    db.rows["Shift Type"] = [_shift("Ca Sáng", overnight=1, post=1)]
    out = mod.list_shift_types(page_size=20)
    # VN custom fields absent → summary buckets safely fall back to 0.
    assert out["summary"] == {"total": 1, "overnight": 0, "ot": 0}


# --------------------------------------------------------------------------- #
# ST-G — get_shift_type
# --------------------------------------------------------------------------- #
def test_st_g1_detail_returns_linked_and_can(admin):
    mod, _stub, db, _audits = admin
    db.rows["Shift Type"] = [_shift("Ca Sáng")]
    db.rows["Shift Assignment"] = [
        {"name": "SA-1", "shift_type": "Ca Sáng", "status": "Active"},
        {"name": "SA-2", "shift_type": "Ca Sáng", "status": "Inactive"},
    ]
    db.rows["Employee"] = [{"name": "EMP-1", "default_shift": "Ca Sáng"}]
    db.rows["VN Employee Shift Instance"] = [{"name": "SI-1", "shift_type": "Ca Sáng"}]
    out = mod.get_shift_type("Ca Sáng")
    assert out["start_time"] == "08:00:00"
    assert out["linked"]["assignments_total"] == 2
    assert out["linked"]["assignments_active"] == 1
    assert out["linked"]["employees_default"] == 1
    assert out["linked"]["shift_instances"] == 1
    assert out["can"] == {"rename": True, "duplicate": True, "delete": False}


def test_st_g2_missing_shift_throws_vietnamese(admin):
    mod, _stub, _db, _audits = admin
    with pytest.raises(Exception, match="không tồn tại"):
        mod.get_shift_type("KHONG-TON-TAI")


def test_st_g3_unused_shift_is_deletable(admin):
    mod, _stub, db, _audits = admin
    db.rows["Shift Type"] = [_shift("Ca Trống")]
    out = mod.get_shift_type("Ca Trống")
    assert out["can"]["delete"] is True


# --------------------------------------------------------------------------- #
# ST-R — rename_shift_type
# --------------------------------------------------------------------------- #
def test_st_r1_rename_calls_rename_doc_and_audits_new_name(admin):
    mod, stub, db, audits = admin
    db.rows["Shift Type"] = [_shift("Ca A")]
    # HRMS allow_rename=0 → the endpoint must flip it via a Property Setter
    # (data-level customisation) BEFORE the canonical rename_doc call.
    assert stub._meta_obj.allow_rename == 0
    out = mod.rename_shift_type("Ca A", "Ca A Mới")
    assert out == {"name": "Ca A Mới"}
    # frappe.rename_doc does the Link-field cascade — we only hand it over.
    assert stub.rename_calls == [("Shift Type", "Ca A", "Ca A Mới")]
    assert len(stub.property_setters) == 1
    assert stub.property_setters[0]["property"] == "allow_rename"
    assert stub.property_setters[0]["value"] == "1"
    assert audits and audits[0][1]["reference_name"] == "Ca A Mới"
    assert audits[0][1]["new_value"] == {"old_name": "Ca A", "new_name": "Ca A Mới"}


def test_st_r4_allow_rename_setter_created_once(admin):
    mod, stub, db, _audits = admin
    db.rows["Shift Type"] = [_shift("Ca A")]
    mod.rename_shift_type("Ca A", "Ca B")
    mod.rename_shift_type("Ca B", "Ca C")
    # Second rename: the Property Setter already exists → no duplicate write.
    assert len(stub.property_setters) == 1
    assert stub.rename_calls == [("Shift Type", "Ca A", "Ca B"), ("Shift Type", "Ca B", "Ca C")]


def test_st_r2_rename_to_existing_name_throws(admin):
    mod, _stub, db, _audits = admin
    _seed(db)
    with pytest.raises(Exception, match="đã tồn tại"):
        mod.rename_shift_type("Ca Sáng", "Ca Đêm")


def test_st_r3_rename_blank_and_noop(admin):
    mod, stub, db, _audits = admin
    db.rows["Shift Type"] = [_shift("Ca A")]
    with pytest.raises(Exception, match="không được để trống"):
        mod.rename_shift_type("Ca A", "   ")
    with pytest.raises(Exception, match="không tồn tại"):
        mod.rename_shift_type("KHONG-TON-TAI", "Bất Kỳ")
    # Same name → idempotent no-op (no rename_doc, no audit).
    assert mod.rename_shift_type("Ca A", "Ca A") == {"name": "Ca A"}
    assert stub.rename_calls == []


# --------------------------------------------------------------------------- #
# ST-D — duplicate_shift_type
# --------------------------------------------------------------------------- #
def test_st_d1_duplicate_copies_fields_and_inserts(admin):
    mod, stub, db, audits = admin
    db.rows["Shift Type"] = [_shift("Ca A", post=1, color="Red")]
    out = mod.duplicate_shift_type("Ca A")
    assert out["name"] == "Ca A (bản sao)"
    doc = stub.last_doc
    assert doc.inserted
    assert doc.name == "Ca A (bản sao)"
    # The site's Shift Type autoname is PROMPT — the insert payload must carry
    # the name explicitly or insert raises "Please set the document name".
    assert stub.last_payload.get("name") == "Ca A (bản sao)"
    assert doc.start_time == "08:00:00"
    assert doc.color == "Red"
    assert doc.vn_allow_overtime_after_shift == 1
    assert audits and audits[0][1]["new_value"] == {"duplicated_from": "Ca A"}


def test_st_d2_duplicate_twice_auto_suffixes(admin):
    mod, _stub, db, _audits = admin
    db.rows["Shift Type"] = [_shift("Ca A")]
    assert mod.duplicate_shift_type("Ca A")["name"] == "Ca A (bản sao)"
    assert mod.duplicate_shift_type("Ca A")["name"] == "Ca A (bản sao) 2"
    # Explicit collision still throws instead of suffixing.
    with pytest.raises(Exception, match="đã tồn tại"):
        mod.duplicate_shift_type("Ca A", "Ca A (bản sao)")


def test_st_l7_page_rows_carry_assignments_active(admin):
    mod, _stub, db, _audits = admin
    _seed(db)
    db.rows["Shift Assignment"] = [
        {"name": "SA-1", "shift_type": "Ca Sáng", "status": "Active", "docstatus": 1},
        {"name": "SA-2", "shift_type": "Ca Sáng", "status": "Active", "docstatus": 1},
        {"name": "SA-3", "shift_type": "Ca Sáng", "status": "Inactive", "docstatus": 1},
    ]
    out = mod.list_shift_types(page_size=20)
    by_name = {r["name"]: r for r in out["data"]}
    assert by_name["Ca Sáng"]["assignments_active"] == 2  # Active only
    assert by_name["Ca Đêm"]["assignments_active"] == 0


def test_st_v1_versions_parsed_from_version_doctype(admin):
    mod, _stub, db, _audits = admin
    db.rows["Shift Type"] = [_shift("Ca A")]
    db.rows["Version"] = [
        {
            "name": "VER-1",
            "creation": "2026-08-28 10:00:00",
            "owner": "hr@x.vn",
            "ref_doctype": "Shift Type",
            "docname": "Ca A",
            "data": json.dumps({"changed": [["start_time", "07:00:00", "08:00:00"]]}),
        },
        {
            "name": "VER-2",
            "creation": "2026-08-27 09:00:00",
            "owner": "hr@x.vn",
            "ref_doctype": "Shift Type",
            "docname": "Ca A",
            "data": "not-json",
        },
    ]
    out = mod.shift_type_versions("Ca A")
    v1 = next(v for v in out if v["name"] == "VER-1")
    assert v1["fields"] == ["start_time"]
    assert v1["owner"] == "hr@x.vn"
    v2 = next(v for v in out if v["name"] == "VER-2")
    assert v2["fields"] == []  # corrupt data JSON degrades to empty fields


def test_st_v2_versions_missing_shift_throws(admin):
    mod, _stub, _db, _audits = admin
    with pytest.raises(Exception, match="không tồn tại"):
        mod.shift_type_versions("KHONG-TON-TAI")


def test_st_l7_page_rows_carry_assignments_active(admin):
    mod, _stub, db, _audits = admin
    _seed(db)
    db.rows["Shift Assignment"] = [
        {"name": "SA-1", "shift_type": "Ca Sáng", "status": "Active", "docstatus": 1},
        {"name": "SA-2", "shift_type": "Ca Sáng", "status": "Active", "docstatus": 1},
        {"name": "SA-3", "shift_type": "Ca Sáng", "status": "Inactive", "docstatus": 1},
    ]
    out = mod.list_shift_types(page_size=20)
    by_name = {r["name"]: r for r in out["data"]}
    assert by_name["Ca Sáng"]["assignments_active"] == 2  # Active only
    assert by_name["Ca Đêm"]["assignments_active"] == 0


def test_st_v1_versions_parsed_from_version_doctype(admin):
    mod, _stub, db, _audits = admin
    db.rows["Shift Type"] = [_shift("Ca A")]
    db.rows["Version"] = [
        {
            "name": "VER-1",
            "creation": "2026-08-28 10:00:00",
            "owner": "hr@x.vn",
            "ref_doctype": "Shift Type",
            "docname": "Ca A",
            "data": json.dumps({"changed": [["start_time", "07:00:00", "08:00:00"]]}),
        },
        {
            "name": "VER-2",
            "creation": "2026-08-27 09:00:00",
            "owner": "hr@x.vn",
            "ref_doctype": "Shift Type",
            "docname": "Ca A",
            "data": "not-json",
        },
    ]
    out = mod.shift_type_versions("Ca A")
    v1 = next(v for v in out if v["name"] == "VER-1")
    assert v1["fields"] == ["start_time"]
    assert v1["owner"] == "hr@x.vn"
    v2 = next(v for v in out if v["name"] == "VER-2")
    assert v2["fields"] == []  # corrupt data JSON degrades to empty fields


def test_st_v2_versions_missing_shift_throws(admin):
    mod, _stub, _db, _audits = admin
    with pytest.raises(Exception, match="không tồn tại"):
        mod.shift_type_versions("KHONG-TON-TAI")


def test_st_d1b_duplicate_missing_source_throws(admin):
    mod, _stub, _db, _audits = admin
    with pytest.raises(Exception, match="không tồn tại"):
        mod.duplicate_shift_type("KHONG-TON-TAI")
