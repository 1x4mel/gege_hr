"""Bench-free unit tests for ``api/benefits_admin.py``.

NEW-6 (hr-gap-audit 🟥) + plan-benefits-desk-free §5.1 (B1–B10, B22, B24).
Harness stub theo pattern test_recruitment.py (op-aware get_all + get_doc +
copy_doc + frappe.call dotted-path cho helper HRMS).
"""

import importlib
import sys
import types

import pytest


def _match(rv, op, val):
    if op == "in":
        return rv in (val or [])
    if op == "like":
        return str(val or "").strip("%").lower() in str(rv or "").lower()
    if op == ">=":
        return (rv or "") >= (val or "")
    if op == "<=":
        return (rv or "") <= (val or "")
    if op == "!=":
        return rv != val
    return rv == val


class _Doc:
    def __init__(self, doctype, store):
        self.doctype = doctype
        self._store = store
        self.name = None
        self.docstatus = 0
        self.amended_from = None

    def insert(self, ignore_permissions=False):
        self.name = self.name or f"{self.doctype.replace(' ', '-')}-{len(self._store) + 1:04d}"
        self._store[(self.doctype, self.name)] = self
        return self

    def save(self):
        if not self.name or (self.doctype, self.name) not in self._store:
            return self.insert()
        return self

    def submit(self):
        self.docstatus = 1
        return self

    def cancel(self):
        self.docstatus = 2
        return self

    def append(self, key, row):
        if not isinstance(getattr(self, key, None), list):
            setattr(self, key, [])
        getattr(self, key).append(row)
        return self


class _Frappe:
    def __init__(self):
        self.store = {}
        self.list_rows = {}
        self.values = {}  # (doctype, name, field) -> value (docs not in store)
        self.employee_for_user = "HR-EMP-1"
        self.roles = {"HR Manager"}
        self.call_returns = {}  # dotted path -> value | callable

    def whitelist(self, fn=None, **k):
        return fn if fn is not None else (lambda f: f)

    def _(self, s):
        return s

    def log_error(self, *a, **k):
        return None

    def throw(self, msg, *a, **k):
        raise Exception(msg)

    def call(self, path, **kwargs):
        rv = self.call_returns.get(path)
        return rv(**kwargs) if callable(rv) else rv

    @property
    def session(self):
        return types.SimpleNamespace(user="hr@gege.local")

    def get_roles(self, user):
        return set(self.roles)

    def get_doc(self, doctype, name=None):
        if isinstance(doctype, dict):
            doc = self.new_doc(doctype.get("doctype") or "")
            for key, val in doctype.items():
                setattr(doc, key, val)
            return doc
        doc = self.store.get((doctype, name))
        if doc is None:
            raise Exception(f"{doctype} {name} not found")
        return doc

    def copy_doc(self, doc):
        new = _Doc(doc.doctype, self.store)
        for key, val in vars(doc).items():
            if key.startswith("_") or key in ("name",):
                continue
            setattr(new, key, val)
        new.name = None
        new.docstatus = 0
        new.amended_from = None
        return new

    def new_doc(self, doctype):
        return _Doc(doctype, self.store)

    class _DB:
        def __init__(self, fr):
            self.fr = fr

        def get_value(self, doctype, name, field=None, as_dict=False):
            if doctype == "Employee" and isinstance(name, dict):
                return self.fr.employee_for_user
            doc = self.fr.store.get((doctype, name))
            if doc is not None and isinstance(field, str):
                return getattr(doc, field, None)
            return self.fr.values.get((doctype, name, field))

    @property
    def db(self):
        return self._DB(self)

    @property
    def utils(self):
        return types.SimpleNamespace(today=lambda: "2026-08-08")

    def get_all(
        self,
        doctype,
        filters=None,
        or_filters=None,
        fields=None,
        order_by=None,
        limit_start=0,
        limit_page_length=0,
        pluck=None,
        distinct=None,
        **k,
    ):
        rows = list(self.list_rows.get(doctype, []))

        def conds(items):
            if isinstance(items, dict):
                return [[key, "=", val] for key, val in items.items()]
            return [c for c in (items or []) if not isinstance(c, str)]

        def keep(r):
            for cond in conds(filters):
                if not _match(r.get(cond[0]), cond[1], cond[2]):
                    return False
            if or_filters:
                if not any(_match(r.get(c[0]), c[1], c[2]) for c in conds(or_filters)):
                    return False
            return True

        rows = [r for r in rows if keep(r)]
        if limit_page_length:
            rows = rows[int(limit_start or 0) : int(limit_start or 0) + int(limit_page_length)]
        if pluck:
            return [r.get(pluck) for r in rows]
        if fields:
            return [{f: r.get(f) for f in fields} for r in rows]
        return rows


@pytest.fixture
def mod(monkeypatch):
    stub = _Frappe()
    monkeypatch.setitem(sys.modules, "frappe", stub)
    m = importlib.import_module("gege_hr.gege_hr.api.benefits_admin")
    importlib.reload(m)
    return m, stub


# ── Legacy (list + gratuity gate + promotion bare-create — giữ đến P4) ───────


def test_my_benefit_applications_filters_own(mod):
    m, stub = mod
    stub.list_rows["Employee Benefit Application"] = [
        {"name": "BA-1", "employee": "HR-EMP-1"},
        {"name": "BA-2", "employee": "HR-EMP-2"},
    ]
    res = m.my_benefit_applications()
    assert res["total"] == 1
    assert res["data"][0]["status"] == "Draft"  # docstatus-mapped label


def test_all_benefit_requires_manager(mod):
    m, stub = mod
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.all_benefit_applications()


def test_list_gratuities_requires_manager(mod):
    m, stub = mod
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.list_gratuities()


def test_promotion_validates(mod):
    m, _ = mod
    with pytest.raises(Exception, match="thuộc tính"):
        m.save_promotion(payload={"employee": "HR-EMP-1"})


def test_promotion_requires_manager(mod):
    m, stub = mod
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.save_promotion(
            payload={"employee": "HR-EMP-1", "details": [{"fieldname": "designation", "new_value": "X"}]}
        )


# ── B24: endpoint cũ broken đã xoá ───────────────────────────────────────────


def test_b24_old_endpoints_removed(mod):
    m, _ = mod
    assert not hasattr(m, "submit_benefit_application")
    assert not hasattr(m, "create_promotion")


# ── B1–B3: benefit_context ───────────────────────────────────────────────────


def _seed_context(stub):
    stub.values[("Employee", "HR-EMP-1", "company")] = "GEGE"
    stub.values[("Salary Structure", "SS-1", "max_benefits")] = 12_000_000
    stub.list_rows["Salary Structure Assignment"] = [
        {"employee": "HR-EMP-1", "from_date": "2026-01-01", "docstatus": 1, "salary_structure": "SS-1"},
        {"employee": "HR-EMP-9", "from_date": "2026-01-01", "docstatus": 1, "salary_structure": "SS-9"},
    ]
    stub.list_rows["Salary Detail"] = [
        {
            "parent": "SS-1",
            "parentfield": "earnings",
            "is_flexible_benefit": 1,
            "salary_component": "Meal Card",
        },
        {"parent": "SS-1", "parentfield": "earnings", "is_flexible_benefit": 1, "salary_component": "Fuel"},
        {"parent": "SS-1", "parentfield": "deductions", "is_flexible_benefit": 1, "salary_component": "Bad"},
        {"parent": "SS-1", "parentfield": "earnings", "is_flexible_benefit": 0, "salary_component": "Basic"},
    ]
    stub.values[("Salary Component", "Meal Card", "max_benefit_amount")] = 3_000_000
    stub.values[("Salary Component", "Meal Card", "pay_against_benefit_claim")] = 1
    stub.values[("Salary Component", "Meal Card", "depends_on_payment_days")] = 0
    stub.values[("Salary Component", "Fuel", "max_benefit_amount")] = 2_000_000
    stub.values[("Salary Component", "Fuel", "pay_against_benefit_claim")] = 0
    stub.list_rows["Payroll Period"] = [
        {"name": "PP-2026", "start_date": "2026-01-01", "end_date": "2026-12-31", "company": "GEGE"},
    ]
    stub.call_returns[
        "hrms.payroll.doctype.employee_benefit_application."
        "employee_benefit_application.get_max_benefits_remaining"
    ] = 9_000_000


def test_b1_benefit_context_full(mod):
    m, stub = mod
    _seed_context(stub)
    res = m.benefit_context()
    assert res["payroll_period"]["name"] == "PP-2026"
    assert res["max_benefits"] == 12_000_000
    assert res["remaining"] == 9_000_000
    names = [c["name"] for c in res["components"]]
    assert names == ["Meal Card", "Fuel"]  # chỉ earnings flexi của SS-1
    meal = res["components"][0]
    assert meal["max_benefit_amount"] == 3_000_000
    assert meal["pay_against_benefit_claim"] == 1
    assert res["can"]["create"] is True
    assert res["existing_application"] is None


def test_b2_benefit_context_empty_site(mod):
    m, _ = mod
    res = m.benefit_context()
    assert res["max_benefits"] == 0
    assert res["components"] == []
    assert res["payroll_period"] is None
    assert res["remaining"] == 0
    assert res["can"]["create"] is False


def test_b3_benefit_context_existing_application(mod):
    m, stub = mod
    _seed_context(stub)
    stub.list_rows["Employee Benefit Application"] = [
        {"name": "BA-1", "employee": "HR-EMP-1", "payroll_period": "PP-2026", "docstatus": 1},
    ]
    res = m.benefit_context()
    assert res["existing_application"] == {"name": "BA-1", "docstatus": 1}
    assert res["can"]["create"] is False
    assert res["can"]["cancel"] is True  # HR Manager mặc định


# ── B4–B6: save_benefit_application ──────────────────────────────────────────


def _save_payload(**over):
    payload = {
        "employee": "HR-EMP-1",
        "payroll_period": "PP-2026",
        "benefits": [{"earning_component": "Meal Card", "amount": 500_000}],
    }
    payload.update(over)
    return payload


def test_b4_save_requires_rows(mod):
    m, _ = mod
    with pytest.raises(Exception, match="phúc lợi"):
        m.save_benefit_application(payload=_save_payload(benefits=[]))


def test_b5_save_creates_with_rows(mod):
    m, stub = mod
    stub.values[("Salary Component", "Meal Card", "max_benefit_amount")] = 3_000_000
    stub.values[("Salary Component", "Meal Card", "pay_against_benefit_claim")] = 1
    res = m.save_benefit_application(payload=_save_payload())
    assert res["name"]
    assert res["status"] == "Draft"
    assert res["total_amount"] == 500_000
    doc = stub.store[("Employee Benefit Application", res["name"])]
    assert doc.payroll_period == "PP-2026"
    assert len(doc.employee_benefits) == 1
    assert doc.employee_benefits[0]["max_benefit_amount"] == 3_000_000
    assert doc.employee_benefits[0]["pay_against_benefit_claim"] == 1


def test_b6_save_update_submitted_throws(mod):
    m, stub = mod
    stub.values[("Salary Component", "Meal Card", "max_benefit_amount")] = 0
    stub.values[("Salary Component", "Meal Card", "pay_against_benefit_claim")] = 0
    res = m.save_benefit_application(payload=_save_payload())
    stub.store[("Employee Benefit Application", res["name"])].docstatus = 1
    with pytest.raises(Exception, match="nháp"):
        m.save_benefit_application(payload=_save_payload(name=res["name"]))


# ── B7–B10: lifecycle actions ────────────────────────────────────────────────


def _create_draft(m, stub):
    stub.values[("Salary Component", "Meal Card", "max_benefit_amount")] = 0
    stub.values[("Salary Component", "Meal Card", "pay_against_benefit_claim")] = 0
    return m.save_benefit_application(payload=_save_payload())["name"]


def test_b7_employee_cannot_submit(mod):
    m, stub = mod
    name = _create_draft(m, stub)
    stub.roles = {"Employee"}
    with pytest.raises(Exception, match="Chỉ HR"):
        m.set_benefit_application_action(name, "submit")


def test_b8_hr_submits_draft(mod):
    m, stub = mod
    name = _create_draft(m, stub)
    out = m.set_benefit_application_action(name, "submit")
    assert out["docstatus"] == 1
    assert out["status"] == "Submitted"


def test_b9_cancel_from_draft_throws(mod):
    m, stub = mod
    name = _create_draft(m, stub)
    with pytest.raises(Exception, match="Chỉ huỷ"):
        m.set_benefit_application_action(name, "cancel")


def test_b10_amend_from_cancelled(mod):
    m, stub = mod
    name = _create_draft(m, stub)
    m.set_benefit_application_action(name, "submit")
    m.set_benefit_application_action(name, "cancel")
    out = m.set_benefit_application_action(name, "amend")
    assert out["docstatus"] == 0
    assert out["status"] == "Draft"
    assert out["amended_from"] == name
    assert out["name"] != name


# ── B11–B15: Benefit Claim (P2) ──────────────────────────────────────────────


def _claim_payload(**over):
    payload = {
        "employee": "HR-EMP-1",
        "claim_date": "2026-08-01",
        "earning_component": "Meal Card",
        "claimed_amount": 300_000,
    }
    payload.update(over)
    return payload


def test_b11_claim_rejects_non_claim_component(mod):
    m, stub = mod
    _seed_context(stub)  # Fuel là flexi nhưng pro-rata (claim=0)
    with pytest.raises(Exception, match="hoàn Từ"):
        m.save_benefit_claim(payload=_claim_payload(earning_component="Fuel"))


def test_b12_claim_creates_with_cap(mod):
    m, stub = mod
    _seed_context(stub)
    res = m.save_benefit_claim(payload=_claim_payload())
    assert res["name"]
    assert res["status"] == "Draft"
    assert res["max_amount_eligible"] == 3_000_000
    doc = stub.store[("Employee Benefit Claim", res["name"])]
    assert doc.claimed_amount == 300_000
    assert doc.pay_against_benefit_claim == 1


def test_b13_my_claims_idor_guard(mod):
    m, stub = mod
    stub.roles = {"Employee"}
    with pytest.raises(Exception, match="chính mình"):
        m.my_benefit_claims(employee="HR-EMP-2")


def test_b14_all_claims_requires_manager(mod):
    m, stub = mod
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.all_benefit_claims()


def test_b15_get_claim_with_attachments(mod):
    m, stub = mod
    _seed_context(stub)
    res = m.save_benefit_claim(payload=_claim_payload())
    stub.list_rows["File"] = [
        {
            "name": "F1",
            "file_name": "hoa-don.jpg",
            "file_url": "/files/hd.jpg",
            "attached_to_doctype": "Employee Benefit Claim",
            "attached_to_name": res["name"],
        },
        {
            "name": "F2",
            "file_name": "khac.pdf",
            "file_url": "/files/k.pdf",
            "attached_to_doctype": "Expense Claim",
            "attached_to_name": "OTHER",
        },
    ]
    out = m.get_benefit_claim(res["name"])
    assert len(out["attachments"]) == 1
    assert out["attachments"][0]["file_name"] == "hoa-don.jpg"
    assert out["can"]["submit"] is True  # draft + HR Manager


# ── B16–B19: Gratuity (P3) ───────────────────────────────────────────────────


def test_b16_preview_requires_relieving(mod):
    m, _ = mod
    with pytest.raises(Exception, match="ngày nghỉ việc"):
        m.preview_gratuity(payload={"employee": "HR-EMP-1", "gratuity_rule": "Rule A"})


def test_b17_preview_does_not_save(mod, monkeypatch):
    m, stub = mod
    stub.values[("Employee", "HR-EMP-1", "relieving_date")] = "2026-08-01"
    monkeypatch.setattr(
        _Doc,
        "calculate_work_experience_and_amount",
        lambda self: {"current_work_experience": 5.2, "amount": 60_000_000},
        raising=False,
    )
    out = m.preview_gratuity(payload={"employee": "HR-EMP-1", "gratuity_rule": "Rule A"})
    assert out["current_work_experience"] == 5.2
    assert out["amount"] == 60_000_000
    assert not any(dt == "Gratuity" for dt, _n in stub.store)


def test_b18_create_gratuity_slip_channel_requires_fields(mod):
    m, stub = mod
    stub.values[("Employee", "HR-EMP-1", "relieving_date")] = "2026-08-01"
    with pytest.raises(Exception, match="ngày chi trả"):
        m.create_gratuity(payload={"employee": "HR-EMP-1", "gratuity_rule": "Rule A"})


def test_b19_gratuity_create_and_submit(mod):
    m, stub = mod
    stub.values[("Employee", "HR-EMP-1", "relieving_date")] = "2026-08-01"
    res = m.create_gratuity(
        payload={
            "employee": "HR-EMP-1",
            "gratuity_rule": "Rule A",
            "payroll_date": "2026-09-05",
            "salary_component": "Gratuity",
        }
    )
    assert res["name"]
    doc = stub.store[("Gratuity", res["name"])]
    assert doc.pay_via_salary_slip == 1
    assert doc.salary_component == "Gratuity"
    out = m.set_gratuity_action(res["name"], "submit")
    assert out["docstatus"] == 1


# ── B20–B21: Promotion details (P4) ──────────────────────────────────────────


def test_b20_save_promotion_builds_details(mod):
    m, stub = mod
    stub.values[("Employee", "HR-EMP-1", "designation")] = "Sale"
    stub.values[("Employee", "HR-EMP-1", "ctc")] = 500_000_000
    res = m.save_promotion(
        payload={
            "employee": "HR-EMP-1",
            "promotion_date": "2026-09-01",
            "details": [{"fieldname": "designation", "new_value": "Sale Lead"}],
            "revised_ctc": 600_000_000,
        }
    )
    assert res["name"]
    doc = stub.store[("Employee Promotion", res["name"])]
    assert len(doc.promotion_details) == 1
    row = doc.promotion_details[0]
    assert row["fieldname"] == "designation"
    assert row["property"] == "Chức danh"
    assert row["current"] == "Sale"
    assert row["new"] == "Sale Lead"
    assert doc.current_ctc == 500_000_000  # fetch fallback từ Employee.ctc
    assert doc.revised_ctc == 600_000_000


def test_b21_promotion_submit(mod):
    m, stub = mod
    stub.values[("Employee", "HR-EMP-1", "designation")] = "Sale"
    res = m.save_promotion(
        payload={"employee": "HR-EMP-1", "details": [{"fieldname": "designation", "new_value": "Sale Lead"}]}
    )
    out = m.set_promotion_action(res["name"], "submit")
    assert out["docstatus"] == 1
    assert out["status"] == "Submitted"


# ── B22: options mở rộng ─────────────────────────────────────────────────────


def test_b22_filter_options_extended(mod):
    m, stub = mod
    stub.list_rows["Salary Component"] = [
        {"name": "Meal Card", "is_flexible_benefit": 1},
        {"name": "Basic", "is_flexible_benefit": 0},
    ]
    stub.list_rows["Payroll Period"] = [
        {"name": "PP-2026", "start_date": "2026-01-01", "end_date": "2026-12-31"},
    ]
    stub.list_rows["Gratuity Rule"] = [{"name": "Rule A"}]
    stub.list_rows["Gratuity"] = [{"status": "Unpaid"}]
    res = m.get_benefit_filter_options()
    assert res["application_statuses"] == ["Draft", "Submitted", "Cancelled"]
    assert res["claim_statuses"] == ["Draft", "Submitted", "Cancelled"]
    assert "Unpaid" in res["gratuity_statuses"]
    assert res["components"] == ["Meal Card"]
    assert res["payroll_periods"][0]["name"] == "PP-2026"
    assert res["gratuity_rules"] == ["Rule A"]
    # legacy keys vẫn tồn tại cho FE cũ
    assert res["benefits"] == ["Draft", "Submitted", "Cancelled"]
    assert "gratuity" in res and "promotion" in res
