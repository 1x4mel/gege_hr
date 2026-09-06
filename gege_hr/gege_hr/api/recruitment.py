"""NEW-5 (hr-gap-audit 🟥) — Recruitment + Training API (full no-desk lifecycle).

Reuses Frappe HR's native DocTypes — ``Job Opening`` / ``Job Applicant`` /
``Interview Round`` / ``Interview`` / ``Job Offer`` / ``Employee Onboarding`` /
``Training Event`` / ``Employee Training`` — and exposes a DNA-compliant
surface per plan-recruitment-full-frontend.md:

* P1 — Job Opening CRUD + close/open, Job Applicant pipeline + CV + detail.
* P2 — Interview Rounds, scheduling, interviewer feedback, reschedule.
* P3 — Job Offer create/decide + Employee Onboarding handoff.

Existing read/list endpoints (openings, applicants, training) are unchanged so
the current SPA keeps working. All mutating endpoints gate on
``_require_manager()`` and do NOT bypass role permissions (the grants live in
``api/setup_permissions.py``); only the public-ish ``submit_job_application``
keeps ``ignore_permissions=True`` as before.
"""

from __future__ import annotations

import frappe

from gege_hr.gege_hr.utils import pagination

OPENING_DOCTYPE = "Job Opening"
APPLICANT_DOCTYPE = "Job Applicant"
TRAINING_EVENT_DOCTYPE = "Training Event"
EMP_TRAINING_DOCTYPE = "Employee Training"
ROUND_DOCTYPE = "Interview Round"
INTERVIEW_DOCTYPE = "Interview"
INTERVIEW_DETAIL_DOCTYPE = "Interview Detail"
OFFER_DOCTYPE = "Job Offer"
ONBOARDING_DOCTYPE = "Employee Onboarding"

_MANAGER_ROLES = {"HR Manager", "HR User", "System Manager"}

# Standard Select options per HRMS schema (job_opening.json / job_applicant.json
# / interview.json / job_offer.json). Hard-coded so gear-popover dropdowns are
# never empty (DNA §6.3); merged with distinct data values in
# ``recruitment_filter_options`` in case the site carries extras.
OPENING_STATUSES = ["Open", "Closed"]
APPLICANT_STATUSES = ["Open", "Replied", "Rejected", "Hold", "Accepted"]
INTERVIEW_STATUSES = ["Pending", "Under Review", "Cleared", "Rejected"]
OFFER_STATUSES = ["Awaiting Response", "Accepted", "Rejected"]

# Statuses an interviewer may pick when submitting feedback (Pending is the
# pre-feedback state — it is set by ``schedule_interview`` only).
_FEEDBACK_STATUSES = {"Under Review", "Cleared", "Rejected"}

# Job Applicant state machine (plan §0) — validated in ``set_applicant_status``
# and surfaced as ``can.set_status`` in ``get_applicant``.
_APPLICANT_TRANSITIONS = {
    "Open": {"Replied", "Hold", "Rejected"},
    "Replied": {"Hold", "Rejected", "Accepted"},
    "Hold": {"Replied", "Rejected"},
    "Rejected": {"Open"},  # mở lại
    "Accepted": set(),  # terminal → Employee Onboarding
}

_ACTIVE_APPLICANT_STATUSES = ["Open", "Replied", "Hold"]

_OPENING_FIELDS = [
    "name",
    "designation",
    "company",
    "status",
    "description",
    "posting_date",
    "vacancies",
]
_APPLICANT_FIELDS = [
    "name",
    "applicant_name",
    "email_id",
    "phone_number",
    # ``job_title`` IS the Link → Job Opening in this HRMS (label "Job Opening").
    "job_title",
    "status",
    "designation",
]
_EVENT_FIELDS = ["name", "event_name", "trainer_name", "start_time", "end_time", "status", "location"]
_EMP_TRAINING_FIELDS = ["name", "employee", "employee_name", "training_event", "status"]
_ROUND_FIELDS = ["name", "round_name", "designation", "interview_type", "expected_average_rating"]
_INTERVIEW_FIELDS = [
    "name",
    "job_applicant",
    "interview_round",
    "scheduled_on",
    "from_time",
    "to_time",
    "status",
    "average_rating",
]
_OFFER_FIELDS = ["name", "job_applicant", "offer_date", "designation", "company", "status"]

# Broad-search (or_filters LIKE) fields per DocType — cover EVERY content column
# incl. display-name FKs (company / trainer_name) so broad search finds them too
# (DNA view-design-dna.md §6.6 A / Law #3).
_OPENING_SEARCH = ["name", "designation", "company", "description"]
_APPLICANT_SEARCH = ["name", "applicant_name", "email_id", "job_title", "job_opening"]
_EVENT_SEARCH = ["name", "event_name", "trainer_name", "location"]
_EMP_TRAINING_SEARCH = ["name", "employee_name", "training_event"]
_ROUND_SEARCH = ["name", "round_name", "designation"]
_INTERVIEW_SEARCH = ["name", "job_applicant", "interview_round"]
_OFFER_SEARCH = ["name", "job_applicant", "designation", "company"]


def _is_manager() -> bool:
    roles = set(frappe.get_roles(frappe.session.user))
    return bool(roles & _MANAGER_ROLES)


def _require_manager() -> None:
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager được thao tác.")


def _list(doctype, fields, filters, status, search, page, page_size, search_fields=None) -> dict:
    flt = list(filters or [])
    if status:
        flt.append(["status", "=", status])
    or_filters = None
    q = (search or "").strip()
    if q:
        like = f"%{pagination.escape_like(q)}%"
        sf = search_fields or ["name"]
        or_filters = [[field, "like", like] for field in sf]
    page = max(1, int(page or 1))
    page_size = max(1, int(page_size or 20))
    try:
        rows = (
            frappe.get_all(
                doctype,
                filters=flt or None,
                or_filters=or_filters,
                fields=fields,
                order_by="creation desc",
                limit_start=(page - 1) * page_size,
                limit_page_length=page_size,
            )
            or []
        )
        total = len(
            frappe.get_all(
                doctype, filters=flt or None, or_filters=or_filters, fields=["name"], limit_page_length=0
            )
            or []
        )
    except Exception:
        frappe.log_error(title=f"recruitment.list {doctype} failed")
        rows, total = [], 0
    return {"data": rows, "total": total}


def _count(doctype, filters) -> int:
    return len(frappe.get_all(doctype, filters=filters, fields=["name"], limit_page_length=0) or [])


def _doc_dict(doc, fields) -> dict:
    return {f: getattr(doc, f, None) for f in fields}


def _today() -> str:
    """Today's date; safe fallback for the bench-free test stub."""
    try:
        from frappe.utils import nowdate

        return nowdate()
    except Exception:
        return "2026-01-01"


def _applicant_interviews(applicant: str) -> list:
    return (
        frappe.get_all(
            INTERVIEW_DOCTYPE,
            filters=[["job_applicant", "=", applicant]],
            fields=_INTERVIEW_FIELDS,
            order_by="scheduled_on asc",
            limit_page_length=0,
        )
        or []
    )


def _applicant_offer(applicant: str):
    rows = (
        frappe.get_all(
            OFFER_DOCTYPE,
            filters=[["job_applicant", "=", applicant]],
            fields=_OFFER_FIELDS,
            order_by="creation desc",
            limit_page_length=1,
        )
        or []
    )
    return rows[0] if rows else None


def _all_rounds_cleared(interviews: list) -> bool:
    return bool(interviews) and all(iv.get("status") == "Cleared" for iv in interviews)


# ── Recruitment: openings list / apply (unchanged surface) ────────────────────
@frappe.whitelist()
def list_job_openings(status=None, search=None, page=1, page_size=20):
    # Status is now view-controlled (default "Open" is set on the FE filter),
    # so an explicit None means "all" — do NOT force "Open" server-side (DNA Law #2).
    return _list(OPENING_DOCTYPE, _OPENING_FIELDS, None, status, search, page, page_size, _OPENING_SEARCH)


@frappe.whitelist()
def submit_job_application(
    job_opening=None, applicant_name=None, email_id=None, phone_number=None, cover_letter=None
):
    if not (applicant_name or "").strip() or not (email_id or "").strip():
        frappe.throw("Cần tên + email ứng viên.")
    opening_designation = None
    if job_opening:
        opening_designation = frappe.db.get_value(OPENING_DOCTYPE, job_opening, "designation")
    doc = frappe.new_doc(APPLICANT_DOCTYPE)
    doc.applicant_name = applicant_name.strip()
    doc.email_id = email_id.strip()
    doc.phone_number = phone_number or ""
    # ``job_title`` is a Link → Job Opening (stock HRMS semantics): store the
    # opening NAME; ``designation`` then fetches from job_title.designation.
    # Interview.validate_designation later matches the round against it.
    doc.job_title = job_opening or ""
    if opening_designation:
        doc.designation = opening_designation
    doc.cover_letter = cover_letter or ""
    doc.status = "Open"
    doc.insert(ignore_permissions=True)
    return {"name": doc.name, "applicant_name": doc.applicant_name}


@frappe.whitelist()
def all_applicants(status=None, search=None, page=1, page_size=20):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager xem ứng viên.")
    return _list(
        APPLICANT_DOCTYPE, _APPLICANT_FIELDS, None, status, search, page, page_size, _APPLICANT_SEARCH
    )


# ── Recruitment: Job Opening admin (P1) ───────────────────────────────────────
@frappe.whitelist()
def save_job_opening(payload=None, **kwargs):
    """Create (no ``name``) / update (with ``name``) a Job Opening. HR-only."""
    _require_manager()
    data = dict(payload or kwargs)
    designation = (data.get("designation") or "").strip()
    company = (data.get("company") or "").strip()
    if not designation or not company:
        frappe.throw("Cần chức danh (designation) + công ty.")
    try:
        vacancies = max(1, int(data.get("vacancies") or 1))
    except (TypeError, ValueError):
        vacancies = 1

    # This site marks Job Opening.job_title mandatory — it is the display
    # title, so mirror the designation into it (HRMS standard behaviour).
    name = data.get("name")
    if name:
        doc = frappe.get_doc(OPENING_DOCTYPE, name)
        doc.designation = designation
        doc.job_title = (data.get("job_title") or "").strip() or designation
        doc.company = company
        doc.description = data.get("description") or getattr(doc, "description", "") or ""
        doc.vacancies = vacancies
        doc.save()
    else:
        doc = frappe.new_doc(OPENING_DOCTYPE)
        doc.designation = designation
        doc.job_title = (data.get("job_title") or "").strip() or designation
        doc.company = company
        doc.description = data.get("description") or ""
        doc.vacancies = vacancies
        doc.status = "Open"
        doc.insert()
    return {"name": doc.name, "status": getattr(doc, "status", None) or "Open"}


@frappe.whitelist()
def set_job_opening_status(name=None, status=None, force=False):
    """Open ↔ Closed. Closing guards against applicants still in progress
    (Open/Replied/Hold); ``force=True`` (manager) overrides the guard."""
    _require_manager()
    if not name:
        frappe.throw("Cần tên vị trí.")
    if status not in OPENING_STATUSES:
        frappe.throw("Trạng thái vị trí không hợp lệ.")
    if status == "Closed" and not force:
        active = _count(
            APPLICANT_DOCTYPE,
            [["job_title", "=", name], ["status", "in", _ACTIVE_APPLICANT_STATUSES]],
        )
        if active:
            frappe.throw(f"Vị trí đang có {active} ứng viên đang xử lý — không thể đóng.")
    frappe.db.set_value(OPENING_DOCTYPE, name, "status", status)
    return {"name": name, "status": status}


@frappe.whitelist()
def delete_job_opening(name=None):
    _require_manager()
    if not name:
        frappe.throw("Cần tên vị trí.")
    linked = _count(APPLICANT_DOCTYPE, [["job_title", "=", name]])
    if linked:
        frappe.throw(f"Vị trí đang có {linked} ứng viên liên kết — không xoá được.")
    frappe.delete_doc(OPENING_DOCTYPE, name)
    return {"name": name}


@frappe.whitelist()
def get_job_opening(name=None):
    _require_manager()
    if not name:
        frappe.throw("Cần tên vị trí.")
    doc = frappe.get_doc(OPENING_DOCTYPE, name)
    active = _count(
        APPLICANT_DOCTYPE,
        [["job_title", "=", name], ["status", "in", _ACTIVE_APPLICANT_STATUSES]],
    )
    total = _count(APPLICANT_DOCTYPE, [["job_title", "=", name]])
    return {
        "doc": _doc_dict(doc, _OPENING_FIELDS),
        "applicant_count": total,
        "active_applicant_count": active,
        "can": {
            "edit": True,
            "close": (getattr(doc, "status", None) or "Open") == "Open",
            "delete": total == 0,
        },
    }


# ── Recruitment: Job Applicant pipeline (P1) ─────────────────────────────────
@frappe.whitelist()
def get_applicant(name=None):
    """Full applicant context: doc + resume files + interviews + offer +
    server-computed action matrix (``can``) per the §0 state machine."""
    _require_manager()
    if not name:
        frappe.throw("Cần tên ứng viên.")
    doc = frappe.get_doc(APPLICANT_DOCTYPE, name)
    data = _doc_dict(doc, _APPLICANT_FIELDS + ["cover_letter", "designation"])
    interviews = _applicant_interviews(name)
    offer = _applicant_offer(name)
    try:
        resume = (
            frappe.get_all(
                "File",
                filters=[
                    ["attached_to_doctype", "=", APPLICANT_DOCTYPE],
                    ["attached_to_name", "=", name],
                ],
                fields=["name", "file_name", "file_url"],
                limit_page_length=0,
            )
            or []
        )
    except Exception:
        frappe.log_error(title=f"recruitment.resume {name} failed")
        resume = []
    status = data.get("status") or "Open"
    return {
        "doc": data,
        "resume": resume,
        "interviews": interviews,
        "offer": offer,
        "all_rounds_cleared": _all_rounds_cleared(interviews),
        "can": {
            "set_status": sorted(_APPLICANT_TRANSITIONS.get(status, set())),
            "schedule_interview": status not in {"Accepted", "Rejected"},
            "create_offer": status not in {"Accepted", "Rejected"},
        },
    }


@frappe.whitelist()
def set_applicant_status(name=None, status=None):
    """Move an applicant along the pipeline — validated against the §0 matrix."""
    _require_manager()
    if not name or not status:
        frappe.throw("Cần ứng viên + trạng thái.")
    if status not in APPLICANT_STATUSES:
        frappe.throw("Trạng thái ứng viên không hợp lệ.")
    current = frappe.db.get_value(APPLICANT_DOCTYPE, name, "status")
    if status == current:
        frappe.throw(f"Ứng viên đã ở trạng thái {status}.")
    if status not in _APPLICANT_TRANSITIONS.get(current, set()):
        frappe.throw(f"Không thể chuyển từ '{current}' sang '{status}'.")
    frappe.db.set_value(APPLICANT_DOCTYPE, name, "status", status)
    return {"name": name, "status": status}


@frappe.whitelist()
def update_applicant(name=None, phone_number=None, email_id=None, cover_letter=None):
    _require_manager()
    if not name:
        frappe.throw("Cần ứng viên.")
    doc = frappe.get_doc(APPLICANT_DOCTYPE, name)
    for field, value in (
        ("phone_number", phone_number),
        ("email_id", email_id),
        ("cover_letter", cover_letter),
    ):
        if value is not None:
            setattr(doc, field, value)
    doc.save()
    return {"name": name}


# ── Recruitment: Interview Rounds + Interviews (P2) ───────────────────────────
@frappe.whitelist()
def list_interview_rounds(designation=None):
    _require_manager()
    flt = [["designation", "=", designation]] if designation else None
    rows = frappe.get_all(ROUND_DOCTYPE, filters=flt, fields=_ROUND_FIELDS, limit_page_length=0) or []
    return {"data": rows, "total": len(rows)}


@frappe.whitelist()
def save_interview_round(payload=None, **kwargs):
    """HR creates an Interview Round (round_name + designation + interviewers
    list of user emails) — required before any Interview can be scheduled."""
    _require_manager()
    data = dict(payload or kwargs)
    round_name = (data.get("round_name") or "").strip()
    designation = (data.get("designation") or "").strip()
    interviewers = data.get("interviewers") or []
    if not round_name or not designation or not interviewers:
        frappe.throw("Cần tên vòng, chức danh + người phỏng vấn.")
    doc = frappe.new_doc(ROUND_DOCTYPE)
    doc.round_name = round_name
    doc.designation = designation
    doc.interview_type = data.get("interview_type") or ""
    if data.get("expected_average_rating") is not None:
        doc.expected_average_rating = data.get("expected_average_rating")
    # Child rows must go through doc.append (plain dicts break _set_defaults).
    for user in interviewers:  # Table MultiSelect ``Interviewer`` — fieldname ``user``
        doc.append("interviewers", {"user": user})
    # ``expected_skill_set`` (child) is mandatory on this site — default the
    # round name itself as the single skill, creating the Skill master row if
    # missing (HR Manager gets a create grant in setup_permissions).
    skills = [s.strip() for s in (data.get("expected_skills") or []) if s and s.strip()]
    if not skills:
        skills = [round_name]
    for skill in skills:
        if not frappe.db.exists("Skill", skill):
            try:
                frappe.get_doc({"doctype": "Skill", "skill_name": skill}).insert()
            except Exception:
                frappe.log_error(title=f"recruitment.skill {skill} create failed")
        if frappe.db.exists("Skill", skill):
            doc.append("expected_skill_set", {"skill": skill})
    doc.insert()
    return {"name": doc.name, "round_name": doc.round_name}


@frappe.whitelist()
def schedule_interview(payload=None, **kwargs):
    """Create an Interview (status Pending) for an applicant. ``job_opening``
    is copied from the applicant's custom Link so per-opening funnels work."""
    _require_manager()
    data = dict(payload or kwargs)
    applicant = data.get("job_applicant")
    round_name = data.get("interview_round")
    scheduled_on = data.get("scheduled_on")
    interviewers = data.get("interviewers") or []
    if not applicant or not round_name or not scheduled_on or not interviewers:
        frappe.throw("Cần ứng viên, vòng phỏng vấn, ngày + người phỏng vấn.")
    if not frappe.db.get_value(ROUND_DOCTYPE, round_name, "name"):
        frappe.throw("Vòng phỏng vấn không tồn tại.")
    # ``job_title`` IS the applicant's Link → Job Opening (stock HRMS), so the
    # Interview.job_opening fetch_from resolves to a valid Link naturally.
    job_opening = frappe.db.get_value(APPLICANT_DOCTYPE, applicant, "job_title") or ""
    doc = frappe.new_doc(INTERVIEW_DOCTYPE)
    doc.job_applicant = applicant
    doc.interview_round = round_name
    doc.job_opening = job_opening
    doc.scheduled_on = scheduled_on
    # from_time / to_time are mandatory on this site — default a 1h slot.
    doc.from_time = data.get("from_time") or "09:00:00"
    doc.to_time = data.get("to_time") or "10:00:00"
    # Child table ``Interview Detail`` — fieldname ``interviewer`` (Link User).
    for user in interviewers:
        doc.append("interview_details", {"interviewer": user})
    doc.status = "Pending"
    doc.insert()
    return {"name": doc.name, "interview_round": round_name}


@frappe.whitelist()
def list_interviews(status=None, search=None, page=1, page_size=20):
    _require_manager()
    return _list(
        INTERVIEW_DOCTYPE, _INTERVIEW_FIELDS, None, status, search, page, page_size, _INTERVIEW_SEARCH
    )


@frappe.whitelist()
def my_interviews(status=None, search=None, page=1, page_size=20):
    """Interviews where the current user is an interviewer (no HR role needed)."""
    names = (
        frappe.get_all(
            INTERVIEW_DETAIL_DOCTYPE,
            filters={"interviewer": frappe.session.user},
            pluck="parent",
            limit_page_length=0,
        )
        or []
    )
    flt = [["name", "in", names or ["__none__"]]]
    return _list(
        INTERVIEW_DOCTYPE, _INTERVIEW_FIELDS, flt, status, search, page, page_size, _INTERVIEW_SEARCH
    )


@frappe.whitelist()
def submit_interview_feedback(name=None, average_rating=None, interview_summary=None, status=None):
    """Interviewer-owned (or HR) feedback. ``average_rating`` / ``interview_summary``
    are ``allow_on_submit`` on Interview, and the doc may already be submitted —
    so updates go through ``db.set_value`` which is safe for both docstates.
    Returns ``all_rounds_cleared`` so the FE can suggest creating the offer."""
    if not name:
        frappe.throw("Cần buổi phỏng vấn.")
    doc = frappe.get_doc(INTERVIEW_DOCTYPE, name)
    if not _is_manager():
        is_interviewer = frappe.get_all(
            INTERVIEW_DETAIL_DOCTYPE,
            filters={
                "parent": name,
                "parenttype": INTERVIEW_DOCTYPE,
                "interviewer": frappe.session.user,
            },
            limit_page_length=1,
        )
        if not is_interviewer:
            frappe.throw("Chỉ người phỏng vấn hoặc HR được nhận xét.")
    if status is not None and status not in _FEEDBACK_STATUSES:
        frappe.throw("Kết quả phỏng vấn không hợp lệ.")
    values = {}
    if average_rating is not None:
        values["average_rating"] = average_rating
    if interview_summary is not None:
        values["interview_summary"] = interview_summary
    if status is not None:
        values["status"] = status
    if not values:
        frappe.throw("Không có gì để cập nhật.")
    frappe.db.set_value(INTERVIEW_DOCTYPE, name, values)
    interviews = _applicant_interviews(getattr(doc, "job_applicant", None))
    return {"name": name, "all_rounds_cleared": _all_rounds_cleared(interviews)}


@frappe.whitelist()
def reschedule_interview(name=None, scheduled_on=None, from_time=None, to_time=None):
    _require_manager()
    if not name or not scheduled_on:
        frappe.throw("Cần buổi phỏng vấn + ngày mới.")
    values = {"scheduled_on": scheduled_on}
    if from_time:
        values["from_time"] = from_time
    if to_time:
        values["to_time"] = to_time
    frappe.db.set_value(INTERVIEW_DOCTYPE, name, values)
    return {"name": name, "scheduled_on": scheduled_on}


# ── Recruitment: Job Offer + onboarding handoff (P3) ─────────────────────────
@frappe.whitelist()
def save_job_offer(payload=None, **kwargs):
    _require_manager()
    data = dict(payload or kwargs)
    applicant = data.get("job_applicant")
    offer_date = data.get("offer_date")
    if not applicant or not offer_date:
        frappe.throw("Cần ứng viên + ngày offer.")
    if frappe.db.get_value(APPLICANT_DOCTYPE, applicant, "status") == "Rejected":
        frappe.throw("Ứng viên đã bị từ chối — không tạo được offer.")
    if not data.get("force") and not _all_rounds_cleared(_applicant_interviews(applicant)):
        frappe.throw("Ứng viên chưa đủ vòng phỏng vấn Cleared — không tạo được offer.")

    job_opening = frappe.db.get_value(APPLICANT_DOCTYPE, applicant, "job_title") or ""
    designation = (
        (data.get("designation") or "").strip()
        or frappe.db.get_value(APPLICANT_DOCTYPE, applicant, "designation")
        or (frappe.db.get_value(OPENING_DOCTYPE, job_opening, "designation") if job_opening else "")
        or ""
    )
    company = (
        (data.get("company") or "").strip()
        or (frappe.db.get_value(OPENING_DOCTYPE, job_opening, "company") if job_opening else "")
        or ""
    )
    if not designation or not company:
        frappe.throw("Cần chức danh + công ty cho offer (không suy ra được từ ứng viên).")
    doc = frappe.new_doc(OFFER_DOCTYPE)
    doc.job_applicant = applicant
    # applicant_name / designation / company are mandatory on this site.
    doc.applicant_name = frappe.db.get_value(APPLICANT_DOCTYPE, applicant, "applicant_name") or ""
    doc.offer_date = offer_date
    doc.designation = designation
    doc.company = company
    doc.status = "Awaiting Response"
    doc.insert()
    return {"name": doc.name, "status": doc.status}


@frappe.whitelist()
def set_job_offer_status(name=None, status=None, create_onboarding=False):
    """Decide an offer (Accepted/Rejected). Accepted/Rejected propagate to the
    applicant (offer flow bypasses the §0 matrix as intended). Optionally
    creates an Employee Onboarding doc and returns its name for deep-linking."""
    _require_manager()
    if not name or not status:
        frappe.throw("Cần offer + trạng thái.")
    if status not in {"Accepted", "Rejected"}:
        frappe.throw("Trạng thái offer không hợp lệ.")
    doc = frappe.get_doc(OFFER_DOCTYPE, name)
    frappe.db.set_value(OFFER_DOCTYPE, name, "status", status)

    applicant = getattr(doc, "job_applicant", None)
    if applicant:
        current = frappe.db.get_value(APPLICANT_DOCTYPE, applicant, "status")
        if current not in {"Accepted", "Rejected"}:
            frappe.db.set_value(APPLICANT_DOCTYPE, applicant, "status", status)

    onboarding = None
    if status == "Accepted" and create_onboarding:
        try:
            ob = frappe.new_doc(ONBOARDING_DOCTYPE)
            ob.job_applicant = applicant
            # Mandatory on this site: job_offer / employee_name / joining dates.
            ob.job_offer = name
            ob.employee_name = frappe.db.get_value(
                APPLICANT_DOCTYPE, applicant, "applicant_name"
            ) or ""
            ob.designation = getattr(doc, "designation", "") or ""
            ob.company = getattr(doc, "company", "") or ""
            join_date = getattr(doc, "offer_date", None) or _today()
            ob.date_of_joining = join_date
            ob.boarding_begins_on = join_date
            ob.insert()
            onboarding = ob.name
        except Exception:
            frappe.log_error(title=f"recruitment.onboarding {name} failed")
            onboarding = None
    return {"name": name, "status": status, "applicant": applicant, "onboarding": onboarding}


@frappe.whitelist()
def get_job_offer(name=None):
    _require_manager()
    if not name:
        frappe.throw("Cần offer.")
    doc = frappe.get_doc(OFFER_DOCTYPE, name)
    return {"doc": _doc_dict(doc, _OFFER_FIELDS)}


# ── Training ─────────────────────────────────────────────────────────────────
@frappe.whitelist()
def list_training_events(status=None, search=None, page=1, page_size=20):
    return _list(TRAINING_EVENT_DOCTYPE, _EVENT_FIELDS, None, status, search, page, page_size, _EVENT_SEARCH)


@frappe.whitelist()
def enroll_training(employee=None, training_event=None):
    if not employee or not training_event:
        frappe.throw("Cần nhân viên + lớp đào tạo.")
    if not _is_manager():
        own = frappe.db.get_value("Employee", {"user_id": frappe.session.user})
        if employee != own:
            frappe.throw("Chỉ được đăng ký đào tạo cho chính mình.")
        if frappe.db.exists(EMP_TRAINING_DOCTYPE, {"employee": employee, "training_event": training_event}):
            frappe.throw("Nhân viên đã đăng ký lớp đào tạo này.")
    doc = frappe.new_doc(EMP_TRAINING_DOCTYPE)
    doc.employee = employee
    doc.training_event = training_event
    doc.status = "Mandatory"
    doc.insert(ignore_permissions=True)
    return {"name": doc.name, "training_event": training_event}


@frappe.whitelist()
def my_training(employee=None, status=None, page=1, page_size=20):
    if not employee:
        emp = frappe.db.get_value("Employee", {"user_id": frappe.session.user})
        if not emp:
            frappe.throw("Tài khoản chưa liên kết nhân viên.")
        employee = emp
    if not _is_manager():
        own = frappe.db.get_value("Employee", {"user_id": frappe.session.user})
        if employee != own:
            frappe.throw("Chỉ xem được đào tạo của chính mình.")
    return _list(
        EMP_TRAINING_DOCTYPE,
        _EMP_TRAINING_FIELDS,
        [["employee", "=", employee]],
        status,
        None,
        page,
        page_size,
    )


@frappe.whitelist()
def all_training(status=None, search=None, page=1, page_size=20):
    if not _is_manager():
        frappe.throw("Chỉ HR/Manager xem tất cả đào tạo.")
    return _list(
        EMP_TRAINING_DOCTYPE,
        _EMP_TRAINING_FIELDS,
        None,
        status,
        search,
        page,
        page_size,
        _EMP_TRAINING_SEARCH,
    )


@frappe.whitelist()
def recruitment_filter_options() -> dict:
    """Status options per recruitment DocType — feeds the gear popover
    SearchableSelect (DNA §6.3) so no dropdown is ever empty. Standard options
    are hard-coded (∪ distinct data values) so a status with 0 rows still
    appears in the filter."""

    def _distinct(doctype, field):
        try:
            rows = frappe.get_all(doctype, fields=[field], limit_page_length=0)
        except Exception:
            frappe.log_error(title=f"recruitment.options {doctype} failed")
            return []
        return sorted({r.get(field) for r in rows if r.get(field)})

    def _merged(doctype, field, standard):
        return sorted(set(standard) | set(_distinct(doctype, field)))

    return {
        "opening_statuses": _merged(OPENING_DOCTYPE, "status", OPENING_STATUSES),
        "event_statuses": _merged(TRAINING_EVENT_DOCTYPE, "status", []),
        "training_statuses": _distinct(EMP_TRAINING_DOCTYPE, "status"),
        "applicant_statuses": _merged(APPLICANT_DOCTYPE, "status", APPLICANT_STATUSES),
        "interview_statuses": _merged(INTERVIEW_DOCTYPE, "status", INTERVIEW_STATUSES),
        "offer_statuses": _merged(OFFER_DOCTYPE, "status", OFFER_STATUSES),
    }
