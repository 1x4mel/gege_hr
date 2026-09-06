"""Bench-free unit tests for the Payroll Period master API (G11, plan
payroll-periods-desk-free §5.1 — cases PP-01…PP-12).

Same stub-``frappe`` harness style as ``test_payroll_master``: the wrapper
layer of ``api/payroll_master.py`` (validation, overlap mapping, usage guards,
batched counting) is exercised without a bench; admin helpers are stubbed out.
"""

import datetime
import importlib
import sys
import types

import pytest


class _FrappeError(Exception):
    """Stand-in for frappe.exceptions.ValidationError used by frappe.throw."""


def _build_stub_frappe():
    mod = types.ModuleType("frappe")
    mod._ = lambda s: s
    mod.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)

    utils = types.ModuleType("frappe.utils")
    utils.getdate = lambda v=None: (
        datetime.date.today() if v in (None, "") else datetime.date.fromisoformat(str(v)[:10])
    )
    utils.flt = lambda v, *a, **k: float(v) if v not in (None, "") else 0.0
    mod.utils = utils

    mod.db = None
    mod.get_doc = None
    mod.get_all = lambda *a, **k: []
    mod.throw = lambda msg, exc=_FrappeError, *args, **kwargs: (_ for _ in ()).throw(exc(msg))
    mod.log_error = lambda *a, **k: None
    mod.delete_doc = None

    class _S:
        pass

    mod.session = _S()
    mod.session.user = "hr.demo@gege.demo"
    mod.local = _S()
    mod.local.request_ip = None
    return mod


class _FakeDoc:
    """Minimal Frappe document stub (insert/save + field get/set)."""

    def __init__(self, payload, name=None):
        self.__dict__.update(payload)
        self.name = payload.get("name", name or "NEW-0001")
        self.docstatus = payload.get("docstatus", 0)
        self.inserted = False
        self.saved = False

    def insert(self, ignore_permissions=False):
        self.inserted = True
        return self

    def save(self, ignore_permissions=False):
        self.saved = True
        return self

    def get(self, key, default=None):
        return getattr(self, key, default)


class _FakeDB:
    def __init__(self):
        self.exists_map = {}  # {(doctype, name): bool}
        self.commits = 0

    def exists(self, doctype, name):
        return self.exists_map.get((doctype, name), True)

    def commit(self):
        self.commits += 1


class _FakeFrappe:
    def __init__(self):
        self.db = _FakeDB()
        self.docs_created = []
        self.deleted = []
        self._next_id = 1
        self._existing = {}  # {(doctype, name): _FakeDoc}
        self._all_rows = {}
        self.get_all_calls = []  # [(doctype, kwargs)] — assertions PP-09/PP-10

    def get_doc(self, payload_or_doctype, name=None):
        if name is not None:
            return self._existing[(payload_or_doctype, name)]
        doc = _FakeDoc(payload_or_doctype, name=f"NEW-{self._next_id:04d}")
        self._next_id += 1
        self.docs_created.append(doc)
        return doc

    def delete_doc(self, doctype, name):
        self.deleted.append((doctype, name))
        self._existing.pop((doctype, name), None)
        return name

    def get_all(self, doctype, **kwargs):
        self.get_all_calls.append((doctype, kwargs))
        return self._all_rows.get(doctype, [])

    def throw(self, msg, exc=_FrappeError, *a, **k):
        raise exc(msg)


@pytest.fixture
def fake(monkeypatch):
    """Register the stub frappe, import payroll_master, wire a fake harness."""
    stub = _build_stub_frappe()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", stub.utils)

    api = importlib.import_module("gege_hr.gege_hr.api.payroll_master")
    monkeypatch.setattr(api, "frappe", stub)

    harness = _FakeFrappe()
    monkeypatch.setattr(stub, "db", harness.db)
    monkeypatch.setattr(stub, "get_doc", harness.get_doc)
    monkeypatch.setattr(stub, "get_all", harness.get_all)
    monkeypatch.setattr(stub, "throw", harness.throw)
    monkeypatch.setattr(stub, "delete_doc", harness.delete_doc)

    monkeypatch.setattr(api, "_require_hr_admin", lambda *a, **k: None)
    audit_calls = []
    monkeypatch.setattr(api, "_audit_admin", lambda *a, **k: audit_calls.append((a, k)))
    monkeypatch.setattr(api, "_default_company", lambda: "GEGE")

    harness.api = api
    harness.stub = stub
    harness.audit_calls = audit_calls
    harness.set_rows = lambda doctype, rows: harness._all_rows.__setitem__(doctype, rows)
    return harness


# --------------------------------------------------------------------------- #
# PP-01 — context (chưa có kỳ nào → 01-01 năm hiện tại + health missing)
# --------------------------------------------------------------------------- #
def test_pp01_context_first_period_defaults(fake):
    fake.set_rows("Company", [{"name": "GEGE", "abbr": "GG"}])
    fake.set_rows("Payroll Period", [])
    ctx = fake.api.payroll_period_context()
    today = datetime.date.today()
    assert ctx["suggested"]["start_date"] == f"{today.year}-01-01"
    assert ctx["suggested"]["end_date"] == f"{today.year}-12-31"
    assert ctx["health"] == "missing"
    assert ctx["active"] is None
    assert ctx["warning_days"] == 60
    assert ctx["companies"] == [{"name": "GEGE", "abbr": "GG"}]


def test_pp01b_context_suggested_follows_latest_end_date(fake):
    # Stub get_all bỏ qua filters — row phải đủ field cho mọi nhánh query.
    fake.set_rows(
        "Payroll Period",
        [
            {
                "name": "PP-2026",
                "company": "GEGE",
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
            }
        ],
    )
    ctx = fake.api.payroll_period_context()
    assert ctx["suggested"]["start_date"] == "2027-01-01"
    assert ctx["suggested"]["end_date"] == "2027-12-31"
    assert ctx["health"] == "ok"
    assert ctx["active"]["name"] == "PP-2026"
    assert ctx["active"]["days_remaining"] >= 0


# --------------------------------------------------------------------------- #
# PP-02 — save tạo hợp lệ
# --------------------------------------------------------------------------- #
def test_pp02_save_creates_period(fake):
    res = fake.api.save_payroll_period(
        company="GEGE", start_date="2027-01-01", end_date="2027-12-31", label="PP-2027"
    )
    assert res["name"] == "PP-2027"
    assert len(fake.docs_created) == 1
    assert fake.docs_created[0].inserted is True
    assert fake.audit_calls, "phải có audit sau khi tạo"
    assert fake.db.commits >= 1


# --------------------------------------------------------------------------- #
# PP-03 — thiếu end_date → throw sớm, không insert
# --------------------------------------------------------------------------- #
def test_pp03_save_requires_end_date(fake):
    with pytest.raises(_FrappeError):
        fake.api.save_payroll_period(company="GEGE", start_date="2027-01-01", end_date="")
    assert fake.docs_created == []


# --------------------------------------------------------------------------- #
# PP-04 — end < start → throw trước khi đụng controller
# --------------------------------------------------------------------------- #
def test_pp04_save_rejects_inverted_range(fake):
    with pytest.raises(_FrappeError):
        fake.api.save_payroll_period(
            company="GEGE", start_date="2027-12-31", end_date="2027-01-01"
        )
    assert fake.docs_created == []


# --------------------------------------------------------------------------- #
# PP-05 — overlap native (HTML link) → map tiếng Việt + overlap_name
# --------------------------------------------------------------------------- #
def test_pp05_overlap_error_mapped_to_vietnamese_without_html(fake):
    native = (
        "A Payroll Period exists between 01-01-2027 and 31-12-2027 "
        '( <b><a href="/app/Form/Payroll Period/PP-2027">PP-2027</a></b> ) for GEGE'
    )

    class _OverlapDoc(_FakeDoc):
        def insert(self, ignore_permissions=False):
            raise _FrappeError(native)

    fake._existing[("Payroll Period", "X")] = _OverlapDoc(
        {"doctype": "Payroll Period", "company": "GEGE"}, name="X"
    )
    fake.set_rows("Payroll Period", [{"name": "PP-2027"}])

    doc = _OverlapDoc({"doctype": "Payroll Period", "company": "GEGE"}, name="NEW-OV")
    monkey_get_doc = fake.get_doc

    def _get_doc(payload_or_doctype, name=None):
        if name is None:
            return doc
        return monkey_get_doc(payload_or_doctype, name)

    fake.stub.get_doc = _get_doc

    with pytest.raises(_FrappeError) as ei:
        fake.api.save_payroll_period(
            company="GEGE", start_date="2027-06-01", end_date="2028-05-31"
        )
    msg = str(ei.value)
    assert "PP-2027" in msg, "phải nêu tên kỳ giao nhau"
    assert "<a" not in msg and "/app/" not in msg, "không lộ HTML link Desk"


# --------------------------------------------------------------------------- #
# PP-06 — update kỳ đã bị EBA reference vẫn được sửa (chỉ delete bị chặn)
# --------------------------------------------------------------------------- #
def test_pp06_update_allowed_even_when_referenced(fake):
    fake.set_rows(
        "Employee Benefit Application", [{"payroll_period": "PP-2027", "n": 2}]
    )
    fake._existing[("Payroll Period", "PP-2027")] = _FakeDoc(
        {
            "doctype": "Payroll Period",
            "name": "PP-2027",
            "company": "GEGE",
            "start_date": "2027-01-01",
            "end_date": "2027-12-31",
        },
        name="PP-2027",
    )
    res = fake.api.save_payroll_period(
        name="PP-2027", company="GEGE", start_date="2027-01-01", end_date="2027-06-30"
    )
    assert res["end_date"] == "2027-06-30"
    assert fake._existing[("Payroll Period", "PP-2027")].saved is True


# --------------------------------------------------------------------------- #
# PP-07 — delete usage=0
# --------------------------------------------------------------------------- #
def test_pp07_delete_unused_period(fake):
    res = fake.api.delete_payroll_period("PP-2027")
    assert res == {"name": "PP-2027"}
    assert ("Payroll Period", "PP-2027") in fake.deleted
    assert fake.audit_calls


# --------------------------------------------------------------------------- #
# PP-08 — delete có 2 EBA + 1 Tax → chặn với tổng đúng
# --------------------------------------------------------------------------- #
def test_pp08_delete_blocked_by_usage(fake):
    fake.set_rows(
        "Employee Benefit Application", [{"payroll_period": "PP-2027", "n": 2}]
    )
    fake.set_rows(
        "Employee Tax Exemption Declaration", [{"payroll_period": "PP-2027", "n": 1}]
    )
    with pytest.raises(_FrappeError) as ei:
        fake.api.delete_payroll_period("PP-2027")
    assert "3" in str(ei.value)
    assert fake.deleted == []


# --------------------------------------------------------------------------- #
# PP-09 — list broad-search: or_filters 4 field + escape_like
# --------------------------------------------------------------------------- #
def test_pp09_list_broad_search_or_filters(fake):
    today = datetime.date.today()
    fake.set_rows(
        "Payroll Period",
        [
            {
                "name": "PP-ACTIVE",
                "company": "GEGE",
                "start_date": f"{today.year}-01-01",
                "end_date": f"{today.year}-12-31",
            },
            {
                "name": "PP-OLD",
                "company": "GEGE",
                "start_date": "2020-01-01",
                "end_date": "2020-12-31",
            },
        ],
    )
    rows = fake.api.list_payroll_periods(q="2027", company="GEGE")
    assert [r["name"] for r in rows] == ["PP-ACTIVE", "PP-OLD"]
    assert rows[0]["is_active_today"] == 1
    assert rows[1]["is_active_today"] == 0

    # Query chính là call "Payroll Period" (sau nó còn 2 call usage batch).
    pp_calls = [c for c in fake.get_all_calls if c[0] == "Payroll Period"]
    assert pp_calls, "phải có query list Payroll Period"
    doctype, kwargs = pp_calls[-1]
    or_filters = kwargs.get("or_filters") or []
    fields = [f[0] for f in or_filters]
    assert fields == ["name", "company", "start_date", "end_date"]
    assert all(f[1] == "like" and "%" in f[2] for f in or_filters)
    assert kwargs.get("filters") == [["company", "=", "GEGE"]]


# --------------------------------------------------------------------------- #
# PP-10 — usage đếm batch (đúng 2 query group_by, không N+1)
# --------------------------------------------------------------------------- #
def test_pp10_usage_is_two_grouped_queries(fake):
    fake.set_rows(
        "Employee Benefit Application",
        [{"payroll_period": "A", "n": 1}, {"payroll_period": "B", "n": 5}],
    )
    fake.set_rows("Employee Tax Exemption Declaration", [{"payroll_period": "A", "n": 2}])
    out = fake.api._pp_usage(["A", "B", "C"])
    assert out == {
        "A": {"benefit_applications": 1, "tax_declarations": 2},
        "B": {"benefit_applications": 5, "tax_declarations": 0},
        "C": {"benefit_applications": 0, "tax_declarations": 0},
    }
    usage_calls = [c for c in fake.get_all_calls if c[0] != "Payroll Period"]
    assert len(usage_calls) == 2
    assert all(c[1].get("group_by") == "payroll_period" for c in usage_calls)


# --------------------------------------------------------------------------- #
# PP-11 — guest (thiếu role) → mọi endpoint ném lỗi quyền
# --------------------------------------------------------------------------- #
def test_pp11_guest_blocked_on_all_endpoints(fake, monkeypatch):
    def _deny(*_a, **_k):
        raise _FrappeError("no permission")

    monkeypatch.setattr(fake.api, "_require_hr_admin", _deny)
    calls = [
        lambda: fake.api.payroll_period_context(),
        lambda: fake.api.list_payroll_periods(),
        lambda: fake.api.get_payroll_period("PP-2027"),
        lambda: fake.api.save_payroll_period(
            company="GEGE", start_date="2027-01-01", end_date="2027-12-31"
        ),
        lambda: fake.api.delete_payroll_period("PP-2027"),
    ]
    for call in calls:
        with pytest.raises(_FrappeError):
            call()


# --------------------------------------------------------------------------- #
# PP-12 — update path có previous dates trong audit
# --------------------------------------------------------------------------- #
def test_pp12_update_audits_previous_dates(fake):
    fake._existing[("Payroll Period", "PP-2027")] = _FakeDoc(
        {
            "doctype": "Payroll Period",
            "name": "PP-2027",
            "company": "GEGE",
            "start_date": "2027-01-01",
            "end_date": "2027-12-31",
        },
        name="PP-2027",
    )
    fake.api.save_payroll_period(
        name="PP-2027", company="GEGE", start_date="2027-01-01", end_date="2027-06-30"
    )
    _, kwargs = fake.audit_calls[-1]
    new_value = kwargs.get("new_value") or {}
    assert new_value.get("previous") == {
        "start_date": "2027-01-01",
        "end_date": "2027-12-31",
        "company": "GEGE",
    }
    assert new_value.get("end_date") == "2027-06-30"
