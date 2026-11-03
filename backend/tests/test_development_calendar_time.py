from datetime import datetime

import pytest

from app.modules.development.time import DevelopmentTimeError, resolve_interval, resolve_local_instant


def test_dst_gap_is_rejected() -> None:
    with pytest.raises(DevelopmentTimeError, match="DST gap") as exc_info:
        resolve_local_instant(datetime(2026, 3, 29, 2, 30), "Europe/Rome")
    assert exc_info.value.code == "local_time_nonexistent"


def test_dst_fold_requires_explicit_offset() -> None:
    with pytest.raises(DevelopmentTimeError, match="ambiguous") as exc_info:
        resolve_local_instant(datetime(2026, 10, 25, 2, 30), "Europe/Rome")
    assert exc_info.value.code == "local_time_ambiguous"


def test_dst_fold_accepts_valid_explicit_offset() -> None:
    resolved = resolve_local_instant(
        datetime(2026, 10, 25, 2, 30),
        "Europe/Rome",
        utc_offset_minutes=60,
    )
    assert resolved.utc_offset_minutes == 60
    assert resolved.instant_utc.isoformat() == "2026-10-25T01:30:00+00:00"


def test_invalid_zone_is_rejected() -> None:
    with pytest.raises(DevelopmentTimeError) as exc_info:
        resolve_local_instant(datetime(2026, 9, 12, 12, 0), "Europe/DefinitelyNotAZone")
    assert exc_info.value.code == "timezone_invalid"


def test_interval_compares_resolved_instants() -> None:
    start, end = resolve_interval(
        datetime(2026, 9, 12, 12, 0),
        datetime(2026, 9, 12, 13, 0),
        "Europe/Rome",
    )
    assert end.instant_utc > start.instant_utc


def test_interval_resolves_fold_endpoints_independently() -> None:
    start, end = resolve_interval(
        datetime(2026, 10, 25, 2, 15),
        datetime(2026, 10, 25, 2, 45),
        "Europe/Rome",
        start_utc_offset_minutes=120,
        end_utc_offset_minutes=60,
    )
    assert start.utc_offset_minutes == 120
    assert end.utc_offset_minutes == 60
    assert end.instant_utc > start.instant_utc
