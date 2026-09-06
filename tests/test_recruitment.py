"""Bench-free unit tests for ``api/recruitment.py`` (NEW-5 + plan-recruitment-full-frontend)."""

import importlib
import sys
import types

import pytest


class _Doc:
    def __init__(self, doctype, store):
        self.doctype = doctype
        self._store = store
        self.name = None
        self.status = "Open"

    def insert(self, ignore_permissions=False):
        self.name = self.name or f"{self.doctype.replace(' ', '-')}-{len(self._store) + 1:04d}"
        self._store[(self.doctype, self.name)] = self
        return self

    def save(self):
        if not self.name or (self.doctype, self.name) not in self._store:
            return self.insert()
        return self

    def append(self, key, row):
        if not isinstance(getattr(self, key, None), list):
            setattr(self, key, [])
        getattr(self, key).append(row)
        return self


def _match(rv, op, val):
    if op == "in":
        return rv in (val or [])
    if op == "like":
        return str(val or "").strip("%").lower() in str(rv or "").lower()
    return rv == val


class _Frappe:
    def __init__(self):
        self.store = {}
        self.list_rows = {}
        self.values = {}  # (doctype, name, field) -> value (docs not in store)
        self.deleted = []
        self.opening_designation = "Sale Lead"
        self.employee_for_user = "HR-EMP-1"
        self.roles = {"HR Manager"}

    def whitelist(self, fn=None, **k):
        return fn if fn is not None else (lambda f: f)

    def _(self, s):
        return s

    def log_error(self, *a, **k):
        return None

    def throw(self, msg, *a, **k):
        raise Exception(msg)

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

    def delete_doc(self, doctype, name):
        self.store.pop((doctype, name), None)
        self.deleted.append((doctype, name))

    def new_doc(self, doctype):
        return _Doc(doctype, self.store)

    class _DB:
        def __init__(self, fr):
            self.fr = fr

        def get_value(self, doctype, name, field=None, as_dict=False):
            # Employee-by-filters lookup (my_training legacy behaviour)
            if doctype == "Employee" and isinstance(name, dict):
                return self.fr.employee_for_user
            doc = self.fr.store.get((doctype, name))
            if doc is not None and isinstance(field, str):
                return getattr(doc, field, None)
            key = (doctype, name, field)
            if key in self.fr.values:
                return self.fr.values[key]
            if doctype == "Job Opening" and name and field == "designation":
                return self.fr.opening_designation
            return None

        def exists(self, doctype, name):
            return (doctype, name) in self.fr.store

        def set_value(self, doctype, name, field, value=None, **k):
            doc = self.fr.store.get((doctype, name))
            values = field if isinstance(field, dict) else {field: value}
            if doc is not None:
                for key, val in values.items():
                    setattr(doc, key, val)
            else:
                for key, val in values.items():
                    self.fr.values[(doctype, name, key)] = val
            return name

    @property
    def db(self):
        return self._DB(self)

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
        **k,
    ):
        rows = list(self.list_rows.get(doctype, []))

        def conds(items):
            if isinstance(items, dict):
                return [[key, "=", val] for key, val in items.items()]
            return [c for c in (items or []) if not isinstance(c, str)]

        def keep(r):
            for cond in conds(filters):
                if not _match(
                    r.get(cond[0]), cond[1] if len(cond) > 2 else "=", cond[2] if len(cond) > 2 else cond[1]
                ):
                    return False
            if or_filters:
                if not any(
                    _match(r.get(c[0]), c[1] if len(c) > 2 else "=", c[2] if len(c) > 2 else c[1])
                    for c in conds(or_filters)
                ):
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
    m = importlib.import_module("gege_hr.gege_hr.api.recruitment")
    importlib.reload(m)
    return m, stub


# ── recruitment (legacy surface — must keep working) ─────────────────────────
def test_list_job_openings(mod):
    m, stub = mod
    stub.list_rows["Job Opening"] = [{"name": "JO-1", "designation": "Sale Lead", "status": "Open"}]
    res = m.list_job_openings()
    assert res["total"] == 1


def test_submit_job_application_validates(mod):
    m, _ = mod
    with pytest.raises(Exception):
        m.submit_job_application(applicant_name="", email_id="x@y.z")
    res = m.submit_job_application(
        job_opening="JO-1", applicant_name="Nguyen A", email_id="a@b.c", phone_number="090"
    )
    assert res["applicant_name"] == "Nguyen A"
    # the applicant doc is stored by the stub
    assert any(k[0] == "Job Applicant" for k in _.store)


def test_all_applicants_requires_manager(mod):
    m, stub = mod
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        m.all_applicants()


# ── training (legacy) ─────────────────────────────────────────────────────────
def test_list_training_events(mod):
    m, stub = mod
    stub.list_rows["Training Event"] = [{"name": "TE-1", "event_name": "Sales 101", "status": "Scheduled"}]
    res = m.list_training_events()
    assert res["total"] == 1


def test_enroll_training_validates(mod):
    m, _ = mod
    with pytest.raises(Exception):
        m.enroll_training(employee=None, training_event=None)
    res = m.enroll_training(employee="HR-EMP-1", training_event="TE-1")
    assert res["training_event"] == "TE-1"


def test_my_training_filters_own(mod):
    m, stub = mod
    stub.list_rows["Employee Training"] = [
        {"name": "ET-1", "employee": "HR-EMP-1", "employee_name": "An"},
        {"name": "ET-2", "employee": "HR-EMP-2", "employee_name": "Binh"},
    ]
    res = m.my_training()
    assert res["total"] == 1
    assert res["data"][0]["name"] == "ET-1"


# ── B1–B6: Job Opening admin ─────────────────────────────────────────────────
def test_b1_save_job_opening_requires_designation(mod):
    m, _ = mod
    with pytest.raises(Exception):
        m.save_job_opening(payload={"designation": "", "company": "Gege"})


def test_b2_save_job_opening_update_keeps_single_doc(mod):
    m, stub = mod
    created = m.save_job_opening(payload={"designation": "Dev", "company": "Gege", "vacancies": 2})
    assert created["status"] == "Open"
    before = [k for k in stub.store if k[0] == "Job Opening"]
    updated = m.save_job_opening(
        payload={"name": created["name"], "designation": "Senior Dev", "company": "Gege", "vacancies": 3}
    )
    assert updated["name"] == created["name"]
    after = [k for k in stub.store if k[0] == "Job Opening"]
    assert len(after) == len(before) == 1
    assert stub.store[("Job Opening", created["name"])].vacancies == 3


def test_b3_close_opening_blocked_by_active_applicants(mod):
    m, stub = mod
    stub.list_rows["Job Applicant"] = [
        {"name": "JA-1", "job_title": "JO-X", "status": "Open"},
        {"name": "JA-2", "job_title": "JO-X", "status": "Replied"},
    ]
    with pytest.raises(Exception, match="2"):
        m.set_job_opening_status(name="JO-X", status="Closed")


def test_b4_close_opening_force_overrides(mod):
    m, stub = mod
    stub.list_rows["Job Applicant"] = [{"name": "JA-1", "job_title": "JO-X", "status": "Open"}]
    res = m.set_job_opening_status(name="JO-X", status="Closed", force=True)
    assert res["status"] == "Closed"
    assert stub.values[("Job Opening", "JO-X", "status")] == "Closed"


def test_b5_delete_opening_blocked_by_linked_applicants(mod):
    m, stub = mod
    stub.list_rows["Job Applicant"] = [{"name": "JA-1", "job_title": "JO-X", "status": "Rejected"}]
    with pytest.raises(Exception):
        m.delete_job_opening(name="JO-X")
    assert ("Job Opening", "JO-X") not in dict.fromkeys(stub.deleted)


def test_b6_get_job_opening_can_matrix(mod):
    m, stub = mod
    created = m.save_job_opening(payload={"designation": "Dev", "company": "Gege"})
    stub.list_rows["Job Applicant"] = [
        {"name": "JA-1", "job_title": created["name"], "status": "Open"},
        {"name": "JA-2", "job_title": created["name"], "status": "Accepted"},
    ]
    res = m.get_job_opening(name=created["name"])
    assert res["applicant_count"] == 2
    assert res["active_applicant_count"] == 1
    assert res["can"]["close"] is True
    assert res["can"]["delete"] is False


# ── B7–B10: applicant pipeline ───────────────────────────────────────────────
def _make_applicant(m, stub, status="Open"):
    res = m.submit_job_application(
        job_opening="JO-1", applicant_name="Nguyen A", email_id="a@b.c", phone_number="090"
    )
    doc = stub.store[("Job Applicant", res["name"])]
    doc.status = status
    return doc


def test_b7_set_applicant_status_open_to_replied(mod):
    m, stub = mod
    doc = _make_applicant(m, stub)
    res = m.set_applicant_status(name=doc.name, status="Replied")
    assert res["status"] == "Replied"
    assert stub.store[("Job Applicant", doc.name)].status == "Replied"


def test_b8_set_applicant_status_open_to_accepted_rejected(mod):
    m, stub = mod
    doc = _make_applicant(m, stub)
    with pytest.raises(Exception, match="Không thể chuyển"):
        m.set_applicant_status(name=doc.name, status="Accepted")


def test_b9_set_applicant_status_rejected_reopen(mod):
    m, stub = mod
    doc = _make_applicant(m, stub, status="Rejected")
    res = m.set_applicant_status(name=doc.name, status="Open")
    assert res["status"] == "Open"


def test_b10_submit_application_links_opening(mod):
    m, stub = mod
    res = m.submit_job_application(
        job_opening="JO-1", applicant_name="Tran B", email_id="b@b.c", phone_number=""
    )
    doc = stub.store[("Job Applicant", res["name"])]
    assert doc.job_title == "JO-1"  # job_title IS the Link -> Job Opening
    assert doc.designation == "Sale Lead"


# ── B11–B15 + B21: interviews ────────────────────────────────────────────────
def _make_round(m, stub):
    return m.save_interview_round(
        payload={
            "round_name": "Technical",
            "designation": "Sale Lead",
            "interviewers": ["i1@gege.local", "i2@gege.local"],
        }
    )


def test_b11_save_interview_round_validates(mod):
    m, _ = mod
    with pytest.raises(Exception):
        m.save_interview_round(payload={"round_name": "", "designation": "Dev", "interviewers": []})


def test_b12_schedule_interview(mod):
    m, stub = mod
    applicant = _make_applicant(m, stub)
    round_res = _make_round(m, stub)
    res = m.schedule_interview(
        payload={
            "job_applicant": applicant.name,
            "interview_round": round_res["name"],
            "scheduled_on": "2026-09-05",
            "from_time": "09:00:00",
            "to_time": "10:00:00",
            "interviewers": ["i1@gege.local"],
        }
    )
    doc = stub.store[("Interview", res["name"])]
    assert doc.status == "Pending"
    assert doc.job_opening == "JO-1"
    assert doc.interview_details == [{"interviewer": "i1@gege.local"}]
    assert doc.scheduled_on == "2026-09-05"


def test_b13_my_interviews_scopes_to_interviewer(mod):
    m, stub = mod
    stub.roles = {"Employee"}
    stub.list_rows["Interview Detail"] = [
        {"parent": "IV-1", "parenttype": "Interview", "interviewer": "hr@gege.local"}
    ]
    stub.list_rows["Interview"] = [
        {"name": "IV-1", "job_applicant": "JA-9", "interview_round": "R1", "status": "Pending"},
        {"name": "IV-2", "job_applicant": "JA-9", "interview_round": "R2", "status": "Pending"},
    ]
    res = m.my_interviews()
    assert res["total"] == 1
    assert res["data"][0]["name"] == "IV-1"


def test_b14_feedback_rejects_bad_status(mod):
    m, stub = mod
    doc = stub.new_doc("Interview")
    doc.name = "IV-2"
    doc.job_applicant = "JA-9"
    stub.store[("Interview", "IV-2")] = doc
    with pytest.raises(Exception):
        m.submit_interview_feedback(name="IV-2", status="Open")


def test_b15_feedback_cleared_flags_all_rounds(mod):
    m, stub = mod
    stub.list_rows["Interview"] = [
        {"name": "IV-0", "job_applicant": "JA-9", "interview_round": "R1", "status": "Cleared"}
    ]
    doc = stub.new_doc("Interview")
    doc.name = "IV-2"
    doc.job_applicant = "JA-9"
    stub.store[("Interview", "IV-2")] = doc
    res = m.submit_interview_feedback(
        name="IV-2", average_rating=4, interview_summary="Tốt", status="Cleared"
    )
    assert res["all_rounds_cleared"] is True
    assert stub.store[("Interview", "IV-2")].average_rating == 4


def test_b21_list_interviews_and_rounds(mod):
    m, stub = mod
    stub.list_rows["Interview"] = [
        {"name": "IV-1", "job_applicant": "JA-9", "interview_round": "R1", "status": "Pending"}
    ]
    stub.list_rows["Interview Round"] = [
        {"name": "R1", "round_name": "Technical", "designation": "Sale Lead"}
    ]
    assert m.list_interviews()["total"] == 1
    rounds = m.list_interview_rounds(designation="Sale Lead")
    assert rounds["total"] == 1
    assert rounds["data"][0]["round_name"] == "Technical"


# ── B16–B18: offers + onboarding handoff ─────────────────────────────────────
def _make_offer(m, stub):
    applicant = _make_applicant(m, stub, status="Replied")
    stub.list_rows["Interview"] = []  # not cleared → force needed
    res = m.save_job_offer(
        payload={
            "job_applicant": applicant.name,
            "offer_date": "2026-09-10",
            "force": True,
            "designation": "Sale Lead",
            "company": "Gege",
        }
    )
    return applicant, res


def test_b16_save_job_offer_requires_cleared_interviews(mod):
    m, stub = mod
    applicant = _make_applicant(m, stub, status="Replied")
    stub.list_rows["Interview"] = []
    with pytest.raises(Exception, match="Cleared"):
        m.save_job_offer(payload={"job_applicant": applicant.name, "offer_date": "2026-09-10"})
    # force=True bypasses for the manager (designation + company mandatory now)
    m.save_job_offer(
        payload={
            "job_applicant": applicant.name,
            "offer_date": "2026-09-10",
            "force": True,
            "designation": "Sale Lead",
            "company": "Gege",
        }
    )
    assert any(k[0] == "Job Offer" for k in stub.store)


def test_b17_offer_accepted_syncs_applicant(mod):
    m, stub = mod
    applicant, offer = _make_offer(m, stub)
    res = m.set_job_offer_status(name=offer["name"], status="Accepted")
    assert res["status"] == "Accepted"
    assert stub.store[("Job Offer", offer["name"])].status == "Accepted"
    assert stub.store[("Job Applicant", applicant.name)].status == "Accepted"


def test_b18_offer_accepted_creates_onboarding(mod):
    m, stub = mod
    _, offer = _make_offer(m, stub)
    res = m.set_job_offer_status(name=offer["name"], status="Accepted", create_onboarding=True)
    assert res["onboarding"]
    assert any(k[0] == "Employee Onboarding" for k in stub.store)


# ── B19: filter options ──────────────────────────────────────────────────────
def test_b19_recruitment_filter_options_standard_keys(mod):
    m, _ = mod
    res = m.recruitment_filter_options()
    assert set(res.keys()) == {
        "opening_statuses",
        "event_statuses",
        "training_statuses",
        "applicant_statuses",
        "interview_statuses",
        "offer_statuses",
    }
    assert "Open" in res["opening_statuses"]
    assert "Hold" in res["applicant_statuses"]
    assert "Under Review" in res["interview_statuses"]
    assert "Awaiting Response" in res["offer_statuses"]


# ── B20: permission gates ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "fn, kwargs",
    [
        ("save_job_opening", {"payload": {"designation": "D", "company": "C"}}),
        ("set_job_opening_status", {"name": "JO-1", "status": "Closed"}),
        ("delete_job_opening", {"name": "JO-1"}),
        ("get_applicant", {"name": "JA-1"}),
        ("set_applicant_status", {"name": "JA-1", "status": "Replied"}),
        (
            "save_interview_round",
            {"payload": {"round_name": "R", "designation": "D", "interviewers": ["u@x"]}},
        ),
        (
            "schedule_interview",
            {
                "payload": {
                    "job_applicant": "JA-1",
                    "interview_round": "R",
                    "scheduled_on": "2026-01-01",
                    "interviewers": ["u@x"],
                }
            },
        ),
        ("save_job_offer", {"payload": {"job_applicant": "JA-1", "offer_date": "2026-01-01"}}),
        ("set_job_offer_status", {"name": "JOF-1", "status": "Accepted"}),
    ],
)
def test_b20_manager_endpoints_deny_regular_users(mod, fn, kwargs):
    m, stub = mod
    stub.roles = {"Employee"}
    with pytest.raises(Exception):
        getattr(m, fn)(**kwargs)
