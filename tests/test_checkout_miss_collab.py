"""Bench-free unit tests for the checkout-miss collaboration layer —
plans/plan-checkout-miss-deskfree-complete.md §5.1 group A (S1).

Covers (CM2-A*):
  A1  comment_checkout_miss / get_checkout_miss_comments / timeline merge
  A2  assign_checkout_miss (ToDo sync) + detail assignees key
  A3  remove_checkout_miss_evidence (File delete + evidence_ref strip)
  A4  download_checkout_miss_pdf (owner/HR guard + passthrough)
  A5  bulk_extend_checkout_miss_grace (partial-safe, cap, guards)
  A6  run_checkout_miss_engine_now (role guard + summary)

Same stub-frappe harness as tests/test_checkout_miss_api.py.
"""

import sys
import types

import pytest

from tests.test_checkout_miss_api import FrappeError, _ticket, api


# --------------------------------------------------------------------------- #
# Test-injection helpers — optional Frappe machinery the stub lacks.
# --------------------------------------------------------------------------- #
def _inject_assign_to(monkeypatch, calls):
    """Fake ``frappe.desk.form.assign_to`` recording add/remove payloads."""
    form_mod = types.ModuleType("frappe.desk.form")
    form_mod.assign_to = types.SimpleNamespace(
        add=lambda payload: calls.append(("add", payload)),
        remove=lambda *a: calls.append(("remove", a)),
    )
    desk_mod = types.ModuleType("frappe.desk")
    desk_mod.form = form_mod
    monkeypatch.setitem(sys.modules, "frappe.desk", desk_mod)
    monkeypatch.setitem(sys.modules, "frappe.desk.form", form_mod)


def _inject_print_format(monkeypatch, calls):
    """Fake ``frappe.utils.print_format.download_pdf`` recorder."""
    pf = types.ModuleType("frappe.utils.print_format")
    pf.download_pdf = lambda *a, **kw: calls.append((a, kw))
    monkeypatch.setitem(sys.modules, "frappe.utils.print_format", pf)


# --------------------------------------------------------------------------- #
# A1 — comment thread
# --------------------------------------------------------------------------- #
def test_cm2_a1_1_hr_comment_creates_comment_doc(api):
    stub, mod = api(tickets=[_ticket()])
    out = mod.comment_checkout_miss("CM-0001", "Đã kiểm tra camera cửa")
    comments = [d for d in stub.created_docs if d.get("doctype") == "Comment"]
    assert len(comments) == 1
    assert comments[0]["reference_name"] == "CM-0001"
    assert comments[0]["content"] == "Đã kiểm tra camera cửa"
    assert out["ticket"] == "CM-0001"
    assert "comments" in out


def test_cm2_a1_2_empty_or_too_long_comment_throws(api):
    stub, mod = api(tickets=[_ticket()])
    with pytest.raises(FrappeError):
        mod.comment_checkout_miss("CM-0001", "   ")
    with pytest.raises(FrappeError):
        mod.comment_checkout_miss("CM-0001", "x" * 2001)
    assert not [d for d in stub.created_docs if d.get("doctype") == "Comment"]


def test_cm2_a1_3_stranger_cannot_comment(api):
    stub, mod = api(tickets=[_ticket()], roles=("Employee",), employee_for_user=None)
    with pytest.raises(FrappeError):
        mod.comment_checkout_miss("CM-0001", "lý do")
    assert not [d for d in stub.created_docs if d.get("doctype") == "Comment"]


def test_cm2_a1_4_owner_can_comment_without_hr_role(api):
    stub, mod = api(tickets=[_ticket()], roles=("Employee",), employee_for_user="HR-EMP-001")
    mod.comment_checkout_miss("CM-0001", "tôi có việc đột xuất")
    assert len([d for d in stub.created_docs if d.get("doctype") == "Comment"]) == 1


def test_cm2_a1_5_timeline_merges_comment_source(api):
    stub, mod = api(
        tickets=[_ticket()],
        comment_rows=[
            {
                "comment_type": "Comment",
                "reference_doctype": "VN Checkout Miss",
                "reference_name": "CM-0001",
                "owner": "hr@example.com",
                "creation": "2026-08-12 10:00:00",
                "content": "bình luận mẫu",
            }
        ],
    )
    tl = mod.get_checkout_miss("CM-0001")["timeline"]
    assert any(r["source"] == "comment" and r["description"] == "bình luận mẫu" for r in tl)


def test_cm2_a1_6_get_comments_owner_guard(api):
    stub, mod = api(
        tickets=[_ticket()],
        roles=("Employee",),
        employee_for_user="HR-EMP-002",  # not the ticket's employee
    )
    with pytest.raises(FrappeError):
        mod.get_checkout_miss_comments("CM-0001")


# --------------------------------------------------------------------------- #
# A2 — assign (ToDo sync)
# --------------------------------------------------------------------------- #
def test_cm2_a2_1_assign_creates_todo_docs(api):
    stub, mod = api(tickets=[_ticket()])
    out = mod.assign_checkout_miss("CM-0001", ["a@x.com", "a@x.com", "b@x.com"])
    todos = [d for d in stub.created_docs if d.get("doctype") == "ToDo"]
    assert len(todos) == 2  # dedup happened
    assert {t["allocated_to"] for t in todos} == {"a@x.com", "b@x.com"}
    assert all(t["reference_name"] == "CM-0001" for t in todos)
    assert out["ticket"] == "CM-0001"


def test_cm2_a2_2_assign_empty_removes_existing(api):
    stub, mod = api(
        tickets=[_ticket()],
        todo_rows=[
            {
                "reference_type": "VN Checkout Miss",
                "reference_name": "CM-0001",
                "status": "Open",
                "allocated_to": "a@x.com",
                "owner": "hr@example.com",
                "assigned_by": "hr@example.com",
                "creation": "2026-08-12 09:00:00",
                "name": "TD-1",
            }
        ],
    )
    mod.assign_checkout_miss("CM-0001", [])
    assert ("ToDo", "TD-1") in stub.deleted


def test_cm2_a2_3_assign_requires_hr(api):
    stub, mod = api(tickets=[_ticket()], roles=("Employee",), employee_for_user="HR-EMP-001")
    with pytest.raises(FrappeError):
        mod.assign_checkout_miss("CM-0001", ["a@x.com"])


def test_cm2_a2_4_detail_returns_assignees_and_attachments_keys(api):
    stub, mod = api(
        tickets=[_ticket()],
        todo_rows=[
            {
                "reference_type": "VN Checkout Miss",
                "reference_name": "CM-0001",
                "status": "Open",
                "owner": "a@x.com",
                "assigned_by": "hr@example.com",
                "creation": "2026-08-12 09:00:00",
            }
        ],
        file_rows=[
            {
                "attached_to_doctype": "VN Checkout Miss",
                "attached_to_name": "CM-0001",
                "file_url": "/files/a.png",
                "file_name": "a.png",
                "file_size": 1024,
                "name": "F1",
            }
        ],
    )
    out = mod.get_checkout_miss("CM-0001")
    assert out["assignees"][0]["user"] == "a@x.com"
    assert out["attachments"][0]["file_url"] == "/files/a.png"


# --------------------------------------------------------------------------- #
# A3 — evidence removal
# --------------------------------------------------------------------------- #
def test_cm2_a3_1_remove_evidence_deletes_file_and_strips_url(api):
    stub, mod = api(
        tickets=[_ticket(evidence_ref="/files/a.png /files/b.png")],
        file_rows=[
            {
                "attached_to_doctype": "VN Checkout Miss",
                "attached_to_name": "CM-0001",
                "file_url": "/files/a.png",
                "file_name": "a.png",
                "file_size": 1024,
                "name": "F1",
            }
        ],
    )
    out = mod.remove_checkout_miss_evidence("CM-0001", "/files/a.png")
    assert ("File", "F1") in stub.deleted
    assert stub.tickets["CM-0001"]["evidence_ref"] == "/files/b.png"
    assert out["evidence_ref"] == "/files/b.png"


def test_cm2_a3_2_remove_evidence_refused_when_payroll_locked(api):
    stub, mod = api(
        tickets=[_ticket(evidence_ref="/files/a.png")],
        period={"name": "P1", "status": "Approved"},
        has_slip=True,
    )
    with pytest.raises(FrappeError):
        mod.remove_checkout_miss_evidence("CM-0001", "/files/a.png")
    assert stub.deleted == []


# --------------------------------------------------------------------------- #
# A4 — PDF print
# --------------------------------------------------------------------------- #
def test_cm2_a4_1_hr_download_pdf_passthrough(api, monkeypatch):
    calls = []
    _inject_print_format(monkeypatch, calls)
    stub, mod = api(tickets=[_ticket()])
    out = mod.download_checkout_miss_pdf("CM-0001")
    assert len(calls) == 1
    a, kw = calls[0]
    assert a[0] == "VN Checkout Miss" and a[1] == "CM-0001"
    assert kw.get("format") == mod.PRINT_FORMAT
    assert out["format"] == mod.PRINT_FORMAT


def test_cm2_a4_2_stranger_cannot_download(api, monkeypatch):
    calls = []
    _inject_print_format(monkeypatch, calls)
    stub, mod = api(tickets=[_ticket()], roles=("Employee",), employee_for_user="HR-EMP-009")
    with pytest.raises(FrappeError):
        mod.download_checkout_miss_pdf("CM-0001")
    assert calls == []


def test_cm2_a4_3_owner_can_download(api, monkeypatch):
    calls = []
    _inject_print_format(monkeypatch, calls)
    stub, mod = api(tickets=[_ticket()], roles=("Employee",), employee_for_user="HR-EMP-001")
    mod.download_checkout_miss_pdf("CM-0001")
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# A5 — bulk grace extension
# --------------------------------------------------------------------------- #
def test_cm2_a5_1_bulk_grace_partial_safe(api):
    stub, mod = api(
        tickets=[
            _ticket("CM-0001", grace_deadline="2026-08-13 18:00:00"),
            _ticket("CM-0002", grace_deadline="2026-08-13 18:00:00"),
            _ticket("CM-0009", status="Closed", grace_deadline="2026-08-13 18:00:00"),
        ]
    )
    res = mod.bulk_extend_checkout_miss_grace(
        ["CM-0001", "CM-0002", "CM-0009"], "2026-08-14 09:30", "NV có việc đột xuất"
    )
    assert res["updated"] == ["CM-0001", "CM-0002"]
    assert [f["name"] for f in res["failed"]] == ["CM-0009"]
    assert res["counts"]["extended"] == 2


def test_cm2_a5_2_bulk_grace_cap_100(api):
    stub, mod = api(tickets=[_ticket()])
    with pytest.raises(FrappeError):
        mod.bulk_extend_checkout_miss_grace([f"CM-{i}" for i in range(101)], "2026-08-14 09:30", "r")
    assert stub.saved_docs == []


def test_cm2_a5_3_bulk_grace_reason_mandatory(api):
    stub, mod = api(tickets=[_ticket()])
    with pytest.raises(FrappeError):
        mod.bulk_extend_checkout_miss_grace(["CM-0001"], "2026-08-14 09:30", "  ")
    assert stub.saved_docs == []


def test_cm2_a5_4_bulk_grace_past_deadline_fails_every_row(api):
    stub, mod = api(tickets=[_ticket()])
    res = mod.bulk_extend_checkout_miss_grace(["CM-0001"], "2026-08-01 09:00", "lý do")
    assert res["updated"] == []
    assert "tương lai" in res["failed"][0]["error"]


# --------------------------------------------------------------------------- #
# A6 — run engine now
# --------------------------------------------------------------------------- #
def test_cm2_a6_1_plain_hr_user_cannot_run_engine(api):
    stub, mod = api(tickets=[_ticket()], roles=("HR User",))
    with pytest.raises(FrappeError):
        mod.run_checkout_miss_engine_now()


def test_cm2_a6_2_manager_run_returns_engine_summary(api, monkeypatch):
    # NOTE: patch AFTER api() — the fixture reloads the engine module, which
    # would discard a pre-fixture monkeypatch.
    stub, mod = api(tickets=[_ticket()], roles=("HR Manager",))
    import gege_hr.gege_hr.utils.checkout_miss as engine_mod

    monkeypatch.setattr(engine_mod, "run_hourly", lambda: {"closed": 2, "penalised": 1})
    out = mod.run_checkout_miss_engine_now()
    assert out == {"closed": 2, "penalised": 1}


def test_cm2_a6_3_engine_returning_none_is_tolerated(api, monkeypatch):
    stub, mod = api(tickets=[_ticket()], roles=("Payroll Manager",))
    import gege_hr.gege_hr.utils.checkout_miss as engine_mod

    monkeypatch.setattr(engine_mod, "run_hourly", lambda: None)
    assert mod.run_checkout_miss_engine_now() == {"closed": 0, "penalised": 0}
