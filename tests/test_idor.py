"""IDOR deny-path regression — plan v5 §19.4 IDOR Prevention.

Pins the Insecure-Direct-Object-Reference property for the employee-facing
leave + handover APIs:

  * a plain **Employee** may read/mutate ONLY their own data;
  * an **HR Manager / System Manager** bypasses the self-scope check;
  * for handover, an **HR User** is also treated as a manager (broad scope).

The deny path must raise ``frappe.PermissionError`` and produce zero side
effects (no ``get_doc`` load, no ``insert``, no ``save``). A stub ``frappe``
is injected via ``monkeypatch.setitem(sys.modules, ...)``, mirroring the
harness in ``test_handover_api.py`` / ``test_hardening.py``, so these tests
run entirely bench-free.

Covered surface:
  * ``api.leave._assert_own``           — canonical self-scope guard
  * ``api.leave.my_applications``       — end-to-end deny through a real endpoint
  * ``api.handover.create_handover``    — non-manager may only mint for self
  * ``api.handover.update_handover_status`` / ``_get_owned_or_managed``
                                         — non-manager must be from/to_employee
  * ``api.handover.leave_handovers``    — non-manager list scoped to involvement
  * ``api.handover.my_handovers``       — always self-scoped (to_employee only)
"""

import datetime
import importlib
import sys
import types

import pytest


# --------------------------------------------------------------------------- #
# Exceptions carried by the stub frappe — exactly what the guards raise via
# ``frappe.throw(..., frappe.PermissionError)`` / ``frappe.ValidationError``.
# --------------------------------------------------------------------------- #
class _PermissionDenied(Exception):
    pass


class _ValidationError(Exception):
    pass


class _FakeDoc:
    """A document stub that records every mutation a denied call must avoid."""

    def __init__(self, name="NEW-0001", **fields):
        self.name = name
        self.status = "Pending"
        self.from_employee = None
        self.to_employee = None
        for k, v in fields.items():
            setattr(self, k, v)
        self.saved = False
        self.inserted = False
        self.flags = types.SimpleNamespace()

    def get(self, key, default=None):
        return getattr(self, key, default)

    def insert(self, ignore_permissions=False):
        self.inserted = True
        return self

    def save(self, ignore_permissions=False):
        self.saved = True
        return self


class _Harness:
    """Tracks side effects the IDOR guard must prevent."""

    def __init__(self):
        self.created: list[dict] = []
        self.loaded: list[tuple] = []
        self.list_calls: list[dict] = []
        # The doc returned on a load (``frappe.get_doc(doctype, name)``).
        # Tests reconfigure its ``from_employee`` / ``to_employee`` as needed.
        self.doc = _FakeDoc(name="HT-0001")
        # Rows served by ``frappe.db.get_all``.
        self.list_rows: list[dict] = []

    # frappe.get_doc overloads: payload dict (create) or (doctype, name) (load).
    def get_doc(self, *args, **kwargs):
        if len(args) == 1 and isinstance(args[0], dict):
            payload = dict(args[0])
            doc = _FakeDoc(**payload)
            self.created.append(payload)
            return doc
        self.loaded.append(args)
        return self.doc

    def get_all(self, doctype, filters=None, fields=None, order_by=None, limit_page_length=None):
        self.list_calls.append({"doctype": doctype, "filters": filters})
        return [dict(r) for r in self.list_rows]

    def get_value(self, doctype, *args, **kwargs):
        return None

    def table_exists(self, doctype):
        return True


def _build_stub_frappe(harness: _Harness):
    mod = types.ModuleType("frappe")
    mod._ = lambda s: s
    mod.whitelist = lambda fn=None, **kw: fn if fn is not None else (lambda f: f)

    # The two exception types the guards raise through frappe.throw.
    mod.PermissionError = _PermissionDenied
    mod.ValidationError = _ValidationError

    def _throw(msg, exc=None):
        if exc is None:
            raise Exception(str(msg))
        raise exc(str(msg))

    mod.throw = _throw
    mod.log_error = lambda *a, **k: None
    mod.get_roles = lambda user: []

    utils = types.ModuleType("frappe.utils")
    utils.now = lambda: "2026-06-23 10:00:00"
    utils.today = lambda: datetime.date.today().isoformat()
    utils.getdate = lambda v=None: (
        datetime.date.today() if v in (None, "") else datetime.date.fromisoformat(str(v)[:10])
    )
    utils.cint = lambda v, *a: int(v) if v not in (None, "") else 0
    mod.utils = utils

    mod.db = harness
    mod.get_doc = harness.get_doc
    mod.get_all = harness.get_all
    mod.session = types.SimpleNamespace(user="caller@gege.demo")
    mod.local = types.SimpleNamespace(request_ip=None)
    mod.flags = types.SimpleNamespace()
    return mod


@pytest.fixture
def idor(monkeypatch):
    """Install the stub frappe and import the leave + handover API modules.

    Returns a controller whose ``persona(roles=..., employee=...)`` swaps the
    caller's role set + linked Employee for the remainder of the test, by
    patching the shared ``utils.employee`` resolver (both api modules bind the
    same module object, so both observe the swap).
    """
    harness = _Harness()
    stub = _build_stub_frappe(harness)
    monkeypatch.setitem(sys.modules, "frappe", stub)
    monkeypatch.setitem(sys.modules, "frappe.utils", stub.utils)

    leave = importlib.import_module("gege_hr.gege_hr.api.leave")
    handover = importlib.import_module("gege_hr.gege_hr.api.handover")
    leave_extra = importlib.import_module("gege_hr.gege_hr.api.leave_extra")
    employee_services = importlib.import_module("gege_hr.gege_hr.api.employee_services")
    for m in (leave, handover, leave_extra, employee_services):
        monkeypatch.setattr(m, "frappe", stub)

    # The create / update paths fire a best-effort notification; neutralise it
    # so the test never depends on the VN Notification table.
    monkeypatch.setattr(handover.notify, "push_notification", lambda *a, **k: None)
    monkeypatch.setattr(leave_extra.notify, "push_notification", lambda *a, **k: None)
    monkeypatch.setattr(employee_services.notify, "push_notification", lambda *a, **k: None)

    emp_utils = importlib.import_module("gege_hr.gege_hr.utils.employee")

    state = {"roles": [], "employee": None}

    def _roles(user=None):
        return list(state["roles"])

    def _emp(user=None):
        return state["employee"]

    # Patch the shared emp_utils module object — both api modules see it.
    monkeypatch.setattr(emp_utils, "get_user_roles", _roles)
    monkeypatch.setattr(emp_utils, "get_employee_for_user", _emp)

    def persona(roles=None, employee=None):
        state["roles"] = list(roles or [])
        state["employee"] = employee

    # leave_extra / employee_services resolve the caller through their own
    # frappe.get_roles + frappe.db.get_value("Employee", {"user_id": ...}) —
    # wire the same persona state into the stub so the swap is visible there.
    stub.get_roles = _roles
    harness.get_value = lambda doctype, *a, **k: (
        state["employee"] if doctype == "Employee" and a and isinstance(a[0], dict) else None
    )

    return types.SimpleNamespace(
        leave=leave,
        handover=handover,
        leave_extra=leave_extra,
        employee_services=employee_services,
        harness=harness,
        stub=stub,
        persona=persona,
    )


# --------------------------------------------------------------------------- #
# leave._assert_own — the canonical self-scope guard (§19.4)
# --------------------------------------------------------------------------- #
def test_assert_own_denies_other_employee(idor):
    """A plain Employee may NOT read another employee's row."""
    idor.persona(roles=["Employee"], employee="SELF")
    with pytest.raises(_PermissionDenied):
        idor.leave._assert_own("OTHER")


def test_assert_own_allows_self(idor):
    """A plain Employee reading their own row is allowed."""
    idor.persona(roles=["Employee"], employee="SELF")
    idor.leave._assert_own("SELF")  # must not raise


def test_assert_own_allows_hr_manager_bypass(idor):
    """An HR Manager may read anyone (manager bypass)."""
    idor.persona(roles=["HR Manager"], employee="SELF")
    idor.leave._assert_own("OTHER")  # must not raise


def test_assert_own_allows_system_manager_bypass(idor):
    """A System Manager also bypasses the self-scope check."""
    idor.persona(roles=["System Manager"], employee="SELF")
    idor.leave._assert_own("OTHER")  # must not raise


def test_assert_own_denies_hr_user_for_leave(idor):
    """Leave's manager set is HR_MANAGER_ROLES only — HR User is NOT a bypass."""
    idor.persona(roles=["HRUser"], employee="SELF")
    with pytest.raises(_PermissionDenied):
        idor.leave._assert_own("OTHER")


# --------------------------------------------------------------------------- #
# leave.my_applications — end-to-end deny through a real whitelisted endpoint
# --------------------------------------------------------------------------- #
def test_my_applications_denies_cross_employee_with_no_side_effects(idor):
    """Reading another employee's applications is rejected before any DB load."""
    idor.persona(roles=["Employee"], employee="SELF")
    with pytest.raises(_PermissionDenied):
        idor.leave.my_applications(employee="OTHER")
    # No list query may fire — the guard short-circuits.
    assert idor.harness.list_calls == []


def test_my_applications_allows_self(idor):
    """Reading one's own applications proceeds (guard passes)."""
    idor.persona(roles=["Employee"], employee="SELF")
    # Returns whatever the stub serves (here []); the point is it does not throw.
    out = idor.leave.my_applications(employee="SELF")
    assert isinstance(out, list)


# --------------------------------------------------------------------------- #
# handover.create_handover — a non-manager may only mint for themselves
# --------------------------------------------------------------------------- #
def test_create_handover_denies_minting_for_other(idor):
    """A plain Employee may not create a handover where from_employee != self."""
    idor.persona(roles=["Employee"], employee="SELF")
    with pytest.raises(_PermissionDenied):
        idor.handover.create_handover(
            leave_application="LA-0001",
            from_employee="OTHER",
            to_employee="RECV",
            handover_date="2026-06-23",
            description="handover notes",
        )
    assert idor.harness.created == [], "a denied call must not create a document"


def test_create_handover_allows_minting_for_self(idor):
    """A plain Employee minting their own handover is permitted."""
    idor.persona(roles=["Employee"], employee="SELF")
    res = idor.handover.create_handover(
        leave_application="LA-0001",
        from_employee="SELF",
        to_employee="RECV",
        handover_date="2026-06-23",
        description="handover notes",
    )
    assert res["message"]  # created successfully
    assert len(idor.harness.created) == 1


def test_create_handover_manager_mints_for_anyone(idor):
    """An HR Manager may mint a handover for any from_employee."""
    idor.persona(roles=["HR Manager"], employee="SELF")
    res = idor.handover.create_handover(
        leave_application="LA-0001",
        from_employee="ANYONE",
        to_employee="RECV",
        handover_date="2026-06-23",
        description="handover notes",
    )
    assert res["message"]
    assert len(idor.harness.created) == 1


# --------------------------------------------------------------------------- #
# handover._get_owned_or_managed / update_handover_status
# --------------------------------------------------------------------------- #
def test_get_owned_or_managed_denies_unrelated(idor):
    """A non-manager touching a handover they are neither from nor to is denied."""
    idor.persona(roles=["Employee"], employee="SELF")
    idor.harness.doc = _FakeDoc(name="HT-0001", from_employee="A", to_employee="B")
    with pytest.raises(_PermissionDenied):
        idor.handover._get_owned_or_managed("HT-0001")


def test_get_owned_or_managed_allows_receiver(idor):
    """The to_employee (receiver) owns the task and may load it."""
    idor.persona(roles=["Employee"], employee="SELF")
    idor.harness.doc = _FakeDoc(name="HT-0001", from_employee="A", to_employee="SELF")
    doc = idor.handover._get_owned_or_managed("HT-0001")
    assert doc.name == "HT-0001"


def test_get_owned_or_managed_allows_originator(idor):
    """The from_employee (departing) owns the task and may load it."""
    idor.persona(roles=["Employee"], employee="SELF")
    idor.harness.doc = _FakeDoc(name="HT-0001", from_employee="SELF", to_employee="B")
    assert idor.handover._get_owned_or_managed("HT-0001").name == "HT-0001"


def test_update_handover_status_denies_unrelated_no_save(idor):
    """A non-owner, non-manager may not drive the lifecycle — and nothing saves."""
    idor.persona(roles=["Employee"], employee="SELF")
    idor.harness.doc = _FakeDoc(name="HT-0001", from_employee="A", to_employee="B", status="Pending")
    with pytest.raises(_PermissionDenied):
        idor.handover.update_handover_status("HT-0001", "Completed")
    assert not idor.harness.doc.saved, "a denied call must not save the document"


def test_update_handover_status_receiver_allowed(idor):
    """The receiver (to_employee) may complete a handover they own."""
    idor.persona(roles=["Employee"], employee="SELF")
    idor.harness.doc = _FakeDoc(name="HT-0001", from_employee="A", to_employee="SELF", status="Pending")
    res = idor.handover.update_handover_status("HT-0001", "Completed")
    assert res["status"] == "Completed"
    assert idor.harness.doc.saved


def test_update_handover_status_hr_user_treated_as_manager(idor):
    """Handover's manager set includes HR User — so it bypasses ownership."""
    idor.persona(roles=["HR User"], employee="SELF")
    idor.harness.doc = _FakeDoc(name="HT-0001", from_employee="A", to_employee="B", status="Pending")
    res = idor.handover.update_handover_status("HT-0001", "Completed")
    assert res["status"] == "Completed"


# --------------------------------------------------------------------------- #
# handover.leave_handovers — non-manager list must be scoped to involvement
# --------------------------------------------------------------------------- #
def test_leave_handovers_filters_to_involvement(idor):
    """A non-manager sees only rows where they are from or to_employee."""
    idor.persona(roles=["Employee"], employee="SELF")
    idor.harness.list_rows = [
        {"name": "HT-1", "from_employee": "SELF", "to_employee": "B"},
        {"name": "HT-2", "from_employee": "A", "to_employee": "SELF"},
        {"name": "HT-3", "from_employee": "A", "to_employee": "B"},  # leaked?
    ]
    out = idor.handover.leave_handovers()
    names = {r["name"] for r in out}
    assert "HT-1" in names and "HT-2" in names
    assert "HT-3" not in names, "an unrelated handover leaked across the scope"


def test_leave_handovers_manager_sees_all(idor):
    """An HR Manager sees every row regardless of involvement."""
    idor.persona(roles=["HR Manager"], employee="SELF")
    idor.harness.list_rows = [
        {"name": "HT-1", "from_employee": "A", "to_employee": "B"},
        {"name": "HT-2", "from_employee": "C", "to_employee": "D"},
    ]
    out = idor.handover.leave_handovers()
    assert len(out) == 2


def test_my_handovers_always_self_scoped(idor):
    """my_handovers filters by to_employee == caller — no cross-employee leak."""
    idor.persona(roles=["Employee"], employee="SELF")
    idor.harness.list_rows = [
        {"name": "HT-1", "to_employee": "SELF"},
        {"name": "HT-2", "to_employee": "OTHER"},
    ]
    # The scoping mechanism is the emitted DB filter — the stub ``get_all``
    # returns ``list_rows`` verbatim (it does not apply the filter), so we
    # assert on the recorded filter clause rather than the raw rows: the
    # function must pin ``to_employee`` to the caller, never to a peer.
    out = idor.handover.my_handovers()
    assert idor.harness.list_calls, "expected a list query"
    filters = idor.harness.list_calls[-1]["filters"]
    # DNA §6.6 B: list-filter form (a dict can't hold a date range on one
    # field) — the scoping clause must still pin to_employee to the caller.
    assert ["to_employee", "=", "SELF"] in filters
    assert not any(
        c[0] == "to_employee" and c[2] != "SELF" for c in filters if len(c) == 3
    )
    assert len(out) == len(idor.harness.list_rows)  # passthrough — DB applies filter


# --------------------------------------------------------------------------- #
# plan-test-complete-hr-extra §3.3 (G7) — leave_extra + employee_services IDOR
# --------------------------------------------------------------------------- #
def test_leave_extra_my_denies_other_employee(idor):  # ID-01
    """A plain Employee may not list someone else's encashments."""
    idor.persona(roles=["Employee"], employee="SELF")
    with pytest.raises(Exception):
        idor.leave_extra.my_leave_encashments(employee="OTHER")
    with pytest.raises(Exception):
        idor.leave_extra.my_comp_off_requests(employee="OTHER")


def test_services_my_denies_other_employee(idor):  # ID-02 / ID-03
    """A plain Employee may not list someone else's grievances / travels."""
    idor.persona(roles=["Employee"], employee="SELF")
    with pytest.raises(Exception):
        idor.employee_services.my_grievances(employee="OTHER")
    with pytest.raises(Exception):
        idor.employee_services.my_travel_requests(employee="OTHER")


def test_manager_my_bypasses_self_scope(idor):  # ID-04
    """HR Manager may query any employee's rows (intended bypass)."""
    idor.persona(roles=["HR Manager"], employee="SELF")
    idor.leave_extra.my_leave_encashments(employee="OTHER")  # must not raise
    idor.employee_services.my_grievances(employee="OTHER")  # must not raise
    idor.employee_services.my_travel_requests(employee="OTHER")  # must not raise


@pytest.mark.parametrize(
    "fn",
    [
        lambda m: m.leave_extra.approve_leave_encashment(name="X"),
        lambda m: m.leave_extra.reject_leave_encashment(name="X"),
        lambda m: m.leave_extra.approve_comp_off(name="X"),
        lambda m: m.leave_extra.reject_comp_off(name="X"),
        lambda m: m.employee_services.resolve_grievance(name="X"),
        lambda m: m.employee_services.approve_travel_request(name="X"),
        lambda m: m.employee_services.reject_travel_request(name="X"),
    ],
)
def test_actions_deny_plain_employee(idor, fn):  # ID-05
    """Every approve/reject/resolve endpoint denies a plain Employee."""
    idor.persona(roles=["Employee"], employee="SELF")
    with pytest.raises(Exception):
        fn(idor)


@pytest.mark.parametrize(
    "fn",
    [
        lambda m: m.leave_extra.all_leave_encashments(),
        lambda m: m.leave_extra.all_comp_off_requests(),
        lambda m: m.employee_services.all_grievances(),
        lambda m: m.employee_services.all_travel_requests(),
    ],
)
def test_all_lists_deny_plain_employee(idor, fn):  # ID-06
    """Every all_* endpoint denies a plain Employee."""
    idor.persona(roles=["Employee"], employee="SELF")
    with pytest.raises(Exception):
        fn(idor)


def test_hr_user_manager_set_allowed(idor):  # ID-07
    """HR User is inside the manager set for both modules."""
    idor.persona(roles=["HR User"], employee="SELF")
    idor.leave_extra.all_leave_encashments()  # must not raise
    idor.employee_services.all_travel_requests()  # must not raise
    idor.employee_services.resolve_grievance(name="X")  # manager gate passes
