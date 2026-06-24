from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.model.document import Document


class VNWorkLocation(Document):
    """VN Work Location — geofence + (optional) Wi-Fi/IP validation target.

    GPS coordinates together with ``allowed_radius_meters`` define the geofence
    used by ``attendance.mobile_checkin`` (server-side) and the hr-ui client
    pre-check (plan §19.2).
    """

    def validate(self):
        self._validate_geofence()

    def _validate_geofence(self):
        has_lat = self.latitude is not None
        has_lng = self.longitude is not None
        if has_lat != has_lng:
            frappe.throw(_("Vui lòng nhập cả Latitude và Longitude, hoặc bỏ trống cả hai."))
        if (has_lat or has_lng) and (self.allowed_radius_meters or 0) <= 0:
            frappe.throw(_("Allowed Radius phải lớn hơn 0 khi có tọa độ GPS."))
