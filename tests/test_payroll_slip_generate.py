"""WP1 (prod-readiness-plan) — Salary Slip generation without silent fallbacks.

Covers the F-LC14b rewrite of ``api/payroll.generate_salary_slips`` /
``_generate_for_line`` plus the WP-FIX-SSA root fix (``_resolve_ssa`` +
``ssa_preflight``):

  SL1  NV có SSA đủ       → slip thật docstatus 0, totals = review line, line link set
  SL2  NV KHÔNG SSA       → failed_lines + generate_errors JSON, KHÔNG fallback trắng
  SL3  Slip cũ tồn tại    → idempotent: xoá slip cũ, tạo lại, không throw
  SL4  100% lỗi           → generated=0, period vẫn advance, liệt kê đủ lỗi
  SL5  retry_failed=1     → chỉ xử lý dòng chưa có slip
  SL7  OT amount > 0      → stamp vn_overtime_amount bằng đúng line
  SL8  Kỳ không hợp lệ    → chặn với message rõ (không 500)

  WP-FIX-SSA (root fix — classified SSA resolver, HRMS-aligned):
  T1   SSA from_date == period.from_date (boundary)  → slip tạo OK
  T2   Nhiều SSA submitted                            → chọn SSA MỚI NHẤT (from_date desc)
  T3   Không có SSA                                   → code=SSA_MISSING, reason có mã NV + mốc ngày
  T4   Chỉ có SSA Draft                               → code=SSA_DRAFT, reason nêu số bản draft
  T5   SSA hiệu lực giữa kỳ                           → code=SSA_MID_PERIOD, reason chứa 2 mốc ngày
  T6   SSA hiệu lực sau kỳ                            → code=SSA_FUTURE
  T7   Structure chưa submit / is_active=No           → code=SSA_STRUCTURE_INACTIVE
  T8   SSA thuộc công ty khác                         → code=SSA_COMPANY_MISMATCH
  T9   Lỗi hệ thống khi lookup                        → KHÔNG giả dạng thiếu SSA (code=SLIP_ERROR)
  T10  ssa_preflight: 1 dòng đủ + 1 dòng thiếu        → {ok, total, issues[code=SSA_MISSING]}
  T11  ssa_preflight: mọi dòng đủ / đã có slip        → issues=[]

Bench-free: stub ``frappe`` injected into ``sys.modules`` (same harness pattern
as tests/test_checkout_miss_api.py).
"""

from __future__ import annotations

import importlib
import json
import sys
import types

import pytest


class FrappeError(Exception):
    pass


class NSDict(dict):
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError as e:
            raise AttributeError(key) from e


class FakeMeta:
    def __init__(self, fields):
        self._fields = set(fields)

    def has_field(self, name):
        return name in self._fields


class FakeSlip:
    """Salary Slip document double."""

    def __init__(self, payload, counter, *, insert_raises=False):
        self._counter = counter
        self._insert_raises = insert_raises
        self.db_sets: list[tuple] = []
        self.inserted = False
        for k, v in (payload or {}).items():
            setattr(self, k, v)
        self.name = f"SLIP-{next(counter)}"
        self.meta = FakeMeta(["earnings", "gross_pay", "net_pay", "total_deduction", "vn_overtime_amount"])

    def insert(self, **_kw):
        if self._insert_raises:
            raise FrappeError("Salary Slip: Employee needs a Salary Structure Assignment")
        self.inserted = True
        return self

    def db_set(self, field, value):
        self.db_sets.append((field, value))
        setattr(self, field, value)


class FakePeriod:
    def __init__(self, payload):
        for k, v in (payload or {}).items():
            setattr(self, k, v)
        self.saved = False
        self.meta = FakeMeta(["generate_errors"])

    def save(self, **_kw):
        self.saved = True
        return self


class StubFrappe:
    """Configurable frappe stub for the payroll-generate flow.

    ``ssa`` accepts a single record OR a list of records per employee
    (mirroring multiple submitted SSAs — order by ``from_date`` is simulated).
    ``draft_ssas`` maps employee → number of draft SSAs (diagnostic only).
    ``structures`` maps Salary Structure name → {docstatus, is_active};
    unknown structures default to submitted+active.
    ``ssa_error=True`` simulates an unexpected DB failure on SSA lookup.
    """

    def __init__(
        self,
        *,
        lines,
        period,
        ssa=None,
        draft_ssas=None,
        structures=None,
        ssa_error=False,
        existing_slips=None,
        insert_raises=False,
    ):
        self.lines = [dict(l) for l in lines]
        self.period_payload = dict(period)
        self.period_doc = None
        self.ssa = ssa or {}
        self.draft_ssas = dict(draft_ssas or {})
        self.structures = dict(structures or {})
        self.ssa_error = ssa_error
        self.existing_slips = list(existing_slips or [])
        self.insert_raises = insert_raises
        self._counter = iter(range(1000, 9999))
        self.created_slips: list[FakeSlip] = []
        self.deleted: list[tuple] = []
        self.set_values: list[tuple] = []
        self.log_errors: list[str] = []

    def _ssa_records(self) -> dict:
        """Per-employee SSA lists, from_date desc (mimics order_by in DB)."""
        out: dict = {}
        for emp, recs in self.ssa.items():
            if isinstance(recs, dict):
                recs = [recs]
            recs = [dict(r) for r in recs]
            recs.sort(key=lambda r: str(r.get("from_date") or ""), reverse=True)
            out[emp] = recs
        return out

    def _build_db(self):
        outer = self

        class _DB:
            def get_value(inner, doctype, filters=None, fieldname=None, as_dict=False, **_kw):
                if doctype == "Salary Structure Assignment":
                    emp = filters.get("employee") if isinstance(filters, dict) else None
                    rec = (outer._ssa_records().get(emp) or [None])[0]
                    if not rec:
                        return None
                    if isinstance(fieldname, (list, tuple)):
                        return NSDict({f: rec.get(f) for f in fieldname})
                    return rec.get(fieldname)
                if doctype == "Salary Structure":
                    name = filters if isinstance(filters, str) else None
                    rec = outer.structures.get(name or "") or {"docstatus": 1, "is_active": "Yes"}
                    if isinstance(fieldname, (list, tuple)):
                        return NSDict({f: rec.get(f) for f in fieldname})
                    return rec.get(fieldname)
                return None

            def get_all(inner, doctype, filters=None, fields=None, pluck=None, **_kw):
                if doctype == "VN Payroll Review Line":
                    return [dict(l) for l in outer.lines]
                if doctype == "Salary Slip":
                    return list(outer.existing_slips)
                if doctype == "Salary Structure Assignment":
                    if outer.ssa_error:
                        raise RuntimeError("simulated DB failure on SSA lookup")
                    emp = (filters or {}).get("employee")
                    if (filters or {}).get("docstatus") == 1:
                        recs = [dict(r) for r in outer._ssa_records().get(emp, [])]
                    else:  # draft diagnostic count
                        n = int(outer.draft_ssas.get(emp, 0))
                        recs = [{"name": f"SSA-DRAFT-{emp}-{i}"} for i in range(n)]
                    if fields:
                        recs = [{f: r.get(f) for f in fields} for r in recs]
                    if pluck:
                        return [r.get(pluck) for r in recs]
                    return recs
                return []

            def set_value(inner, doctype, name, field=None, value=None, **_kw):
                outer.set_values.append((doctype, name, field, value))
                return None

        return _DB()

    def get_doc(self, arg, name=None, **_kw):
        if isinstance(arg, dict):
            doc = FakeSlip(arg, self._counter, insert_raises=self.insert_raises)
            self.created_slips.append(doc)
            return doc
        if arg == "VN Payroll Review Period":
            self.period_doc = FakePeriod(self.period_payload)
            return self.period_doc
        raise FrappeError(f"unexpected get_doc {arg} {name}")

    def delete_doc(self, doctype, name, **_kw):
        self.deleted.append((doctype, name))

    def log_error(self, title=None, message=None, *_a, **_kw):
        self.log_errors.append(f"{title}: {message}")

    def throw(self, msg, exc=None):
        raise FrappeError(msg)


def _line(name="L1", employee="HR-EMP-001", salary_slip=None, **kw):
    base = {
        "name": name,
        "employee": employee,
        "employee_name": "NV Test",
        "salary_slip": salary_slip,
        "payable_days": 26,
        "regular_hours": 208,
        "overtime_hours": 3,
        "overtime_amount": 150000,
        "night_allowance_amount": 0,
        "late_penalty_amount": 0,
        "salary_advance_deduction": 0,
        "other_deduction": 0,
        "checkout_miss_penalty": 0,
        "total_deduction": 500000,
        "gross_pay": 30000000,
        "net_pay": 29500000,
    }
    base.update(kw)
    return base


def _ssa(name="SSA-1", fd="2026-07-01", structure="ST-MOI", company="GeGe Esport"):
    return {
        "name": name,
        "from_date": fd,
        "salary_structure": structure,
        "company": company,
    }


_PERIOD = {
    "doctype": "VN Payroll Review Period",
    "name": "PRP-00001",
    "status": "Approved",
    "company": "GeGe Esport",
    "payroll_month": "08",
    "payroll_year": 2026,
    "from_date": "2026-08-01",
    "to_date": "2026-08-31",
}


@pytest.fixture()
def api(monkeypatch):
    def _make(**kw):
        stub = StubFrappe(**kw)
        stub.db = stub._build_db()

        utils = types.ModuleType("frappe.utils")
        utils.flt = lambda v, p=None: round(float(v or 0), p if p is not None else 2)
        utils.now = lambda: "2026-09-01 08:00:00"
        utils.getdate = lambda v=None: __import__("datetime").date.today()
        utils.nowdate = utils.now
        utils.cint = lambda v, *a: int(v or 0)

        frappe_mod = types.ModuleType("frappe")
        frappe_mod.db = stub.db
        frappe_mod.get_doc = stub.get_doc
        frappe_mod.delete_doc = stub.delete_doc
        frappe_mod.log_error = stub.log_error
        frappe_mod.throw = stub.throw
        frappe_mod._ = lambda s: s
        frappe_mod.whitelist = lambda *a, **k: a[0] if a and callable(a[0]) else (lambda f: f)
        frappe_mod.utils = utils
        frappe_mod.session = types.SimpleNamespace(user="hr@example.com")
        frappe_mod.ValidationError = FrappeError
        frappe_mod.PermissionError = FrappeError
        frappe_mod.get_traceback = lambda: "tb"

        monkeypatch.setitem(sys.modules, "frappe", frappe_mod)
        monkeypatch.setitem(sys.modules, "frappe.utils", utils)

        fake_audit = types.ModuleType("gege_hr.gege_hr.api.audit")
        fake_audit.log = lambda *a, **kw: None
        monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.api.audit", fake_audit)

        fake_emp = types.ModuleType("gege_hr.gege_hr.utils.employee")
        fake_emp.HR_MANAGER_ROLES = {"HR Manager", "HR User", "Payroll Manager", "System Manager"}
        fake_emp.get_user_roles = lambda: ["HR Manager"]
        fake_emp.get_employee_for_user = lambda: "HR-EMP-001"
        fake_emp.emp_name = lambda v: v
        monkeypatch.setitem(sys.modules, "gege_hr.gege_hr.utils.employee", fake_emp)

        import gege_hr.gege_hr.api as api_pkg
        import gege_hr.gege_hr.utils as utils_pkg

        monkeypatch.setattr(api_pkg, "audit", fake_audit, raising=False)
        monkeypatch.setattr(utils_pkg, "employee", fake_emp, raising=False)

        import gege_hr.gege_hr.utils.payroll as calc_mod

        # monkeypatch (not bare assignment) so the cached module's frappe ref is
        # restored on teardown — no cross-test contamination.
        monkeypatch.setattr(calc_mod, "frappe", frappe_mod, raising=False)
        mod = importlib.reload(importlib.import_module("gege_hr.gege_hr.api.payroll"))
        return stub, mod

    return _make


# --------------------------------------------------------------------------- #
# SL1 — happy path with SSA
# --------------------------------------------------------------------------- #
def test_sl1_generates_real_slip_with_review_totals(api):
    stub, mod = api(
        lines=[_line()],
        period=_PERIOD,
        ssa={"HR-EMP-001": _ssa()},
    )
    res = mod.generate_salary_slips(name="PRP-00001")

    assert res["generated"] == 1
    assert res["failed"] == 0
    assert len(stub.created_slips) == 1
    slip = stub.created_slips[0]
    assert slip.inserted is True
    assert slip.docstatus == 0
    # totals overridden to the REVIEWED amounts
    stamps = dict(slip.db_sets)
    assert stamps["gross_pay"] == 30000000
    assert stamps["net_pay"] == 29500000
    assert stamps["total_deduction"] == 500000
    # line linked
    assert ("VN Payroll Review Line", "L1", "salary_slip", slip.name) in stub.set_values


# --------------------------------------------------------------------------- #
# SL2 — missing SSA surfaces as failed line (no silent fallback)
# --------------------------------------------------------------------------- #
def test_sl2_missing_ssa_lands_in_failed_lines_and_generate_errors(api):
    stub, mod = api(lines=[_line()], period=_PERIOD, ssa={})
    res = mod.generate_salary_slips(name="PRP-00001")

    assert res["generated"] == 0
    assert res["failed"] == 1
    entry = res["failed_lines"][0]
    assert entry["employee"] == "HR-EMP-001"
    assert "Salary Structure Assignment" in entry["reason"]
    # persisted snapshot for the UI banner
    stored = json.loads(stub.period_doc.generate_errors)
    assert stored["failed_lines"][0]["employee"] == "HR-EMP-001"
    assert stub.period_doc.generate_errors is not None
    # error logged — never silent
    assert any("payroll slip generate failed" in e for e in stub.log_errors)


# --------------------------------------------------------------------------- #
# SL3 — idempotent regenerate replaces the prior draft slip
# --------------------------------------------------------------------------- #
def test_sl3_existing_draft_slip_is_replaced_not_duplicated(api):
    stub, mod = api(
        lines=[_line()],
        period=_PERIOD,
        ssa={"HR-EMP-001": _ssa(structure="ST-MOI", company=None)},
        existing_slips=["SAL-SLIP/OLD-001"],
    )
    res = mod.generate_salary_slips(name="PRP-00001")

    assert res["generated"] == 1
    assert ("Salary Slip", "SAL-SLIP/OLD-001") in stub.deleted
    assert len(stub.created_slips) == 1  # exactly one new slip, no duplicate


# --------------------------------------------------------------------------- #
# SL4 — 100% failure still advances the period and lists every reason
# --------------------------------------------------------------------------- #
def test_sl4_all_fail_period_still_advances(api):
    stub, mod = api(
        lines=[_line(name="L1", employee="E1"), _line(name="L2", employee="E2")],
        period=_PERIOD,
        ssa={},
    )
    res = mod.generate_salary_slips(name="PRP-00001")

    assert res["generated"] == 0
    assert res["failed"] == 2
    assert {f["employee"] for f in res["failed_lines"]} == {"E1", "E2"}
    assert stub.period_doc.status == "Slips Generated"
    assert stub.period_doc.saved is True


# --------------------------------------------------------------------------- #
# SL5 — retry only processes lines without a slip
# --------------------------------------------------------------------------- #
def test_sl5_retry_skips_lines_that_already_have_slips(api):
    stub, mod = api(
        lines=[
            _line(name="L1", employee="E1", salary_slip="SAL-SLIP/OK-1"),
            _line(name="L2", employee="E2", salary_slip=None),
        ],
        period={**_PERIOD, "status": "Slips Generated"},
        ssa={"E1": _ssa(name="SSA-1", structure="ST"), "E2": _ssa(name="SSA-2", structure="ST")},
    )
    res = mod.generate_salary_slips(name="PRP-00001", retry_failed=1)

    # only E2 regenerated; E1 (already has slip) untouched
    assert res["generated"] == 1
    assert len(stub.created_slips) == 1
    assert stub.created_slips[0].employee == "E2"


# --------------------------------------------------------------------------- #
# SL7 — OT amount stamped on the slip
# --------------------------------------------------------------------------- #
def test_sl7_overtime_amount_stamped(api, monkeypatch):
    stub, mod = api(
        lines=[_line(overtime_amount=150000)],
        period=_PERIOD,
        ssa={"HR-EMP-001": _ssa()},
    )
    monkeypatch.setattr(mod.calc, "load_component_map", lambda c: {"OT": "OT Component"})
    res = mod.generate_salary_slips(name="PRP-00001")

    assert res["generated"] == 1
    slip = stub.created_slips[0]
    stamps = dict(slip.db_sets)
    assert stamps.get("vn_overtime_amount") == 150000


# --------------------------------------------------------------------------- #
# SL8 — invalid period state blocked with a clear message
# --------------------------------------------------------------------------- #
def test_sl8_locked_period_blocked_with_clear_message(api):
    _, mod = api(lines=[_line()], period={**_PERIOD, "status": "Published"})
    with pytest.raises(FrappeError) as ei:
        mod.generate_salary_slips(name="PRP-00001")
    assert "Approved" in str(ei.value)


# --------------------------------------------------------------------------- #
# insert() failure surfaces the HRMS validation message as the reason
# --------------------------------------------------------------------------- #
def test_insert_failure_becomes_failed_line_reason(api):
    stub, mod = api(
        lines=[_line()],
        period=_PERIOD,
        ssa={"HR-EMP-001": _ssa()},
        insert_raises=True,
    )
    res = mod.generate_salary_slips(name="PRP-00001")
    assert res["generated"] == 0
    assert "Salary Structure Assignment" in res["failed_lines"][0]["reason"]


# --------------------------------------------------------------------------- #
# _generate_for_line raises SlipGenerationError (never returns None)
# --------------------------------------------------------------------------- #
def test_generate_for_line_raises_without_ssa(api):
    _, mod = api(lines=[_line()], period=_PERIOD, ssa={})
    period = mod._get_period("PRP-00001")
    with pytest.raises(mod.SlipGenerationError):
        mod._generate_for_line(period, _line(), {})


# =========================================================================== #
# WP-FIX-SSA — classified resolver (_resolve_ssa) aligned with HRMS semantics
# =========================================================================== #


# --------------------------------------------------------------------------- #
# T1 — boundary: SSA effective exactly on period.from_date is valid
# --------------------------------------------------------------------------- #
def test_t1_ssa_effective_on_period_start_is_used(api):
    stub, mod = api(
        lines=[_line()],
        period=_PERIOD,
        ssa={"HR-EMP-001": _ssa(fd="2026-08-01")},
    )
    res = mod.generate_salary_slips(name="PRP-00001")

    assert res["generated"] == 1
    assert stub.created_slips[0].salary_structure == "ST-MOI"


# --------------------------------------------------------------------------- #
# T2 — multiple submitted SSAs → the LATEST from_date wins (HRMS order_by)
# --------------------------------------------------------------------------- #
def test_t2_latest_ssa_wins_when_multiple(api):
    stub, mod = api(
        lines=[_line()],
        period=_PERIOD,
        ssa={
            "HR-EMP-001": [
                _ssa(name="SSA-OLD", fd="2026-06-01", structure="ST-CU"),
                _ssa(name="SSA-NEW", fd="2026-07-15", structure="ST-MOI"),
            ]
        },
    )
    res = mod.generate_salary_slips(name="PRP-00001")

    assert res["generated"] == 1
    assert stub.created_slips[0].salary_structure == "ST-MOI"


# --------------------------------------------------------------------------- #
# T3 — no SSA at all → SSA_MISSING with employee + required effective date
# --------------------------------------------------------------------------- #
def test_t3_missing_ssa_classified_with_dates(api):
    stub, mod = api(lines=[_line()], period=_PERIOD, ssa={})
    res = mod.generate_salary_slips(name="PRP-00001")

    assert res["failed"] == 1
    entry = res["failed_lines"][0]
    assert entry["code"] == "SSA_MISSING"
    assert entry["employee"] == "HR-EMP-001"
    assert entry["employee"] in entry["reason"]
    assert "2026-08-01" in entry["reason"]  # required effective boundary
    stored = json.loads(stub.period_doc.generate_errors)
    assert stored["failed_lines"][0]["code"] == "SSA_MISSING"


# --------------------------------------------------------------------------- #
# T4 — only draft SSAs → SSA_DRAFT mentions the draft count
# --------------------------------------------------------------------------- #
def test_t4_draft_only_ssa_classified(api):
    stub, mod = api(
        lines=[_line()],
        period=_PERIOD,
        ssa={},
        draft_ssas={"HR-EMP-001": 2},
    )
    res = mod.generate_salary_slips(name="PRP-00001")

    assert res["failed"] == 1
    entry = res["failed_lines"][0]
    assert entry["code"] == "SSA_DRAFT"
    assert "2" in entry["reason"]
    assert "Draft" in entry["reason"]


# --------------------------------------------------------------------------- #
# T5 — SSA effective mid-period → SSA_MID_PERIOD names both boundary dates
# --------------------------------------------------------------------------- #
def test_t5_mid_period_ssa_classified(api):
    stub, mod = api(
        lines=[_line()],
        period=_PERIOD,
        ssa={"HR-EMP-001": _ssa(fd="2026-08-15")},
    )
    res = mod.generate_salary_slips(name="PRP-00001")

    assert res["failed"] == 1
    entry = res["failed_lines"][0]
    assert entry["code"] == "SSA_MID_PERIOD"
    # both boundary dates present so HR knows exactly what to fix
    assert "2026-08-15" in entry["reason"]
    assert "2026-08-01" in entry["reason"]


# --------------------------------------------------------------------------- #
# T6 — SSA effective after the period ends → SSA_FUTURE
# --------------------------------------------------------------------------- #
def test_t6_future_ssa_classified(api):
    stub, mod = api(
        lines=[_line()],
        period=_PERIOD,
        ssa={"HR-EMP-001": _ssa(fd="2026-09-05")},
    )
    res = mod.generate_salary_slips(name="PRP-00001")

    assert res["failed"] == 1
    entry = res["failed_lines"][0]
    assert entry["code"] == "SSA_FUTURE"
    assert "2026-09-05" in entry["reason"]
    assert "2026-08-31" in entry["reason"]


# --------------------------------------------------------------------------- #
# T7 — linked Salary Structure not submitted / inactive → SSA_STRUCTURE_INACTIVE
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "structure_rec",
    [
        {"docstatus": 0, "is_active": "Yes"},  # still a draft
        {"docstatus": 1, "is_active": "No"},  # deactivated
    ],
)
def test_t7_inactive_structure_classified(api, structure_rec):
    stub, mod = api(
        lines=[_line()],
        period=_PERIOD,
        ssa={"HR-EMP-001": _ssa()},
        structures={"ST-MOI": structure_rec},
    )
    res = mod.generate_salary_slips(name="PRP-00001")

    assert res["failed"] == 1
    entry = res["failed_lines"][0]
    assert entry["code"] == "SSA_STRUCTURE_INACTIVE"
    assert "ST-MOI" in entry["reason"]


# --------------------------------------------------------------------------- #
# T8 — SSA belongs to another company → SSA_COMPANY_MISMATCH
# --------------------------------------------------------------------------- #
def test_t8_company_mismatch_classified(api):
    stub, mod = api(
        lines=[_line()],
        period=_PERIOD,
        ssa={"HR-EMP-001": _ssa(company="Cong Ty Khac")},
    )
    res = mod.generate_salary_slips(name="PRP-00001")

    assert res["failed"] == 1
    entry = res["failed_lines"][0]
    assert entry["code"] == "SSA_COMPANY_MISMATCH"
    assert "Cong Ty Khac" in entry["reason"]
    assert "GeGe Esport" in entry["reason"]


# --------------------------------------------------------------------------- #
# T9 — unexpected lookup error must NOT masquerade as a missing-SSA issue
# --------------------------------------------------------------------------- #
def test_t9_system_error_not_masqueraded_as_missing_ssa(api):
    stub, mod = api(
        lines=[_line()],
        period=_PERIOD,
        ssa={},
        ssa_error=True,
    )
    res = mod.generate_salary_slips(name="PRP-00001")

    assert res["failed"] == 1
    entry = res["failed_lines"][0]
    assert entry["code"] == "SLIP_ERROR"
    assert "chưa có Salary Structure Assignment" not in entry["reason"]
    assert "simulated DB failure" in entry["reason"]
    # traceback-logged via the generic handler — never silent
    assert any("payroll slip generate failed" in e for e in stub.log_errors)


# --------------------------------------------------------------------------- #
# T10 — ssa_preflight: mixed readiness returns per-line issues
# --------------------------------------------------------------------------- #
def test_t10_preflight_reports_missing_ssa(api):
    stub, mod = api(
        lines=[_line(name="L1", employee="E1"), _line(name="L2", employee="E2")],
        period=_PERIOD,
        ssa={"E1": _ssa(name="SSA-1")},
    )
    res = mod.ssa_preflight(name="PRP-00001")

    assert res["ok"] == 1
    assert res["total"] == 2
    assert len(res["issues"]) == 1
    issue = res["issues"][0]
    assert issue["line"] == "L2"
    assert issue["code"] == "SSA_MISSING"
    assert issue["reason"]


# --------------------------------------------------------------------------- #
# T11 — ssa_preflight: all ready (lines with an existing slip are skipped)
# --------------------------------------------------------------------------- #
def test_t11_preflight_all_ready_skips_slipped_lines(api):
    _, mod = api(
        lines=[
            _line(name="L1", employee="E1"),
            _line(name="L2", employee="E2", salary_slip="SAL-SLIP/OK-1"),
        ],
        period=_PERIOD,
        ssa={"E1": _ssa(name="SSA-1"), "E2": _ssa(name="SSA-2")},
    )
    res = mod.ssa_preflight(name="PRP-00001")

    assert res["issues"] == []
    assert res["ok"] == 1  # only L1 counted; L2 already has a slip
    assert res["total"] == 1


# --------------------------------------------------------------------------- #
# SL-ADVANCE — salary-advance deduction flows onto the slip exactly once
# (2026-08 rule: advance requested in period P is deducted from P's salary).
#
#   TC-F1  line carries salary_advance_deduction → vn_salary_advance_deduction
#          stamped on the slip
#   TC-F2  total_deduction / net_pay reflect the advance exactly once
#   TC-F3  no double count: the review line is the single source of truth —
#          _stamp_slip_totals OVERRIDES whatever HRMS computed (even if the
#          ERPNext Additional Salary row was already pulled in), so the final
#          totals always equal the reviewed line
#   TC-F4  no advance → vn_salary_advance_deduction = 0, totals unaffected
# --------------------------------------------------------------------------- #
def test_f1_advance_deduction_stamped_onto_slip(api):
    stub, mod = api(
        lines=[
            _line(
                salary_advance_deduction=2_000_000,
                total_deduction=2_500_000,
                gross_pay=10_000_000,
                net_pay=7_500_000,
            )
        ],
        period=_PERIOD,
        ssa={"HR-EMP-001": _ssa()},
    )
    res = mod.generate_salary_slips(name="PRP-00001")

    assert res["generated"] == 1
    slip = stub.created_slips[0]
    stamps = dict(slip.db_sets)
    assert stamps["vn_salary_advance_deduction"] == 2_000_000
    # the period link travels with the breakdown
    assert stamps["vn_payroll_review_period"] == "PRP-00001"


def test_f2_totals_include_advance_exactly_once(api):
    stub, mod = api(
        lines=[
            _line(
                salary_advance_deduction=2_000_000,
                late_penalty_amount=0,
                other_deduction=500_000,
                total_deduction=2_500_000,
                gross_pay=10_000_000,
                net_pay=7_500_000,
            )
        ],
        period=_PERIOD,
        ssa={"HR-EMP-001": _ssa()},
    )
    mod.generate_salary_slips(name="PRP-00001")

    slip = stub.created_slips[0]
    stamps = dict(slip.db_sets)
    assert stamps["total_deduction"] == 2_500_000  # 2M advance + 500k other
    assert stamps["net_pay"] == 7_500_000  # 10M gross − 2.5M deductions
    # arithmetic sanity: totals minus the non-advance parts = the advance
    assert stamps["total_deduction"] - stamps["vn_salary_advance_deduction"] - 500_000 == 0


def test_f3_no_double_count_review_line_is_source_of_truth(api):
    """Even when the ERPNext Additional Salary deduction already exists for
    the Paid advance (HRMS may pull it into the slip), the FINAL stamped
    totals equal the review line — the advance is deducted exactly once."""
    stub, mod = api(
        lines=[
            _line(
                salary_advance_deduction=2_000_000,
                total_deduction=2_000_000,
                gross_pay=10_000_000,
                net_pay=8_000_000,
            )
        ],
        period=_PERIOD,
        ssa={"HR-EMP-001": _ssa()},
    )
    mod.generate_salary_slips(name="PRP-00001")

    slip = stub.created_slips[0]
    stamps = dict(slip.db_sets)
    # stamping happens AFTER insert and overwrites any HRMS-computed totals:
    # the advance appears once (2M), never twice (not 4M).
    assert stamps["total_deduction"] == 2_000_000
    assert stamps["net_pay"] == 8_000_000
    assert slip.total_deduction == 2_000_000
    assert slip.net_pay == 8_000_000


def test_f4_no_advance_leaves_slip_clean(api):
    stub, mod = api(
        lines=[_line(salary_advance_deduction=0)],
        period=_PERIOD,
        ssa={"HR-EMP-001": _ssa()},
    )
    mod.generate_salary_slips(name="PRP-00001")

    slip = stub.created_slips[0]
    stamps = dict(slip.db_sets)
    assert stamps["vn_salary_advance_deduction"] == 0
    assert stamps["total_deduction"] == 500_000
    assert stamps["net_pay"] == 29_500_000
