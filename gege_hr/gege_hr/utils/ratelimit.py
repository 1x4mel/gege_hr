"""
Rate limiting for attendance write paths.

Per plan v5 §Security: server-side rate limit on mobile_checkin
(max 1 request / 3s per employee). Uses Redis ``INCR`` + EXPIRE to avoid the
get→set race condition.
"""

from __future__ import annotations

try:
    import frappe  # type: ignore
except Exception:  # pragma: no cover
    frappe = None


def rate_limit(key: str, max_requests: int = 1, window_seconds: int = 3) -> None:
    """Raise a validation error if ``key`` exceeds ``max_requests`` in the window.

    Implementation uses Redis INCR + EXPIRE (atomic, race-free). When Redis is
    unavailable (dev/no bench), the limit is skipped so the flow stays usable.
    """
    if frappe is None:
        return
    redis = None
    try:
        redis = frappe.cache().get_connection()  # type: ignore[attr-defined]
    except Exception:
        redis = None
    if redis is None:
        return

    full_key = f"gege_hr:rl:{key}"
    try:
        count = redis.incr(full_key)
        if count == 1:
            redis.expire(full_key, window_seconds)
        if count > max_requests:
            frappe.throw(
                f"Quá nhiều yêu cầu. Vui lòng đợi {window_seconds} giây rồi thử lại.",
                frappe.RateLimitError if hasattr(frappe, "RateLimitError") else frappe.ValidationError,
            )
    except Exception:
        # Never block the request because of a rate-limit infra failure.
        return
