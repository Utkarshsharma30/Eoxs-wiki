"""Business-timezone helper for call dates. EOXS's US-side call activity
(sales, clients like Eastern States Steel / IMS Metals) is Eastern-time
business hours, so a call's calendar date should be the ET date, not the
raw UTC date -- a call after ~8pm EDT / 9pm EST otherwise lands on the
next UTC day, disagreeing with what any Eastern-time viewer (or Fireflies'
own UI, rendered in the viewer's local timezone) considers "today".
"""
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")


def et_date(dt_utc):
    """Given a tz-aware UTC (or any tz-aware) datetime, return the
    America/New_York calendar date -- correctly handles the EST/EDT
    daylight-saving transition, unlike a fixed UTC-5/-4 offset."""
    if dt_utc is None:
        return None
    return dt_utc.astimezone(EASTERN).date()
