"""Bench-free unit tests for ``api/leave_blackout.py``.

The pure decision engine (``utils/leave_blackout.evaluate_blackout``) is covered
by ``test_leave_blackout.py``. This file targets the bench-dependent wrappers
using a stub ``frappe`` injected into ``sys.modules`` (pattern shared with
``test_blackout_admin.py`` / ``test_admin_users.py``).

Coverage (plans/plan-blackout-desk-free.md §5.1):

  * ``evaluate_leave_blackout`` — employee → company resolution (legacy) and
    the BX5 regression: the employee leave flow must NOT hit the read gate.
  * ``blackout_periods`` — sort whitelist (BX1–BX2), branch/department filters
    (BX3), HR read gate (BX4), legacy bare-list shape (BX6).
  * ``create_blackout`` / ``update_blackout`` — duplicate-name guards +
    overlap ``force`` override (BC5–BC9).

The stub is registered only for the duration of each test via
``monkeypatch.setitem`` (auto-restored on teardown) so it never leaks into the
sibling bench-free tests that assert ``frappe`` is unimportable.
"""

import importlib
import sys
import types

import pytest


# --------------------------------------------------------------------------- #
# Stub frappe — enough surface for ``api/leave_blackout`` to import + run
# --------------------------------------------------------------------------- #
def _build_stub_frappe():
    mod = types.ModuleType("frappe")
    mod._ = lambda s: s
    mod.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)

    class _ValidationError(Exception):
        pass

    class _PermissionError(Exception):
        pass

    class _DoesNotExistError(Exception):
        pass

    class _DuplicateEntryError(Exception):
        pass

    mod.ValidationError = _ValidationError
    mod.PermissionError = _PermissionError
    mod.DoesNotExistError = _DoesNotExistError
    mod.DuplicateEntryError = _DuplicateEntryError

    def _throw(msg, exc=None):
        raise (exc or Exception)(str(msg))

    mod.throw = _throw
    mod.session = types.SimpleNamespace(user="hr@test.local")
    mod.get_roles = lambda user=None: ("HR Manager",)
    mod.log_error = lambda *a, **k: None
    mod.publish_realtime = lambda *a, **k: None
    mod.get_meta = lambda dt: None
    mod.response = types.SimpleNamespace(filename=None, filecontent=None, type=None)
    # Doc/delete surfaces are swapped per-test by ``make_fake`` (placeholders so
    # monkeypatch.setattr's existence check passes).
    mod.get_doc = lambda *a, **k: None
    mod.delete_doc = lambda doctype, name: None
    # Runtime db bits are swapped per-test.
    mod.db = None
    return mod


class _FakeDoc(dict):
    """Doc-like stub: attribute access + insert/save/delete recording."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)

    def set(self, key, value):
        self[key] = value

    def insert(self):
        self.setdefault("name", self.get("blackout_name") or "NEW-BLK")
        self["_store"]["inserted"].append(dict(self))
        return self

    def save(self):
        self["_store"]["saved"].append(dict(self))
        return self


class _FakeDB:
    """Captures ``get_value``/``get_all`` calls; routes per-doctype row fixtures."""

    def __init__(self):
        self.table_exists_flag = True
        self.company_for = "Gege Demo"  # get_value("Employee", ..., "company")
        self.get_value_calls = []
        self.get_all_rows = []  # VN Leave Blackout Period rows
        self.version_rows = []  # Version rows (blackout_versions)
        self.impact_rows = []  # Leave Application rows (blackout_impact)
        self.get_all_calls = []
        self.raise_on_get_value = False

    def table_exists(self, doctype):
        return self.table_exists_flag

    def get_value(self, doctype, name, field, as_dict=False):
        self.get_value_calls.append((doctype, name, field))
        if self.raise_on_get_value:
            raise RuntimeError("db down")
        if doctype == "Employee":
            return self.company_for
        if as_dict or isinstance(field, (list, tuple)):
            for row in self.get_all_rows:
                if row.get("name") == name or row.get("blackout_name") == name:
                    return dict(row)
            return None
        return None

    def exists(self, doctype, filters=None):
        if isinstance(filters, dict):
            target = filters.get("blackout_name")
            for row in self.get_all_rows:
                if row.get("blackout_name") == target:
                    return row.get("name")
            return None
        for row in self.get_all_rows:
            if row.get("name") == filters or row.get("blackout_name") == filters:
                return row.get("name")
        return None

    def get_all(
        self,
        doctype,
        filters=None,
        fields=None,
        or_filters=None,
        order_by=None,
        limit_start=None,
        limit_page_length=None,
    ):
        self.get_all_calls.append(
            {
                "doctype": doctype,
                "filters": filters,
                "fields": fields,
                "or_filters": or_filters,
                "order_by": order_by,
                "limit_start": limit_start,
                "limit_page_length": limit_page_length,
            }
        )
        if doctype == "Version":
            return list(self.version_rows)
        if doctype == "Leave Application":
            return list(self.impact_rows)
        return list(self.get_all_rows)


def make_fake(monkeypatch, roles=("HR Manager",)):
    """Register the stub frappe, import ``api/leave_blackout``, wire a fake db
    + doc factory. Shared with ``test_blackout_admin.py``."""
    stub = _build_stub_frappe()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    stub.get_roles = lambda user=None: tuple(roles)

    api = importlib.import_module("gege_hr.gege_hr.api.leave_blackout")
    monkeypatch.setattr(api, "frappe", stub)

    db = _FakeDB()
    monkeypatch.setattr(stub, "db", db)

    store = {"inserted": [], "saved": [], "deleted": [], "published": []}

    def _get_doc(arg, name=None):
        if isinstance(arg, dict):
            doc = _FakeDoc(arg)
            doc["_store"] = store
            return doc
        # frappe.get_doc(DOCTYPE, name) two-arg form, or get_doc(name) single.
        target = name if name is not None else arg
        for row in db.get_all_rows:
            if row.get("name") == target or row.get("blackout_name") == target:
                doc = _FakeDoc(dict(row))
                doc["_store"] = store
                return doc
        raise RuntimeError(f"{target} does not exist")

    monkeypatch.setattr(stub, "get_doc", _get_doc)
    monkeypatch.setattr(stub, "delete_doc", lambda doctype, name: store["deleted"].append(name))

    def _publish(event, payload=None):
        store["published"].append((event, payload))

    monkeypatch.setattr(stub, "publish_realtime", _publish)
    return types.SimpleNamespace(api=api, db=db, stub=stub, store=store)


@pytest.fixture
def fake(monkeypatch):
    return make_fake(monkeypatch)


def _rule(
    name="BLK-1",
    blackout_name="Tết",
    company="Gege Demo",
    frm="2026-06-01",
    to="2026-06-30",
    branch="",
    department="",
    leave_type="",
    action="Block",
    is_active=True,
):
    return {
        "name": name,
        "blackout_name": blackout_name,
        "company": company,
        "branch": branch,
        "department": department,
        "from_date": frm,
        "to_date": to,
        "applies_to_leave_type": leave_type,
        "is_active": is_active,
        "action": action,
        "reason": "cao điểm",
        "modified": "2026-06-01",
        "owner": "hr@test.local",
        "modified_by": "hr@test.local",
    }


def _last_call(fake):
    assert fake.db.get_all_calls, "expected a get_all call"
    return fake.db.get_all_calls[-1]


# --------------------------------------------------------------------------- #
# evaluate_leave_blackout — employee → company resolution (legacy regression)
# --------------------------------------------------------------------------- #
def test_evaluate_resolves_company_from_employee(fake):
    fake.db.company_for = "Gege Demo"
    decision = fake.api.evaluate_leave_blackout(
        from_date="2026-06-22",
        to_date="2026-06-22",
        employee="HR-EMP-0001",
    )
    # company resolved from Employee record then forwarded to the rule loader
    assert fake.db.get_value_calls == [("Employee", "HR-EMP-0001", "company")]
    filters = _last_call(fake)["filters"]
    assert ["company", "=", "Gege Demo"] in filters
    assert ["is_active", "=", 1] in filters
    # well-formed decision even with no rules
    assert decision["blocked"] is False
    assert decision["warnings"] == []


def test_evaluate_explicit_company_skips_employee_lookup(fake):
    decision = fake.api.evaluate_leave_blackout(
        from_date="2026-06-22",
        to_date="2026-06-22",
        company="Acme Co",
        employee="HR-EMP-0001",
    )
    # explicit company wins → no Employee lookup at all
    assert fake.db.get_value_calls == []
    assert ["company", "=", "Acme Co"] in _last_call(fake)["filters"]
    assert decision["blocked"] is False


def test_evaluate_swallows_get_value_error(fake):
    fake.db.raise_on_get_value = True
    # must not raise; company stays None and the engine still returns a decision
    decision = fake.api.evaluate_leave_blackout(
        from_date="2026-06-22",
        to_date="2026-06-22",
        employee="HR-EMP-0001",
    )
    filters = _last_call(fake)["filters"]
    assert not any(f[0] == "company" for f in filters)
    assert decision["blocked"] is False


def test_evaluate_delegates_block_rule_to_engine(fake):
    fake.db.get_all_rows = [_rule()]
    decision = fake.api.evaluate_leave_blackout(
        from_date="2026-06-22",
        to_date="2026-06-22",
        employee="HR-EMP-0001",
    )
    assert decision["blocked"] is True
    assert any("cấm nghỉ" in w for w in decision["warnings"])


# --------------------------------------------------------------------------- #
# blackout_periods — BX1–BX6 (sort whitelist / filters / gate / legacy shape)
# --------------------------------------------------------------------------- #
def test_bx1_order_by_whitelisted(fake):
    fake.api.blackout_periods(order_by="company", order_dir="asc")
    assert _last_call(fake)["order_by"] == "company asc, name desc"


def test_bx2_order_by_hostile_falls_back(fake):
    fake.api.blackout_periods(order_by="evil; drop table users", order_dir="desc")
    # hostile string never reaches order_by — whitelist fallback applies
    assert _last_call(fake)["order_by"] == "from_date desc, name desc"


def test_bx3_branch_department_filters(fake):
    fake.api.blackout_periods(branch="HNI", department="Kinh doanh")
    filters = _last_call(fake)["filters"]
    assert ["branch", "=", "HNI"] in filters
    assert ["department", "=", "Kinh doanh"] in filters


def test_bx4_read_gate_rejects_employee_role(monkeypatch):
    fake = make_fake(monkeypatch, roles=("Employee",))
    with pytest.raises(Exception, match="Yêu cầu quyền HR"):
        fake.api.blackout_periods()


def test_bx5_evaluate_has_no_read_gate(monkeypatch):
    """REGRESSION (sống còn): employees apply for leave without HR roles —
    ``evaluate_leave_blackout`` goes through the internal ``_list_blackouts``
    loader, never the gated HTTP wrapper."""
    fake = make_fake(monkeypatch, roles=("Employee",))
    decision = fake.api.evaluate_leave_blackout(
        from_date="2026-06-22",
        to_date="2026-06-22",
        employee="HR-EMP-0001",
    )
    assert decision["blocked"] is False
    assert fake.db.get_all_calls, "rule loader should still have run"


def test_bx6_legacy_call_keeps_bare_list_shape(fake):
    fake.db.get_all_rows = [_rule()]
    res = fake.api.blackout_periods()
    # legacy (no page_size) → bare list, rows normalised through blackout_row
    assert isinstance(res, list) and len(res) == 1
    assert res[0]["is_active"] is True
    assert res[0]["owner"] == "hr@test.local"
    # scope semantics: omitted is_active → NO active filter (SPA "Tất cả");
    # previously a null is_active silently narrowed to active-only.
    assert _last_call(fake)["filters"] == []


# --------------------------------------------------------------------------- #
# create_blackout / update_blackout — BC5–BC9 (dup-name + overlap force)
# --------------------------------------------------------------------------- #
def _create_kwargs(**overrides):
    kwargs = {
        "blackout_name": "Mới",
        "company": "Gege Demo",
        "from_date": "2026-06-10",
        "to_date": "2026-06-15",
        "reason": "thử nghiệm",
        "action": "Warning",
    }
    kwargs.update(overrides)
    return kwargs


def test_bc5_create_duplicate_name_throws_friendly(fake):
    fake.db.get_all_rows = [_rule(blackout_name="Tết")]
    with pytest.raises(Exception, match="đã tồn tại"):
        fake.api.create_blackout(**_create_kwargs(blackout_name="Tết"))
    assert fake.store["inserted"] == []


def test_bc6_update_renaming_onto_existing_name_throws(fake):
    fake.db.get_all_rows = [
        _rule(name="BLK-1", blackout_name="A"),
        _rule(name="BLK-2", blackout_name="B"),
    ]
    with pytest.raises(Exception, match="đã tồn tại"):
        fake.api.update_blackout("BLK-1", blackout_name="B")
    assert fake.store["saved"] == []


def test_bc7_create_overlap_without_force_throws(fake):
    fake.db.get_all_rows = [_rule(frm="2026-06-01", to="2026-06-30")]
    with pytest.raises(Exception, match="Chồng lấp"):
        fake.api.create_blackout(**_create_kwargs())
    assert fake.store["inserted"] == []


def test_bc8_create_overlap_with_force_succeeds(fake):
    fake.db.get_all_rows = [_rule(frm="2026-06-01", to="2026-06-30")]
    res = fake.api.create_blackout(**_create_kwargs(force=1))
    assert res["name"] == "Mới"
    assert len(fake.store["inserted"]) == 1


def test_bc9_create_no_overlap_needs_no_force(fake):
    fake.db.get_all_rows = [_rule(frm="2026-01-01", to="2026-01-31")]
    res = fake.api.create_blackout(**_create_kwargs())
    assert res["name"] == "Mới"
    assert len(fake.store["inserted"]) == 1
