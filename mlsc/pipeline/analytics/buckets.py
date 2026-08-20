"""Converts a document's publication instant to a calendar day in the
monitor's own timezone.

A monitor in Jakarta and one in London disagree about which day a document
published near midnight UTC belongs to. Bucketing in UTC would silently shift
one monitor's series by a day relative to what its users actually
experienced (design.md, "Domain shapes": ``Bucket``).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


class BucketAmbiguous(ValueError):
    """A naive timestamp reached the bucketing boundary.

    A missing timezone means a boundary upstream failed to normalise, and
    guessing one would shift a series silently (design.md, "Failure
    strategy")."""


def bucket_for(instant: datetime, *, timezone: str) -> date:
    if instant.tzinfo is None:
        raise BucketAmbiguous(f"{instant!r} has no timezone")
    local = instant.astimezone(ZoneInfo(timezone))
    return local.date()


def bucket_range_utc(bucket: date, *, timezone: str) -> tuple[datetime, datetime]:
    """The half-open UTC instant range ``[start, end)`` covering one calendar
    day in the monitor's timezone.

    A query filtering ``published_at`` by a UTC day boundary would silently
    include or exclude documents near midnight for any monitor not in UTC
    (the same failure ``bucket_for`` exists to prevent) — this is the range
    every query against ``Document.published_at`` for one bucket must use.
    """
    zone = ZoneInfo(timezone)
    start = datetime(bucket.year, bucket.month, bucket.day, tzinfo=zone)
    end = start + timedelta(days=1)
    return start.astimezone(ZoneInfo("UTC")), end.astimezone(ZoneInfo("UTC"))
