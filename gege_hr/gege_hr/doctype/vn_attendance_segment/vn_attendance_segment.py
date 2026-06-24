from __future__ import unicode_literals

from frappe.model.document import Document


class VNAttendanceSegment(Document):
    """A single computed time-slice inside a Work Session (child table).

    Populated by ``calculate_work_session`` (plan §9.2): each segment records its
    type (Regular / OT / Late / Leave …), night/holiday/weekend flags, and the
    salary-component multiplier used downstream by payroll. ``hours`` is derived
    from ``from_datetime``/``to_datetime``.
    """

    def validate(self):
        if self.from_datetime and self.to_datetime and self.to_datetime < self.from_datetime:
            # Defensive — the engine always emits ordered segments.
            self.to_datetime, self.from_datetime = self.from_datetime, self.to_datetime
        self._compute_hours()

    def _compute_hours(self):
        if not (self.from_datetime and self.to_datetime):
            return
        from datetime import datetime

        try:
            start = datetime.strptime(str(self.from_datetime)[:19], "%Y-%m-%d %H:%M:%S")
            end = datetime.strptime(str(self.to_datetime)[:19], "%Y-%m-%d %H:%M:%S")
            delta = (end - start).total_seconds() / 3600.0
            self.hours = round(max(0.0, delta), 4)
        except Exception:
            return
