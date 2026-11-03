from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class DevelopmentTimeError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ResolvedLocalInstant:
    instant_utc: datetime
    timezone: str
    local_time: datetime
    utc_offset_minutes: int


def resolve_local_instant(
    local_time: datetime,
    timezone: str,
    *,
    utc_offset_minutes: int | None = None,
) -> ResolvedLocalInstant:
    if local_time.tzinfo is not None:
        raise DevelopmentTimeError(
            "local_time_must_be_naive",
            "Local calendar time must not carry a timezone; provide timezone separately.",
        )
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise DevelopmentTimeError("timezone_invalid", "Calendar timezone must be a valid IANA identifier.") from exc

    candidates: list[tuple[int, datetime, datetime]] = []
    for fold in (0, 1):
        aware = local_time.replace(tzinfo=zone, fold=fold)
        instant = aware.astimezone(UTC)
        round_trip = instant.astimezone(zone).replace(tzinfo=None)
        if round_trip != local_time:
            continue
        offset = aware.utcoffset()
        if offset is None:
            continue
        offset_minutes = int(offset / timedelta(minutes=1))
        candidate = (offset_minutes, instant, aware)
        if candidate not in candidates:
            candidates.append(candidate)

    if not candidates:
        raise DevelopmentTimeError(
            "local_time_nonexistent",
            "Calendar local time falls in a DST gap and cannot be scheduled without changing the wall-clock time.",
        )

    distinct = {(offset, instant) for offset, instant, _ in candidates}
    if len(distinct) > 1:
        if utc_offset_minutes is None:
            raise DevelopmentTimeError(
                "local_time_ambiguous",
                "Calendar local time is ambiguous across a DST fold; an explicit UTC offset is required.",
            )
        candidates = [candidate for candidate in candidates if candidate[0] == utc_offset_minutes]
        if len(candidates) != 1:
            raise DevelopmentTimeError(
                "utc_offset_invalid",
                "The supplied UTC offset does not resolve the ambiguous local calendar time.",
            )
    elif utc_offset_minutes is not None and candidates[0][0] != utc_offset_minutes:
        raise DevelopmentTimeError(
            "utc_offset_invalid",
            "The supplied UTC offset does not match the local calendar time.",
        )

    offset_minutes, instant, aware = candidates[0]
    return ResolvedLocalInstant(
        instant_utc=instant,
        timezone=timezone,
        local_time=aware.replace(tzinfo=None),
        utc_offset_minutes=offset_minutes,
    )


def resolve_interval(
    start_local: datetime,
    end_local: datetime,
    timezone: str,
    *,
    start_utc_offset_minutes: int | None = None,
    end_utc_offset_minutes: int | None = None,
) -> tuple[ResolvedLocalInstant, ResolvedLocalInstant]:
    start = resolve_local_instant(
        start_local,
        timezone,
        utc_offset_minutes=start_utc_offset_minutes,
    )
    end = resolve_local_instant(
        end_local,
        timezone,
        utc_offset_minutes=end_utc_offset_minutes,
    )
    if end.instant_utc <= start.instant_utc:
        raise DevelopmentTimeError("calendar_interval_invalid", "Calendar end instant must be after start instant.")
    return start, end
