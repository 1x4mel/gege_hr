"""PS14–PS20 — per-employee salary settings API (``payroll_master`` G10).

Bench-free wrapper tests, same stub-``frappe``-into-``sys.modules`` pattern as
``test_payroll_master.py`` (the harness here is extended with the db surface
the G10 endpoints need: filter-matching ``get_value``, ``set_value`` capture,
``count``, ``get_single_value``).
"""

import datetime
import importlib
import sys
import types

import pytest


class _FrappeError(Exception):
    pass


def _build_stub_frappe():
    mod = types.ModuleType("frappe")
    mod._ = lambda s: s
    mod.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)

    utils = types.ModuleType("frappe.utils")
    utils.getdate = lambda v=None: (
        datetime.date.today() if v in (None, "") else datetime.date.fromisoformat(str(v)[:10])
    )
    utils.today = lambda: datetime.date.today()
    utils.flt = lambda v, *a, **k: float(v) if v not in (None, "") else 0.0
    mod.utils = utils

    mod.db = None
    mod.get_doc = None
    mod.get_all = None
    mod.throw = lambda msg, exc=_FrappeError, *a, **k: (_ for _ in ()).throw(exc(msg))
    mod.log_error = lambda *a, **k: None
    return mod


class _FakeDoc:
    def __init__(self, payload, name=None):
        self.__dict__.update(payload)
        self.name = payload.get("name", name or "NEW-0001")
        self.inserted = False
        self.submitted = False

    def insert(self, ignore_permissions=False):
        self.inserted = True
        return self

    def submit(self):
        self.submitted = True
        return self


class _FakeDB:
    def __init__(self, rows=None):
        self.rows: dict[str, list[dict]] = rows or {}
        self.singles: dict[str, object] = {}
        self.set_calls: list[tuple] = []

    # -- matching ----------------------------------------------------------- #
    @staticmethod
    def _match(row, cond):
        rv = row.get(cond[0])
        op, val = cond[1], cond[2]
        if op == "=":
            return rv == val
        if op == "like":
            return val.strip("%").lower() in str(rv or "").lower()
        if op == "in":
            return rv in val
        if op == "<=":
            return str(rv or "") <= str(val)
        if op == ">=":
            return str(rv or "") >= str(val)
        return rv == val

    def _filtered(self, doctype, filters, or_filters=None):
        rows = list(self.rows.get(doctype, []))
        if filters:
            conds = (
                [f if isinstance(f, list) else [f, "=", filters[f]] for f in filters]
                if isinstance(filters, dict)
                else filters
            )
            rows = [r for r in rows if all(self._match(r, c) for c in conds)]
        if or_filters:
            rows = [r for r in rows if any(self._match(r, c) for c in or_filters)]
        return rows

    # -- API surface --------------------------------------------------------- #
    def exists(self, doctype, name):
        return any(r.get("name") == name or r.get("employee") == name for r in self.rows.get(doctype, []))

    def get_value(self, doctype, name, *args, as_dict=False, order_by=None, **kw):
        if isinstance(name, dict):
            rows = [
                r
                for r in self.rows.get(doctype, [])
                if all(self._match(r, c if isinstance(c, list) else [c, "=", name[c]]) for c in name)
            ]
        else:
            rows = [
                r for r in self.rows.get(doctype, []) if r.get("name") == name or r.get("employee") == name
            ]
        if order_by:
            field = order_by.split()[0]
            rows = sorted(rows, key=lambda r: str(r.get(field) or ""), reverse="desc" in order_by)
        if not rows:
            return {} if as_dict else None
        r = rows[0]
        if not args:
            return dict(r) if as_dict else r
        fields = [args[0]] if isinstance(args[0], str) else args[0]
        out = {f: r.get(f) for f in fields}
        return out if (as_dict or not isinstance(args[0], str)) else out[fields[0]]

    def get_all(
        self, doctype, filters=None, or_filters=None, fields=None, limit_page_length=None, limit_start=0, **kw
    ):
        rows = self._filtered(doctype, filters, or_filters)
        rows = (
            rows[int(limit_start or 0) :][: int(limit_page_length or 0)]
            if limit_page_length
            else rows[int(limit_start or 0) :]
        )
        if fields:
            rows = [{f: r.get(f) for f in fields} for r in rows]
        return rows

    def count(self, doctype, filters=None):
        return len(self._filtered(doctype, filters))

    def get_single_value(self, _dt, field):
        return self.singles.get(field)

    def set_value(self, doctype, name, values, **kw):
        self.set_calls.append((doctype, name, dict(values)))
        for r in self.rows.get(doctype, []):
            if r.get("name") == name or r.get("employee") == name:
                r.update(values)


@pytest.fixture
def fake(monkeypatch):
    stub = _build_stub_frappe()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", stub.utils)

    api = importlib.import_module("gege_hr.gege_hr.api.payroll_master")
    monkeypatch.setattr(api, "frappe", stub)
    payroll = importlib.import_module("gege_hr.gege_hr.utils.payroll")
    monkeypatch.setattr(payroll, "frappe", stub)

    db = _FakeDB()
    created: list[_FakeDoc] = []
    stub.db = db

    def _get_doc(payload_or_doctype, name=None):
        if name is not None:
            for d in created:
                if getattr(d, "name", None) == name:
                    return d
            raise KeyError(name)
        doc = _FakeDoc(payload_or_doctype, name=f"NEW-{len(created) + 1:04d}")
        created.append(doc)
        return doc

    stub.get_doc = _get_doc
    stub.get_all = db.get_all

    monkeypatch.setattr(api, "_require_hr_admin", lambda *a, **k: None)
    audit_calls: list[tuple] = []
    monkeypatch.setattr(api, "_audit_admin", lambda *a, **k: audit_calls.append((a, k)))
    monkeypatch.setattr(api, "_default_company", lambda: "GEGE")
    monkeypatch.setattr(api, "_company_for_employee", lambda e: "GEGE")

    class _H:
        pass

    h = _H()
    h.api = api
    h.db = db
    h.created = created
    h.audit_calls = audit_calls
    return h


def _seed_employees(h):
    h.db.rows["Employee"] = [
        {
            "name": "E1",
            "employee": "E1",
            "employee_name": "Nguyễn A",
            "department": "D1",
            "status": "Active",
            "company": "GEGE",
            "vn_payroll_mode": "Hourly",
            "vn_hourly_rate": 30000,
        },
        {
            "name": "E2",
            "employee": "E2",
            "employee_name": "Trần B",
            "department": "D1",
            "status": "Active",
            "company": "GEGE",
            "vn_payroll_mode": "",
            "vn_hourly_rate": 0,
        },
    ]
    h.db.rows["Department"] = [{"name": "D1", "vn_hourly_rate": 25000}]
    h.db.rows["Salary Structure"] = [{"name": "Lương CB", "is_active": "Yes", "docstatus": 1}]
    h.db.rows["Salary Structure Assignment"] = [
        {
            "name": "SSA-1",
            "employee": "E2",
            "docstatus": 1,
            "from_date": "2026-01-01",
            "base": 10_000_000,
            "salary_structure": "Lương CB",
        }
    ]


# --------------------------------------------------------------------------- #
# PS14 — list_employee_salary_settings
# --------------------------------------------------------------------------- #
def test_ps14_list_joins_resolver_and_ssa(fake):
    _seed_employees(fake)
    out = fake.api.list_employee_salary_settings()
    assert out["total"] == 2
    by_emp = {i["employee"]: i for i in out["items"]}
    # E1: own rate → source employee; no SSA → base 0.
    assert by_emp["E1"]["rate_source"] == "employee"
    assert by_emp["E1"]["effective_hourly_rate"] == 30000
    assert by_emp["E1"]["dept_rate"] == 25000
    assert by_emp["E1"]["monthly_base"] == 0
    # E2: no own rate → department fallback; SSA base joined.
    assert by_emp["E2"]["rate_source"] == "department"
    assert by_emp["E2"]["effective_hourly_rate"] == 25000
    assert by_emp["E2"]["monthly_base"] == 10_000_000
    assert by_emp["E2"]["ssa_name"] == "SSA-1"
    # Mode filter.
    only_monthly = fake.api.list_employee_salary_settings(mode="Monthly")
    assert only_monthly["total"] == 0
    # Search filter (or_filters).
    found = fake.api.list_employee_salary_settings(q="Nguyễn")
    assert [i["employee"] for i in found["items"]] == ["E1"]


# --------------------------------------------------------------------------- #
# PS15 — save: Hourly → Monthly with base (SSA created + audit)
# --------------------------------------------------------------------------- #
def test_ps15_save_switch_to_monthly_creates_ssa(fake):
    _seed_employees(fake)
    res = fake.api.save_employee_salary_setting(
        employee="E1",
        payroll_mode="Monthly",
        base=12_000_000,
        from_date="2026-07-01",
        salary_structure="Lương CB",
    )
    assert res["updated"]["vn_payroll_mode"] == "Monthly"
    assert res["updated"]["ssa"]  # NEW-xxxx
    doc = fake.created[0]
    assert doc.inserted and doc.submitted
    assert doc.base == 12_000_000
    # Employee fields persisted.
    assert ("Employee", "E1", {"vn_payroll_mode": "Monthly"}) in fake.db.set_calls
    # 2 audit rows: the SSA assign (engine) + the setting update itself.
    assert len(fake.audit_calls) == 2
    _args, kwargs = fake.audit_calls[1]
    # Old snapshot travels INSIDE new_value (real _audit_admin has no old_value).
    assert kwargs["new_value"]["old"]["payroll_mode"] == "Hourly"
    assert kwargs["new_value"]["monthly_base"] == 12_000_000


# --------------------------------------------------------------------------- #
# PS16 — save: Monthly without any base → Vietnamese throw
# --------------------------------------------------------------------------- #
def test_ps16_save_monthly_requires_base(fake):
    _seed_employees(fake)
    fake.db.rows["Employee"] = [dict(fake.db.rows["Employee"][1], name="E3", employee="E3")]
    with pytest.raises(_FrappeError) as ei:
        fake.api.save_employee_salary_setting(employee="E3", payroll_mode="Monthly")
    assert "lương tháng" in str(ei.value)


# --------------------------------------------------------------------------- #
# PS17 — save: new SSA overlapping the existing submitted one → engine throws
# --------------------------------------------------------------------------- #
def test_ps17_save_base_overlap_raises(fake):
    _seed_employees(fake)
    with pytest.raises(_FrappeError):
        fake.api.save_employee_salary_setting(
            employee="E2",
            base=11_000_000,
            from_date="2026-02-01",  # overlaps SSA-1 (from 2026-01-01, no end)
            salary_structure="Lương CB",
        )


# --------------------------------------------------------------------------- #
# PS18 — save: hourly rate only
# --------------------------------------------------------------------------- #
def test_ps18_save_hourly_rate(fake):
    _seed_employees(fake)
    res = fake.api.save_employee_salary_setting(employee="E2", hourly_rate=27000)
    assert res["updated"]["vn_hourly_rate"] == 27000
    assert ("Employee", "E2", {"vn_hourly_rate": 27000}) in fake.db.set_calls
    assert fake.audit_calls[0][1]["new_value"]["old"]["hourly_rate"] == 0
    assert fake.audit_calls[0][1]["new_value"]["hourly_rate"] == 27000


# --------------------------------------------------------------------------- #
# PS19 — bulk_set_hourly_rates buckets
# --------------------------------------------------------------------------- #
def test_ps19_bulk_buckets(fake):
    _seed_employees(fake)
    out = fake.api.bulk_set_hourly_rates(
        rows=[
            {"employee": "E1", "hourly_rate": 32000},
            {"employee": "NOPE", "hourly_rate": 1000},
            {"employee": "E2", "hourly_rate": -5},
        ]
    )
    assert out["updated"] == ["E1"]
    assert {s["employee"] for s in out["skipped"]} == {"NOPE", "E2"}
    assert any(c[1] == "E1" and c[2].get("vn_hourly_rate") == 32000 for c in fake.db.set_calls)


def test_ps19b_bulk_optional_mode(fake):
    _seed_employees(fake)
    out = fake.api.bulk_set_hourly_rates(rows=[{"employee": "E1", "hourly_rate": 1}], payroll_mode="Hourly")
    assert out["updated"] == ["E1"]
    assert any(c[2].get("vn_payroll_mode") == "Hourly" for c in fake.db.set_calls if c[1] == "E1")


# --------------------------------------------------------------------------- #
# PS20 — permission gate
# --------------------------------------------------------------------------- #
def test_ps20_permission_gate(fake, monkeypatch):
    _seed_employees(fake)

    def _deny(*_a, **_k):
        raise _FrappeError("chỉ HR admin")

    monkeypatch.setattr(fake.api, "_require_hr_admin", _deny)
    with pytest.raises(_FrappeError):
        fake.api.list_employee_salary_settings()
    with pytest.raises(_FrappeError):
        fake.api.save_employee_salary_setting(employee="E1", hourly_rate=1)
    with pytest.raises(_FrappeError):
        fake.api.bulk_set_hourly_rates(rows=[])
