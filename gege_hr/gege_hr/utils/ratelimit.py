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

    F18 fix: the previous version called ``frappe.cache().get_connection()`` —
    a method that does not exist on Frappe v15's RedisWrapper — so the
    AttributeError was raised and swallowed on EVERY call and the limit never
    fired. ``RedisWrapper`` subclasses ``redis.Redis``, so ``incr``/``expire``
    are called on the wrapper directly (same pattern as ``utils.naming``).
    The ``frappe.throw`` is raised OUTSIDE the try block so it cannot be
    swallowed by the infra-failure guard.
    """
    if frappe is None:
        return

    full_key = f"gege_hr:rl:{key}"
    count = 0
    try:
        cache = frappe.cache()
        count = int(cache.incr(full_key))
        if count == 1:
            cache.expire(full_key, window_seconds)
    except Exception:
        # Never block the request because of a rate-limit infra failure.
        if frappe:
            frappe.log_error(title="ratelimit skipped (cache unavailable)")
        return

    if count > max_requests:
        exc = getattr(frappe, "TooManyRequestsError", None) or frappe.ValidationError
        frappe.throw(
            f"Quá nhiều yêu cầu. Vui lòng đợi {window_seconds} giây rồi thử lại.",
            exc,
        )
