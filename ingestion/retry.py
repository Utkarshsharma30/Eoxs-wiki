"""Shared retry/backoff helper, mirroring the Render pipeline's proven
per-source backoff logic (Fireflies: Retry-After-aware exponential backoff
up to 8 retries; Fathom: 15*2^n capped at 240s, 5 retries). Consolidated
here since the underlying pattern is identical across sources -- only the
API-specific error-detection (which exception/status means "rate limited")
differs, and that's left to the caller via is_retryable.
"""
import logging
import time

logger = logging.getLogger("ingestion.retry")


def call_with_retry(fn, *, max_retries=8, base_delay=5, max_delay=120,
                     is_retryable=None, retry_after_getter=None):
    """Calls fn() and retries on failure per is_retryable(exception).

    retry_after_getter(exception) -> seconds|None lets a caller honor an
    API's own Retry-After value (Fireflies/Fathom both send one) in
    preference to the exponential backoff schedule.
    """
    is_retryable = is_retryable or (lambda e: True)

    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as e:
            if attempt >= max_retries or not is_retryable(e):
                raise

            wait = None
            if retry_after_getter:
                wait = retry_after_getter(e)
            if wait is None:
                wait = min(base_delay * (2 ** attempt), max_delay)

            logger.warning("retry %d/%d after %ss: %s", attempt + 1, max_retries, wait, e)
            time.sleep(wait)
