from __future__ import annotations

import socket
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from app.core.database import initialize_database
from app.core.paths import build_paths
from app.modules.environment import profiles
from app.modules.workspaces.models import WorkspaceCreate
from app.modules.workspaces.service import create_workspace


def _workspace() -> str:
    initialize_database()
    return create_workspace(WorkspaceCreate(name="Environment", slug="environment-test")).id


def _site(wid: str) -> dict[str, object]:
    return profiles.put_site(
        wid,
        {
            "name": "Boulder",
            "latitude": 39.742476,
            "longitude": -105.1786,
            "elevation_m": 1830.14,
            "timezone": "America/Denver",
            "water_body": "reservoir",
        },
        0,
    )


def test_site_validation_revision_cas_and_path_helpers() -> None:
    wid = _workspace()
    site = _site(wid)
    assert site["revision"] == 1
    with pytest.raises(profiles.EnvironmentProfileError, match="conflict"):
        profiles.put_site(wid, site, 0)
    assert build_paths().environment_site_file(wid).is_file()
    with pytest.raises(profiles.EnvironmentProfileError, match="latitude"):
        profiles.put_site(wid, {**site, "latitude": 91}, 1)
    with pytest.raises(profiles.EnvironmentProfileError, match="IANA"):
        profiles.put_site(wid, {**site, "timezone": "No/Such_Zone"}, 1)


def test_profile_digest_immutability_verification_pages_and_bounds() -> None:
    wid = _workspace()
    profile = profiles.create_profile(
        wid,
        name="test",
        timestamps=["2026-01-01T00:15:00Z", "2026-01-01T00:30:00Z"],
        channels={"ghi": [0.0, 10.0], "cloud_cover": [None, 0.5]},
        resolution_minutes=15,
        provenance={"kind": "test"},
    )
    assert profile["profile_id"] == profile["digest"]
    duplicate = profiles.create_profile(
        wid,
        name="test",
        timestamps=["2026-01-01T00:15:00Z", "2026-01-01T00:30:00Z"],
        channels={"ghi": [0.0, 10.0], "cloud_cover": [None, 0.5]},
        resolution_minutes=15,
        provenance={"kind": "test"},
    )
    assert duplicate["profile_id"] == profile["profile_id"]
    assert duplicate["artifact_id"] == profile["artifact_id"]
    assert profiles.read_profile(wid, profile["digest"], limit=1)["total"] == 2
    path = build_paths().environment_profiles_dir(wid) / (profile["digest"][7:] + ".json")
    with pytest.raises(FileExistsError):
        path.open("xb")
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(profiles.EnvironmentProfileError, match="verification"):
        profiles.read_profile(wid, profile["digest"])
    with pytest.raises(profiles.EnvironmentProfileError, match="ghi"):
        profiles.create_profile(
            wid,
            name="bad",
            timestamps=["2026-01-01T00:15:00Z"],
            channels={"ghi": [-1]},
            resolution_minutes=15,
            provenance={},
        )
    with pytest.raises(profiles.EnvironmentProfileError, match="NaN"):
        profiles._canonical({"value": float("nan")})
    assert profiles._canonical({"x": -0.0}) == b'{"x":0}'


def test_profile_read_resolution_aggregation_and_paging() -> None:
    wid = _workspace()
    profile = profiles.create_profile(
        wid,
        name="aggregate",
        timestamps=["2026-01-01T00:15:00Z", "2026-01-01T00:30:00Z", "2026-01-01T00:45:00Z", "2026-01-01T01:00:00Z"],
        channels={"ghi": [0.0, 10.0, 20.0, 30.0], "air_temperature": [280.0, 281.0, 282.0, 283.0]},
        resolution_minutes=15,
        provenance={"kind": "test"},
    )
    coarse = profiles.read_profile(wid, profile["digest"], resolution_minutes=30, limit=1)
    assert coarse["resolution_minutes"] == 30
    assert coarse["total"] == 2
    assert coarse["timestamps"] == ["2026-01-01T00:30:00Z"]
    assert coarse["channels"]["ghi"] == [5.0]
    assert coarse["channels"]["air_temperature"] == [281.0]


def test_spa_reference_clear_sky_scaling_and_synthetic_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    wid = _workspace()
    _site(wid)
    original_socket = socket.socket

    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("network socket creation attempted")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    original_spa = profiles._solar_position
    reference: dict[str, float] = {}

    def capture_spa(*args: object, **kwargs: object):
        result = original_spa(*args, **kwargs)
        reference["apparent_zenith"] = float(result.iloc[0]["apparent_zenith"])
        reference["azimuth"] = float(result.iloc[0]["azimuth"])
        return result

    monkeypatch.setattr(profiles, "_solar_position", capture_spa)
    profiles.generate(
        wid,
        {
            "kind": "clear_sky",
            "name": "SPA reference generator path",
            "parameters": {
                "start": "2003-10-17T19:28:00Z",
                "count": 1,
                "resolution_minutes": 5,
                "pressure_pa": 82_000,
                "temperature_c": 11,
                "delta_t": 67,
            },
        },
    )
    assert reference["apparent_zenith"] == pytest.approx(50.11162, abs=1e-4)
    assert reference["azimuth"] == pytest.approx(194.34024, abs=1e-4)
    start = "2026-06-21T04:00:00Z"
    generated = profiles.generate(
        wid,
        {
            "kind": "clear_sky",
            "name": "sky",
            "parameters": {"start": start, "count": 96, "resolution_minutes": 15, "clearness_factor": 1.0},
        },
    )
    half = profiles.generate(
        wid,
        {
            "kind": "clear_sky",
            "name": "half",
            "parameters": {"start": start, "count": 96, "resolution_minutes": 15, "clearness_factor": 0.5},
        },
    )
    values = profiles.read_profile(wid, generated["digest"])
    halves = profiles.read_profile(wid, half["digest"])
    assert all(values["channels"]["ghi"][i] == 0 for i in range(8))
    assert max(values["channels"]["ghi"]) > 0
    assert halves["channels"]["ghi"] == pytest.approx([v * 0.5 for v in values["channels"]["ghi"]])
    synthetic = profiles.generate(
        wid,
        {
            "kind": "synthetic_day",
            "name": "day",
            "parameters": {
                "start": start,
                "count": 96,
                "resolution_minutes": 15,
                "photoperiod": 12,
                "peak_par": 1500,
                "temperature_mean": 293.15,
                "temperature_amplitude": 5,
            },
        },
    )
    profile = profiles.read_profile(wid, synthetic["digest"])
    assert len(profile["channels"]["par"]) == 96
    assert original_socket


def test_csv_mapping_timezone_units_dst_and_linked_edits(tmp_path: Path) -> None:
    wid = _workspace()
    source = tmp_path / "tiny.csv"
    source.write_text("time,energy,temp\n2026-01-01 00:00,15,10\n2026-01-01 00:15,30,11\n", encoding="utf-8")
    created = profiles.confirm_csv(
        wid,
        source,
        "../tiny.csv",
        {
            "name": "mapped",
            "timestamp_column": "time",
            "timezone": "UTC",
            "resolution_minutes": 15,
            "stamp_convention": "start",
            "channels": {
                "ghi": {"column": "energy", "unit": "Wh m-2 interval-1"},
                "air_temperature": {"column": "temp", "unit": "°C"},
            },
        },
    )
    assert created["provenance"]["filename"] == "tiny.csv"
    values = profiles.read_profile(wid, created["digest"])
    assert values["channels"]["ghi"] == [60, 120]
    assert values["channels"]["air_temperature"] == pytest.approx([283.15, 284.15])
    edit = profiles.edit_profile(
        wid, created["digest"], {"operation": {"type": "cell", "channel": "ghi", "index": 0, "value": 75}}
    )
    assert edit["parent_digest"] == created["digest"]
    assert profiles.read_profile(wid, created["digest"])["channels"]["ghi"][0] == 60
    dst = tmp_path / "dst.csv"
    dst.write_text("time,value\n2026-11-01 01:30,4\n", encoding="utf-8")
    with pytest.raises(profiles.EnvironmentProfileError, match="ambiguous"):
        profiles.confirm_csv(
            wid,
            dst,
            "dst.csv",
            {
                "timestamp_column": "time",
                "timezone": "America/Denver",
                "resolution_minutes": 60,
                "stamp_convention": "end",
                "channels": {"ghi": {"column": "value", "unit": "W m-2"}},
            },
        )
    explicit_fold = profiles.confirm_csv(
        wid,
        dst,
        "dst.csv",
        {
            "timestamp_column": "time",
            "timezone": "America/Denver",
            "resolution_minutes": 60,
            "stamp_convention": "end",
            "fold": 1,
            "channels": {"ghi": {"column": "value", "unit": "W m-2"}},
        },
    )
    assert explicit_fold["timestamps"] == ["2026-11-01T08:30:00Z"]
    nonexistent = tmp_path / "gap.csv"
    nonexistent.write_text("time,value\n2026-03-08 02:30,4\n", encoding="utf-8")
    with pytest.raises(profiles.EnvironmentProfileError, match="nonexistent"):
        profiles.confirm_csv(
            wid,
            nonexistent,
            "gap.csv",
            {
                "timestamp_column": "time",
                "timezone": "America/Denver",
                "resolution_minutes": 60,
                "stamp_convention": "end",
                "channels": {"ghi": {"column": "value", "unit": "W m-2"}},
            },
        )


def test_epw_and_pvgis_fixtures_normalize_time_and_preserve_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("network socket creation attempted during import")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    from app.modules.environment.routes import confirm

    wid = _workspace()
    _site(wid)
    fixtures = Path(__file__).parent / "fixtures" / "environment"
    stage_dir = build_paths().environment_staging_dir(wid)
    stage_dir.mkdir(parents=True, exist_ok=True)
    epw_id = "a" * 32
    (stage_dir / f"{epw_id}.stage").write_bytes((fixtures / "tiny.epw").read_bytes())
    epw = confirm(wid, epw_id, "folder\\tiny.epw", {"format": "epw"})
    values = profiles.read_profile(wid, epw["digest"])
    assert len(values["timestamps"]) == 48
    assert values["timestamps"][0] == "2001-01-31T08:00:00Z"
    assert values["timestamps"][-1] == "2001-02-02T07:00:00Z"
    assert values["resolution_minutes"] == 60
    assert epw["label"] == "representative year (TMY)"
    assert epw["provenance"]["year_normalization"]["nominal_year"] == 2001
    assert epw["provenance"]["year_normalization"]["source_month_years"] == [
        {"month": 1, "year": 2005},
        {"month": 2, "year": 2000},
    ]
    assert epw["provenance"]["year_normalization"]["leap_day_rows_excluded"] == 1
    assert values["channels"]["air_temperature"][0] == pytest.approx(257.65)
    assert values["channels"]["wind_speed"][0] == pytest.approx(1.92)
    assert values["channels"]["ghi"][0] is None
    assert values["channels"]["dni"][0] is None
    pvgis_id = "b" * 32
    (stage_dir / f"{pvgis_id}.stage").write_bytes((fixtures / "tiny-pvgis.csv").read_bytes())
    tmy = confirm(wid, pvgis_id, "tiny-pvgis.csv", {"format": "pvgis_tmy", "timezone": "UTC"})
    assert tmy["label"] == "representative year (TMY)"
    assert tmy["provenance"]["irradiance_time_offset"] == 0.5
    assert len(tmy["provenance"]["selected_month_year_pairs"]) == 12
    assert tmy["provenance"]["selected_month_year_pairs"][1] == {"month": 2, "year": 2019}
    assert len(profiles.read_profile(wid, tmy["digest"], limit=5)["timestamps"]) == 5
    assert tmy["resolution_minutes"] == 60
    assert tmy["provenance"]["year_normalization"]["nominal_year"] == 2001
    assert tmy["provenance"]["year_normalization"]["missing_hourly_intervals_filled_with_null"] == 7224
    tmy_values = profiles.read_profile(wid, tmy["digest"], limit=5000)
    tmy_tail = profiles.read_profile(wid, tmy["digest"], offset=5000, limit=5000)
    assert tmy_values["total"] == 8088
    assert tmy_values["total"] == tmy_tail["total"]
    assert sum(value is None for value in tmy_values["channels"]["ghi"] + tmy_tail["channels"]["ghi"]) == 7224
    assert not any(stamp.startswith("2001-02-29") for stamp in tmy_values["timestamps"])
    assert tmy_values["timestamps"][0].startswith("2001-01-")
    assert tmy_tail["timestamps"][-1].startswith("2001-12-")
    child = profiles.derive_par(wid, tmy["digest"], factor=2.2)
    assert child["label"] == "representative year (TMY)"
    assert child["provenance"]["factor_provenance"] == "operator-entered conversion factor"
    assert child["provenance"]["parent_source_kind"] == "import"
    assert child["provenance"]["parent_parser_version"] == "pvlib.iotools.read_pvgis_tmy"
    edited = profiles.edit_profile(
        wid, child["digest"], {"operation": {"type": "cell", "channel": "ghi", "index": 0, "value": 4.0}}
    )
    assert edited["label"] == "representative year (TMY)"
    assert edited["parent_digest"] == child["digest"]
    assert edited["provenance"]["parent_source_kind"] == "derived_par"


def test_upload_stream_limit_extension_and_path_stripping() -> None:
    import asyncio

    from fastapi import HTTPException
    from starlette.requests import Request

    from app.modules.environment.routes import upload

    wid = _workspace()

    def request_for(chunks: list[bytes], headers: list[tuple[bytes, bytes]] | None = None) -> Request:
        pending = iter(chunks)

        async def receive() -> dict[str, object]:
            try:
                chunk = next(pending)
                return {"type": "http.request", "body": chunk, "more_body": True}
            except StopIteration:
                return {"type": "http.request", "body": b"", "more_body": False}

        return Request(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/",
                "raw_path": b"/",
                "query_string": b"",
                "headers": headers or [],
                "server": ("test", 80),
                "client": ("test", 123),
            },
            receive,
        )

    uploaded = asyncio.run(upload(wid, request_for([b"timestamp,value\n"]), "../../mapped.csv"))
    assert uploaded["filename"] == "mapped.csv"
    (build_paths().environment_staging_dir(wid) / f"{uploaded['upload_id']}.stage").unlink()
    with pytest.raises(HTTPException) as error:
        asyncio.run(upload(wid, request_for([b"x"], [(b"content-length", b"999999999")]), "x.csv"))
    assert error.value.status_code == 413
    with pytest.raises(HTTPException) as error:
        asyncio.run(upload(wid, request_for([b"x" * (20 * 1024 * 1024), b"y"]), "oversize.csv"))
    assert error.value.status_code == 413
    assert not list(build_paths().environment_staging_dir(wid).glob("*.stage"))
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            upload(
                wid,
                request_for([b"x" * (20 * 1024 * 1024), b"y"], [(b"content-length", b"1")]),
                "false-length.csv",
            )
        )
    assert error.value.status_code == 413
    assert not list(build_paths().environment_staging_dir(wid).glob("*.stage"))
    with pytest.raises(HTTPException) as error:
        asyncio.run(upload(wid, request_for([b"x"]), "local.txt"))
    assert error.value.status_code == 400


def test_strict_elapsed_grid_limits_derived_par_and_107_matching_samples() -> None:
    wid = _workspace()
    _site(wid)
    with pytest.raises(profiles.EnvironmentProfileError, match="resolution"):
        profiles.create_profile(
            wid, name="bad-step", timestamps=["2026-01-01T00:00:00Z"], channels={}, resolution_minutes=7, provenance={}
        )
    with pytest.raises(profiles.EnvironmentProfileError, match="exact elapsed"):
        profiles.create_profile(
            wid,
            name="irregular",
            timestamps=["2026-01-01T00:00:00Z", "2026-01-01T00:16:00Z"],
            channels={"ghi": [0, 1]},
            resolution_minutes=15,
            provenance={},
        )
    with pytest.raises(profiles.EnvironmentProfileError, match="250000"):
        profiles._validate({"timestamps": ["2026-01-01T00:00:00Z"] * 250001, "channels": {}})
    start = "2026-06-21T04:00:00Z"
    generated = profiles.generate(
        wid,
        {
            "kind": "clear_sky",
            "name": "sky",
            "parameters": {"start": start, "count": 96, "resolution_minutes": 15, "clearness_factor": 1},
        },
    )
    derived = profiles.derive_par(wid, generated["digest"], 2.06)
    assert derived["label"] == "screening conversion"
    assert derived["parent_digest"] == generated["digest"]
    source_values = profiles.read_profile(wid, generated["digest"])
    derived_values = profiles.read_profile(wid, derived["digest"])
    assert derived_values["channels"]["par"] == [v * 2.06 for v in source_values["channels"]["ghi"]]
    existing_par = profiles.create_profile(
        wid,
        name="existing PAR",
        timestamps=source_values["timestamps"],
        channels={**source_values["channels"], "par": [5.0] * len(source_values["timestamps"])},
        resolution_minutes=15,
        provenance={"kind": "fixture"},
    )
    with pytest.raises(profiles.EnvironmentProfileError, match="replace=true"):
        profiles.derive_par(wid, existing_par["digest"], factor=2.1)
    replaced = profiles.derive_par(wid, existing_par["digest"], factor=2.1, replace=True)
    assert replaced["provenance"]["factor_umol_per_j"] == 2.1
    params = {
        "start": start,
        "count": 96,
        "resolution_minutes": 15,
        "photoperiod": 12.0,
        "peak_par": 1500.0,
        "temperature_mean": 293.15,
        "temperature_amplitude": 5.0,
    }
    synthetic = profiles.generate(wid, {"kind": "synthetic_day", "name": "day", "parameters": params})
    values = profiles.read_profile(wid, synthetic["digest"])
    from app.modules.photobioreactor.pbr_evaluator import synthetic_day_inputs

    expected = []
    expected_temps = []
    for stamp in values["timestamps"]:
        instant = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        solar_hour = (instant.hour + instant.minute / 60 + instant.second / 3600 - 105.1786 / 15) % 24
        par, temperature = synthetic_day_inputs(
            solar_hour,
            params["photoperiod"],
            params["peak_par"],
            params["temperature_mean"],
            params["temperature_amplitude"],
        )
        expected.append(par)
        expected_temps.append(temperature)
    assert values["channels"]["par"] == expected
    assert values["channels"]["air_temperature"] == expected_temps


def test_canonical_jcs_vectors_and_integrity_no_overwrite() -> None:
    assert profiles._canonical({"v": 1e21}) == b'{"v":1e+21}'
    assert profiles._canonical({"v": 1e-7}) == b'{"v":1e-7}'
    assert profiles._canonical({"v": 5.0}) == b'{"v":5}'
    assert profiles._canonical({"b": 1, "a": 2}) == b'{"a":2,"b":1}'
    assert profiles._canonical({"v": 'hello\n"'}) == b'{"v":"hello\\n\\""}'
    with pytest.raises(profiles.EnvironmentProfileError, match="safe range"):
        profiles._canonical({"v": 2**53})

    wid = _workspace()
    profile = profiles.create_profile(
        wid,
        name="integrity",
        timestamps=["2026-01-01T00:15:00Z"],
        channels={"ghi": [2]},
        resolution_minutes=15,
        provenance={"kind": "test"},
    )
    path = build_paths().environment_profiles_dir(wid) / f"{profile['digest'][7:]}.json"
    path.write_bytes(b"tampered")
    with pytest.raises(profiles.EnvironmentProfileError, match="Existing immutable"):
        profiles.create_profile(
            wid,
            name="integrity",
            timestamps=["2026-01-01T00:15:00Z"],
            channels={"ghi": [2]},
            resolution_minutes=15,
            provenance={"kind": "test"},
        )
    assert any(item.get("integrity_error") for item in profiles.list_profiles(wid))


def test_csv_parser_caps_ragged_rows_and_channel_bounds(tmp_path: Path) -> None:
    wid = _workspace()
    ragged = tmp_path / "ragged.csv"
    ragged.write_text("time,value,other\n2026-01-01T00:00:00Z,1\n", encoding="utf-8")
    with pytest.raises(profiles.EnvironmentProfileError, match=r"line 2, column 'other'"):
        profiles.confirm_csv(
            wid,
            ragged,
            "ragged.csv",
            {
                "timestamp_column": "time",
                "resolution_minutes": 60,
                "stamp_convention": "end",
                "channels": {"ghi": {"column": "value", "unit": "W m-2"}},
            },
        )
    huge = tmp_path / "huge.csv"
    huge.write_text("time,value\n2026-01-01T00:00:00Z," + "1" * 140_000 + "\n", encoding="utf-8")
    with pytest.raises(profiles.EnvironmentProfileError, match=r"line 2, column 1 or later"):
        profiles.preview_csv(huge)
    invalid_utf8 = tmp_path / "invalid-utf8.csv"
    invalid_utf8.write_bytes(b"time,value\n2026-01-01T00:00:00Z,\xff\n")
    with pytest.raises(profiles.EnvironmentProfileError, match=r"CSV line 2, column 22"):
        profiles.preview_csv(invalid_utf8)
    too_many_columns = tmp_path / "columns.csv"
    too_many_columns.write_text(",".join(["time"] + [f"c{i}" for i in range(profiles.MAX_CSV_COLUMNS)]) + "\n")
    with pytest.raises(profiles.EnvironmentProfileError, match=r"CSV line 1, column 65: column cap"):
        profiles.preview_csv(too_many_columns)
    many = tmp_path / "many.csv"
    many.write_text("time,value\n" + "2026-01-01T00:00:00Z,1\n" * (profiles.MAX_POINTS + 1), encoding="utf-8")
    with pytest.raises(profiles.EnvironmentProfileError, match=r"line 250002, column 1: row cap"):
        profiles.preview_csv(many)
    with pytest.raises(profiles.EnvironmentProfileError, match="cloud_cover"):
        profiles.create_profile(
            wid,
            name="cloud",
            timestamps=["2026-01-01T00:15:00Z"],
            channels={"cloud_cover": [1.1]},
            resolution_minutes=15,
            provenance={},
        )
    with pytest.raises(profiles.EnvironmentProfileError, match="air_temperature"):
        profiles.create_profile(
            wid,
            name="temperature",
            timestamps=["2026-01-01T00:15:00Z"],
            channels={"air_temperature": [199]},
            resolution_minutes=15,
            provenance={},
        )


def test_clear_sky_outputs_subinterval_average_and_api_statuses() -> None:
    import pandas as pd
    import pvlib
    from fastapi.testclient import TestClient

    from app.main import app

    wid = _workspace()
    site = _site(wid)
    start = "2026-06-21T05:00:00Z"
    generated = profiles.generate(
        wid,
        {
            "kind": "clear_sky",
            "name": "hour average",
            "parameters": {"start": start, "count": 1, "resolution_minutes": 60},
        },
    )
    profile = profiles.read_profile(wid, generated["digest"])
    interval_end = datetime.fromisoformat(profile["timestamps"][0].replace("Z", "+00:00"))
    sample_times = pd.DatetimeIndex([interval_end - timedelta(minutes=5 * (index + 0.5)) for index in range(12)])
    pressure = pvlib.atmosphere.alt2pres(site["elevation_m"])
    solar = profiles._solar_position(
        sample_times,
        site["latitude"],
        site["longitude"],
        site["elevation_m"],
        pressure,
        12,
        67,
    )
    location = pvlib.location.Location(
        site["latitude"],
        site["longitude"],
        site["timezone"],
        site["elevation_m"],
    )
    expected = location.get_clearsky(sample_times, model="ineichen", solar_position=solar)["ghi"].mean()
    assert profile["channels"]["ghi"][0] == pytest.approx(expected)
    assert generated["provenance"]["irradiance_semantics"].startswith("interval average")
    assert generated["provenance"]["site_snapshot"]["revision"] == site["revision"]

    with TestClient(app) as client:
        stale = client.put(
            f"/workspaces/{wid}/environment/site?expected_revision=0",
            json={**site, "latitude": 39.7},
        )
        assert stale.status_code == 409
        missing_factor = client.post(
            f"/workspaces/{wid}/environment/profiles/{generated['digest']}/derive-par",
            json={},
        )
        assert missing_factor.status_code == 400
        absent = client.get(f"/workspaces/{wid}/environment/profiles/sha256:{'f' * 64}")
        assert absent.status_code == 404
        too_large = client.post(
            f"/workspaces/{wid}/environment/uploads?filename=oversize.csv",
            content=b"x" * (profiles.MAX_UPLOAD + 1),
            headers={"content-type": "application/octet-stream"},
        )
        assert too_large.status_code == 413
