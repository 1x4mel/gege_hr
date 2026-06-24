from __future__ import unicode_literals

from frappe.model.document import Document


class VNLeaveCalendarCache(Document):
    """Materialised leave-calendar snapshot keyed by scope+month
    (plan v5 §11.6 / doctype-design §29).

    ``cache_key`` format ``{company}-{branch|ALL}-{dept|ALL}-{YYYY}-{MM}`` (v2.0).
    ``data_json`` holds ``[{employee, employee_name, leaves: [{date, leave_type,
    half_day}]}]`` consumed by the team/leave calendar views. Auto-invalidate is
    triggered by the ``Leave Application`` on_submit/on_cancel hooks (see
    ``api/leave.on_leave_submit`` / ``on_leave_cancel``) which recompute and
    upsert the matching cache row.
    """

    def validate(self):
        self._normalize_key()

    def _normalize_key(self):
        """Derive cache_key from scope when blank so the Master is queryable."""
        if self.cache_key:
            return
        branch = self.branch or "ALL"
        department = self.department or "ALL"
        self.cache_key = "{company}-{branch}-{department}-{year}-{month}".format(
            company=self.company or "ALL",
            branch=branch,
            department=department,
            year=self.year or "",
            month=self.month or "",
        )
