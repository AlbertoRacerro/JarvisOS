"""Immutable workspace environment profiles and offline generators."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.database import open_sqlite_connection
from app.core.paths import build_paths
from app.modules.events.service import utc_now

MAX_UPLOAD = 20 * 1024 * 1024
MAX_POINTS = 250_000
MAX_SECONDS = 2 * 366 * 86400
MAX_CSV_COLUMNS = 64
PARSER_VERSION = "jarvis-environment-csv/1"
GENERATOR_VERSION = "jarvis-environment-generator/1"
CHANNELS = {
    "ghi": ("W m-2", "energy"),
    "dni": ("W m-2", "energy"),
    "dhi": ("W m-2", "energy"),
    "par": ("umol m-2 s-1", "nonnegative"),
    "air_temperature": ("K", "temperature"),
    "sea_temperature": ("K", "temperature"),
    "wind_speed": ("m s-1", "nonnegative"),
    "cloud_cover": ("1", "fraction"),
    "wave_height": ("m", "nonnegative"),
    "wave_period": ("s", "nonnegative"),
}
STEP_MINUTES = {5, 10, 15, 30, 60, 1440}


class EnvironmentProfileError(ValueError):
    pass


class EnvironmentConflict(EnvironmentProfileError):
    status_code = 409


class EnvironmentNotFound(EnvironmentProfileError):
    status_code = 404


class EnvironmentIntegrityError(EnvironmentProfileError):
    status_code = 500


def _canonical(value: Any) -> bytes:
    def number(value: int | float) -> str:
        if isinstance(value, int):
            if abs(value) > 2**53 - 1:
                raise EnvironmentProfileError("Canonical JSON integers must be within the I-JSON safe range.")
            return str(value)
        if not math.isfinite(value):
            raise EnvironmentProfileError("Canonical profile content cannot contain NaN or Infinity.")
        if value == 0:
            return "0"
        raw = repr(value).lower()
        decimal = Decimal(raw)
        magnitude = abs(value)
        if 1e-6 <= magnitude < 1e21:
            result = format(decimal, "f")
            if "." in result:
                result = result.rstrip("0").rstrip(".")
        else:
            mantissa, exponent = raw.split("e") if "e" in raw else (raw, "0")
            mantissa = mantissa.rstrip("0").rstrip(".") if "." in mantissa else mantissa
            exp = int(exponent)
            result = mantissa + "e" + ("+" if exp >= 0 else "") + str(exp)
        return result

    def clean(x: Any) -> Any:
        if isinstance(x, float):
            if not math.isfinite(x):
                raise EnvironmentProfileError("Canonical profile content cannot contain NaN or Infinity.")
            return 0 if x == 0 else x
        if isinstance(x, dict):
            return {str(k): clean(v) for k, v in x.items()}
        if isinstance(x, list):
            return [clean(v) for v in x]
        return x

    def encode(x: Any) -> str:
        x = clean(x)
        if x is None:
            return "null"
        if x is True:
            return "true"
        if x is False:
            return "false"
        if isinstance(x, (int, float)):
            return number(x)
        if isinstance(x, str):
            return json.dumps(x, ensure_ascii=False, separators=(",", ":"))
        if isinstance(x, list):
            return "[" + ",".join(encode(item) for item in x) + "]"
        if isinstance(x, dict):
            keys = sorted(x, key=lambda key: str(key).encode("utf-16be"))
            return "{" + ",".join(encode(str(key)) + ":" + encode(x[key]) for key in keys) + "}"
        raise EnvironmentProfileError(f"Unsupported canonical JSON value: {type(x).__name__}.")

    return encode(value).encode("utf-8")


def require_workspace(wid: str) -> None:
    with open_sqlite_connection() as db:
        if db.execute("SELECT 1 FROM workspaces WHERE id=?", (wid,)).fetchone() is None:
            raise EnvironmentNotFound("Workspace not found.")


def get_site(wid: str) -> dict[str, Any] | None:
    require_workspace(wid)
    p = build_paths().environment_site_file(wid)
    return json.loads(p.read_text("utf-8")) if p.exists() else None


def put_site(wid: str, payload: dict[str, Any], expected_revision: int) -> dict[str, Any]:
    require_workspace(wid)
    site = {k: payload.get(k) for k in ("name", "latitude", "longitude", "elevation_m", "timezone", "water_body")}
    if not isinstance(site["name"], str) or not site["name"].strip() or len(site["name"]) > 120:
        raise EnvironmentProfileError("Site name is required and must be at most 120 characters.")
    if site["water_body"] is not None and (not isinstance(site["water_body"], str) or len(site["water_body"]) > 120):
        raise EnvironmentProfileError("water_body must be a text label of at most 120 characters or null.")
    for key, lo, hi in (("latitude", -90, 90), ("longitude", -180, 180)):
        x = site[key]
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or not lo <= x <= hi:
            raise EnvironmentProfileError(f"{key} must be in [{lo}, {hi}].")
    x = site["elevation_m"]
    if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) or not -500 <= x <= 9000:
        raise EnvironmentProfileError("elevation_m must be finite and in [-500, 9000].")
    try:
        ZoneInfo(str(site["timezone"]))
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise EnvironmentProfileError("timezone must be a valid IANA timezone.") from None
    p = build_paths().environment_site_file(wid)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open_sqlite_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        old = json.loads(p.read_text("utf-8")) if p.exists() else None
        rev = old["revision"] if old else 0
        if rev != expected_revision:
            raise EnvironmentConflict(f"Site revision conflict: expected {expected_revision}, current {rev}.")
        result = {**site, "revision": rev + 1}
        tmp = p.with_name(f".{p.name}.{uuid4().hex}.tmp")
        with tmp.open("xb") as stream:
            stream.write(_canonical(result))
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(p)
        db.commit()
    return result


def _stamp(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise EnvironmentProfileError(f"Invalid timestamp {value!r}.") from e
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise EnvironmentProfileError(f"Timestamp {value!r} requires an explicit UTC offset or timezone.")
    return dt.astimezone(UTC)


def _integer(value: Any, label: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or int(value) != value
    ):
        raise EnvironmentProfileError(f"{label} must be an integer.")
    return int(value)


def _validate(profile: dict[str, Any]) -> None:
    stamps = profile.get("timestamps")
    if not isinstance(stamps, list) or not stamps or len(stamps) > MAX_POINTS:
        raise EnvironmentProfileError(f"Profile must contain 1 to {MAX_POINTS} timestamps.")
    times = [_stamp(s) for s in stamps]
    if any(a >= b for a, b in zip(times, times[1:], strict=False)):
        raise EnvironmentProfileError("Timestamps must be strictly increasing in UTC.")
    span_limit = 366 * 86400 if profile.get("label") == "representative year (TMY)" else MAX_SECONDS
    if (times[-1] - times[0]).total_seconds() > span_limit:
        raise EnvironmentProfileError(
            "TMY profiles must fit one nominal year."
            if span_limit < MAX_SECONDS
            else "Profile span cannot exceed two years."
        )
    step = profile.get("resolution_minutes")
    if step is not None:
        if step not in STEP_MINUTES:
            raise EnvironmentProfileError("resolution_minutes must be 5, 10, 15, 30, 60 or 1440.")
        if any((b - a).total_seconds() != step * 60 for a, b in zip(times, times[1:], strict=False)):
            raise EnvironmentProfileError("Timestamps must use the declared exact elapsed resolution.")
    for name, values in profile.get("channels", {}).items():
        if name not in CHANNELS or not isinstance(values, list) or len(values) != len(stamps):
            raise EnvironmentProfileError(f"Invalid channel {name!r} or column length.")
        kind = CHANNELS[name][1]
        for stamp, v in zip(stamps, values, strict=True):
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise EnvironmentProfileError(f"{name} at {stamp}: value must be finite or null.")
            bad = (
                (kind == "temperature" and not 200 <= v <= 350)
                or (kind == "fraction" and not 0 <= v <= 1)
                or (kind in {"nonnegative", "energy"} and v < 0)
            )
            if bad:
                raise EnvironmentProfileError(f"{name} at {stamp}: value violates channel bounds.")


def create_profile(
    wid: str,
    *,
    name: str,
    timestamps: list[str],
    channels: dict[str, list[float | None]],
    resolution_minutes: int | None,
    provenance: dict[str, Any],
    parent_digest: str | None = None,
    profile_label: str | None = None,
) -> dict[str, Any]:
    require_workspace(wid)
    if not isinstance(name, str) or len(name) > 200:
        raise EnvironmentProfileError("Profile name must be text of at most 200 characters.")
    if len(_canonical(provenance)) > 16_384:
        raise EnvironmentProfileError("Profile provenance exceeds 16 KiB.")
    unknown_channels = set(channels) - CHANNELS.keys()
    if unknown_channels:
        raise EnvironmentProfileError(f"Unknown environment channel(s): {', '.join(sorted(unknown_channels))}.")
    timestamps = [_stamp(stamp).isoformat().replace("+00:00", "Z") for stamp in timestamps]
    profile = {
        "name": name,
        "timestamps": timestamps,
        "channels": channels,
        "units": {k: CHANNELS[k][0] for k in channels},
        "resolution_minutes": resolution_minutes,
        "start": timestamps[0] if timestamps else None,
        "end": timestamps[-1] if timestamps else None,
        "parent_digest": parent_digest,
        "provenance": provenance,
        "label": profile_label,
    }
    _validate(profile)
    content = _canonical(profile)
    root = build_paths().environment_profiles_dir(wid)
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / (".profile-" + uuid4().hex + ".tmp")
    hasher = hashlib.sha256()
    try:
        with temporary.open("xb") as stream:
            for offset in range(0, len(content), 64 * 1024):
                chunk = content[offset : offset + 64 * 1024]
                stream.write(chunk)
                hasher.update(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        digest = "sha256:" + hasher.hexdigest()
        path = root / (digest[7:] + ".json")
        try:
            os.link(temporary, path)
        except FileExistsError:
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest[7:]:
                raise EnvironmentProfileError("Existing immutable profile blob failed digest verification.") from None
    finally:
        temporary.unlink(missing_ok=True)
    aid = str(uuid4())
    now = utc_now()
    with open_sqlite_connection() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            "SELECT id FROM artifacts WHERE workspace_id=? AND artifact_type='environment_profile' AND sha256=?",
            (wid, digest[7:]),
        ).fetchone()
        if existing is not None:
            aid = str(existing["id"])
        else:
            db.execute(
                "INSERT INTO artifacts (id,workspace_id,filename,stored_path,artifact_type,mime_type,sha256,source_ref,status,created_at,notes) VALUES (?,?,?,?, 'environment_profile','application/json',?,?,'registered',?,?)",
                (aid, wid, path.name, str(path), digest[7:], digest, now, name),
            )
        db.commit()
    return {"profile_id": digest, "digest": digest, "artifact_id": aid, **profile}


def _read(wid: str, digest: str) -> dict[str, Any]:
    require_workspace(wid)
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise EnvironmentProfileError("Invalid profile digest.")
    p = build_paths().environment_profiles_dir(wid) / (digest[7:] + ".json")
    try:
        content = p.read_bytes()
    except OSError as e:
        raise EnvironmentNotFound("Profile not found.") from e
    if hashlib.sha256(content).hexdigest() != digest[7:]:
        raise EnvironmentIntegrityError("Profile digest verification failed.")
    return json.loads(content)


def list_profiles(wid: str) -> list[dict[str, Any]]:
    require_workspace(wid)
    root = build_paths().environment_profiles_dir(wid)
    out = []
    if root.exists():
        for p in sorted(root.glob("*.json"), reverse=True):
            d = "sha256:" + p.stem
            try:
                item = _read(wid, d)
            except EnvironmentProfileError as error:
                out.append(
                    {
                        "profile_id": d,
                        "digest": d,
                        "name": p.stem,
                        "channels": {},
                        "start": "",
                        "end": "",
                        "resolution_minutes": None,
                        "provenance": {"kind": "integrity_failure"},
                        "parent_digest": None,
                        "integrity_error": str(error),
                    }
                )
                continue
            out.append(
                {
                    "profile_id": d,
                    "digest": d,
                    "name": item["name"],
                    "channels": item["units"],
                    "start": item["start"],
                    "end": item["end"],
                    "resolution_minutes": item["resolution_minutes"],
                    "provenance": item["provenance"],
                    "parent_digest": item["parent_digest"],
                    "label": item.get("label"),
                }
            )
    return out


def read_profile(
    wid: str,
    digest: str,
    start: str | None = None,
    end: str | None = None,
    offset: int = 0,
    limit: int = 1000,
    resolution_minutes: int | None = None,
) -> dict[str, Any]:
    item = _read(wid, digest)
    indexes = [
        i
        for i, s in enumerate(item["timestamps"])
        if (start is None or _stamp(s) >= _stamp(start)) and (end is None or _stamp(s) <= _stamp(end))
    ]
    timestamps = [item["timestamps"][i] for i in indexes]
    columns = {k: [v[i] for i in indexes] for k, v in item["channels"].items()}
    base_resolution = item["resolution_minutes"]
    result_resolution = resolution_minutes or base_resolution
    if resolution_minutes is not None:
        if resolution_minutes not in STEP_MINUTES or base_resolution is None or resolution_minutes < base_resolution:
            raise EnvironmentProfileError(
                "Requested read resolution must be an allowed step at least as coarse as the profile."
            )
        if resolution_minutes % base_resolution:
            raise EnvironmentProfileError("Requested read resolution must be a multiple of the profile resolution.")
        factor = resolution_minutes // base_resolution
        if factor > 1:
            grouped_stamps: list[str] = []
            grouped_columns: dict[str, list[float | None]] = {k: [] for k in columns}
            grouped_indexes: list[int] = []
            groups: dict[int, list[int]] = {}
            bin_seconds = resolution_minutes * 60
            for position, stamp in enumerate(timestamps):
                epoch = int(_stamp(stamp).timestamp())
                bucket = (epoch - 1) // bin_seconds
                groups.setdefault(bucket, []).append(position)
            for bucket, positions in sorted(groups.items()):
                if len(positions) != factor:
                    continue
                grouped_stamps.append(
                    datetime.fromtimestamp((bucket + 1) * bin_seconds, UTC)
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z")
                )
                grouped_indexes.append(indexes[positions[-1]])
                for name, values in columns.items():
                    group = [values[position] for position in positions]
                    if any(value is None for value in group):
                        grouped_columns[name].append(None)
                    elif CHANNELS[name][1] == "energy":
                        grouped_columns[name].append(sum(group) / len(group))
                    else:
                        grouped_columns[name].append(group[-1])
            timestamps, columns = grouped_stamps, grouped_columns
            indexes = grouped_indexes
    total = len(timestamps)
    page = slice(max(0, offset), max(0, offset) + min(max(1, limit), 5000))
    return {
        **item,
        "profile_id": digest,
        "digest": digest,
        "timestamps": timestamps[page],
        "channels": {k: v[page] for k, v in columns.items()},
        "resolution_minutes": result_resolution,
        "offset": max(0, offset),
        "total": total,
        "indices": indexes[page],
    }


def _grid(start: str, count: int, step: int) -> list[datetime]:
    if step not in STEP_MINUTES:
        raise EnvironmentProfileError("resolution_minutes must be 5, 10, 15, 30, 60 or 1440.")
    if not 1 <= count <= MAX_POINTS:
        raise EnvironmentProfileError("Invalid point count.")
    if count * step * 60 > MAX_SECONDS:
        raise EnvironmentProfileError("Generated profile span cannot exceed two years.")
    first = _stamp(start)
    return [first + timedelta(minutes=step * (i + 1)) for i in range(count)]


def _solar_position(times, latitude, longitude, elevation_m, pressure_pa, temperature_c, delta_t):
    """The shared SPA implementation used by the reference vector and clear-sky generator."""
    import pvlib

    return pvlib.solarposition.spa_python(
        times,
        latitude,
        longitude,
        altitude=elevation_m,
        pressure=pressure_pa,
        temperature=temperature_c,
        delta_t=delta_t,
    )


def generate(wid: str, payload: dict[str, Any]) -> dict[str, Any]:
    site = get_site(wid)
    if site is None:
        raise EnvironmentProfileError("Set the workspace site before generating profiles.")
    kind = payload.get("kind")
    p = payload.get("parameters", {})
    if not isinstance(p, dict):
        raise EnvironmentProfileError("Generator parameters must be an object.")
    accepted_parameters = {
        "clear_sky": {
            "start",
            "count",
            "resolution_minutes",
            "clearness_factor",
            "pressure_pa",
            "temperature_c",
            "delta_t",
        },
        "synthetic_day": {
            "start",
            "count",
            "resolution_minutes",
            "photoperiod",
            "peak_par",
            "temperature_mean",
            "temperature_amplitude",
        },
    }
    if kind in accepted_parameters and set(p) - accepted_parameters[kind]:
        raise EnvironmentProfileError("Generator parameters contain unsupported fields.")
    step = _integer(p.get("resolution_minutes", 60), "resolution_minutes")
    count = _integer(p.get("count", 24), "count")
    times = _grid(str(p.get("start")), count, step)
    snap = {k: site[k] for k in ("name", "latitude", "longitude", "elevation_m", "timezone", "water_body")}
    channels: dict[str, list[float | None]]
    if kind == "synthetic_day":
        photo = float(p["photoperiod"])
        peak = float(p["peak_par"])
        mean = float(p["temperature_mean"])
        amp = float(p["temperature_amplitude"])
        if not 0 < photo <= 24 or not math.isfinite(peak) or peak < 0 or not all(map(math.isfinite, (mean, amp))):
            raise EnvironmentProfileError(
                "synthetic_day requires photoperiod in (0, 24], nonnegative peak PAR, and finite temperatures."
            )
        from app.modules.photobioreactor.pbr_evaluator import synthetic_day_inputs

        par: list[float | None] = []
        temp: list[float | None] = []
        for t in times:
            solar_hour = (t.hour + t.minute / 60 + t.second / 3600 + site["longitude"] / 15) % 24
            par_value, temp_value = synthetic_day_inputs(solar_hour, photo, peak, mean, amp)
            par.append(par_value)
            temp.append(temp_value)
        channels = {"par": par, "air_temperature": temp}
    elif kind == "clear_sky":
        import pandas as pd
        import pvlib

        substep = 5
        samples_per_interval = step // substep
        sample_times = [t - timedelta(minutes=substep * (i + 0.5)) for t in times for i in range(samples_per_interval)]
        idx = pd.DatetimeIndex(sample_times)
        loc = pvlib.location.Location(site["latitude"], site["longitude"], site["timezone"], site["elevation_m"])
        pressure = float(p.get("pressure_pa", pvlib.atmosphere.alt2pres(site["elevation_m"])))
        temperature = float(p.get("temperature_c", 12.0))
        delta_t = float(p.get("delta_t", 67.0))
        solar_position = _solar_position(
            idx,
            site["latitude"],
            site["longitude"],
            site["elevation_m"],
            pressure,
            temperature,
            delta_t,
        )
        weather = loc.get_clearsky(idx, model="ineichen", solar_position=solar_position)
        factor = float(p.get("clearness_factor", 1))
        if not math.isfinite(factor) or not 0 <= factor <= 1:
            raise EnvironmentProfileError("clearness_factor must be in [0, 1].")
        channels = {}
        for key, col in (("ghi", "ghi"), ("dni", "dni"), ("dhi", "dhi")):
            samples = weather[col].tolist()
            means: list[float | None] = []
            for i in range(0, len(samples), samples_per_interval):
                block = [float(v) for v in samples[i : i + samples_per_interval]]
                if not all(math.isfinite(v) for v in block):
                    raise EnvironmentProfileError(f"Clear-sky {key} produced a non-finite value.")
                means.append(max(0.0, sum(block) / len(block) * factor))
            channels[key] = means
    else:
        raise EnvironmentProfileError("Unknown generator kind.")
    stamps = [x.isoformat(timespec="seconds").replace("+00:00", "Z") for x in times]
    return create_profile(
        wid,
        name=str(payload.get("name") or kind),
        timestamps=stamps,
        channels=channels,
        resolution_minutes=step,
        provenance={
            "kind": kind,
            "parameters": p,
            "generator_version": GENERATOR_VERSION,
            "pvlib_version": pvlib.__version__ if kind == "clear_sky" else None,
            "irradiance_semantics": "interval average from 5-minute midpoint samples" if kind == "clear_sky" else None,
            "site_snapshot": {**snap, "revision": site["revision"]},
        },
    )


def derive_par(wid: str, digest: str, factor: float, name: str | None = None, replace: bool = False) -> dict[str, Any]:
    s = _read(wid, digest)
    if "ghi" not in s["channels"] or not math.isfinite(factor) or factor <= 0:
        raise EnvironmentProfileError("A positive conversion factor and GHI channel are required.")
    if "par" in s["channels"] and not replace:
        raise EnvironmentProfileError("Profile already contains PAR; set replace=true to replace it.")
    c = {**s["channels"], "par": [None if v is None else v * factor for v in s["channels"]["ghi"]]}
    return create_profile(
        wid,
        name=name or f"{s['name']} · derived PAR",
        timestamps=s["timestamps"],
        channels=c,
        resolution_minutes=s["resolution_minutes"],
        provenance={
            "kind": "derived_par",
            "source_digest": digest,
            "factor_umol_per_j": factor,
            "label": "screening conversion",
            "factor_provenance": "operator-entered conversion factor",
            "replace_existing_par": replace,
            "parent_source_kind": s["provenance"].get("kind"),
            "parent_parser_version": s["provenance"].get("parser"),
            "parent_generator_version": s["provenance"].get("generator_version"),
        },
        parent_digest=digest,
        profile_label=s.get("label") or "screening conversion",
    )


def edit_profile(wid: str, digest: str, payload: dict[str, Any]) -> dict[str, Any]:
    s = _read(wid, digest)
    c = {k: list(v) for k, v in s["channels"].items()}
    op = payload.get("operation", {})
    if not isinstance(op, dict):
        raise EnvironmentProfileError("Edit operation must be an object.")
    if set(op) - {"type", "channel", "index", "value", "start", "end", "changes"}:
        raise EnvironmentProfileError("Edit operation contains unsupported fields.")
    name = str(op.get("channel", ""))
    if op.get("type") == "cells":
        changes = op.get("changes")
        if not isinstance(changes, list) or not changes or len(changes) > 5000:
            raise EnvironmentProfileError("Cell edit batch must contain 1 to 5000 changes.")
        for change in changes:
            if not isinstance(change, dict) or set(change) != {"channel", "index", "value"}:
                raise EnvironmentProfileError("Each cell change requires channel, index and value.")
            channel = change["channel"]
            if channel not in c:
                raise EnvironmentProfileError(f"Edit channel {channel!r} not found.")
            index = _integer(change["index"], "edit index")
            if not 0 <= index < len(c[channel]):
                raise EnvironmentProfileError("Edit index is outside the profile.")
            c[channel][index] = change["value"]
        summary = {
            "type": "cell edits",
            "count": len(changes),
            "channels": sorted({change["channel"] for change in changes}),
        }
    elif name not in c:
        raise EnvironmentProfileError("Edit channel not found.")
    elif op.get("type") == "cell":
        index = _integer(op["index"], "edit index")
        if not 0 <= index < len(c[name]):
            raise EnvironmentProfileError("Edit index is outside the profile.")
        c[name][index] = op.get("value")
        summary = {"type": "cell edit", "channel": name, "index": index}
    elif op.get("type") in {"scale", "offset"}:
        raw_value = op.get("value")
        if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)) or not math.isfinite(raw_value):
            raise EnvironmentProfileError("Edit value must be finite and numeric.")
        value = float(raw_value)
        for i, stamp in enumerate(s["timestamps"]):
            if op.get("start") and _stamp(stamp) < _stamp(op["start"]):
                continue
            if op.get("end") and _stamp(stamp) > _stamp(op["end"]):
                continue
            if c[name][i] is not None:
                c[name][i] = c[name][i] * value if op["type"] == "scale" else c[name][i] + value
        summary = {
            "type": op["type"],
            "channel": name,
            "start": op.get("start"),
            "end": op.get("end"),
            "value": value,
        }
    else:
        raise EnvironmentProfileError("Unknown edit operation.")
    return create_profile(
        wid,
        name=str(payload.get("name") or f"{s['name']} · edited"),
        timestamps=s["timestamps"],
        channels=c,
        resolution_minutes=s["resolution_minutes"],
        provenance={
            "kind": "edit",
            "summary": summary,
            "parent_source_kind": s["provenance"].get("kind"),
            "parent_parser_version": s["provenance"].get("parser"),
            "parent_generator_version": s["provenance"].get("generator_version"),
        },
        parent_digest=digest,
        profile_label=s.get("label"),
    )


def preview_csv(path: Path) -> dict[str, Any]:
    _enforce_csv_shape(path)
    try:
        with path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.reader(f)
            columns = next(reader, [])
            if not columns:
                raise EnvironmentProfileError("CSV line 1: expected a header and at least one row.")
            if len(columns) > MAX_CSV_COLUMNS:
                raise EnvironmentProfileError(f"CSV line 1: column cap is {MAX_CSV_COLUMNS}.")
            if len(columns) != len(set(columns)):
                raise EnvironmentProfileError("CSV line 1, column 1: duplicate header names are not allowed.")
            rows: list[list[str]] = []
            row_count = 0
            for line, row in enumerate(reader, 2):
                if line > MAX_POINTS + 1:
                    raise EnvironmentProfileError(f"CSV line {line}: row cap is {MAX_POINTS}.")
                if len(row) > MAX_CSV_COLUMNS:
                    raise EnvironmentProfileError(
                        f"CSV line {line}, column {MAX_CSV_COLUMNS + 1}: column cap exceeded."
                    )
                row_count += 1
                if len(rows) < 5:
                    rows.append(row)
    except csv.Error as e:
        raise EnvironmentProfileError(f"CSV line {reader.line_num}, column 1 or later: {e}") from e
    except UnicodeDecodeError as e:
        raise EnvironmentProfileError(_csv_decode_location(path, e)) from e
    if row_count == 0:
        raise EnvironmentProfileError("CSV line 1: expected a header and at least one row.")
    return {"columns": columns, "rows": rows, "row_count": row_count, "parser": PARSER_VERSION}


def confirm_csv(wid: str, path: Path, filename: str, m: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(m, dict) or len(_canonical(m)) > 16_384:
        raise EnvironmentProfileError("CSV mapping must be a small object.")
    allowed_mapping_fields = {
        "name",
        "timestamp_column",
        "timestamp_format",
        "timezone",
        "resolution_minutes",
        "stamp_convention",
        "fold",
        "channels",
    }
    if set(m) - allowed_mapping_fields:
        raise EnvironmentProfileError("CSV mapping contains unsupported fields.")
    _enforce_csv_shape(path)
    try:
        with path.open(encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            cols = reader.fieldnames or []
            if len(cols) > MAX_CSV_COLUMNS:
                raise EnvironmentProfileError(f"CSV line 1: column cap is {MAX_CSV_COLUMNS}.")
            if len(cols) != len(set(cols)):
                raise EnvironmentProfileError("CSV line 1, column 1: duplicate header names are not allowed.")
            rows: list[dict[str, str | None]] = []
            for line, row in enumerate(reader, 2):
                if line > MAX_POINTS + 1:
                    raise EnvironmentProfileError(f"CSV line {line}: row cap is {MAX_POINTS}.")
                if None in row:
                    raise EnvironmentProfileError(f"CSV line {line}, column {MAX_CSV_COLUMNS + 1}: too many columns.")
                if any(value is None for value in row.values()):
                    missing_column = next(key for key, value in row.items() if value is None)
                    raise EnvironmentProfileError(f"CSV line {line}, column {missing_column!r}: missing value.")
                rows.append(row)
    except csv.Error as e:
        raise EnvironmentProfileError(f"CSV line {reader.line_num}, column 1 or later: {e}") from e
    except UnicodeDecodeError as e:
        raise EnvironmentProfileError(_csv_decode_location(path, e)) from e
    tc = m.get("timestamp_column")
    if tc not in cols:
        raise EnvironmentProfileError("CSV column mapping: timestamp column not found.")
    tc = str(tc)
    stamps = []
    try:
        zone = ZoneInfo(str(m["timezone"])) if m.get("timezone") else None
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise EnvironmentProfileError("CSV mapping timezone must be a valid IANA timezone.") from None
    for line, row in enumerate(rows, 2):
        raw = row.get(tc)
        if raw is None:
            raise EnvironmentProfileError(f"CSV line {line}, column {tc!r}: missing value.")
        try:
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                dt = datetime.strptime(raw, str(m.get("timestamp_format") or "%Y-%m-%d %H:%M:%S"))
            if dt.tzinfo is None:
                if zone is None:
                    raise EnvironmentProfileError(
                        f"CSV line {line}, column {tc!r}: naive timestamp needs an IANA timezone."
                    )
                a, b = dt.replace(tzinfo=zone, fold=0), dt.replace(tzinfo=zone, fold=1)
                if a.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != dt:
                    raise EnvironmentProfileError(f"CSV line {line}, column {tc!r}: nonexistent DST timestamp.")
                if a.utcoffset() != b.utcoffset():
                    fold = m.get("fold")
                    if fold not in (0, 1):
                        raise EnvironmentProfileError(
                            f"CSV line {line}, column {tc!r}: ambiguous DST timestamp needs an explicit fold mapping."
                        )
                    a = dt.replace(tzinfo=zone, fold=fold)
                dt = a
            stamps.append(dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"))
        except EnvironmentProfileError:
            raise
        except (ValueError, TypeError) as e:
            raise EnvironmentProfileError(f"CSV line {line}, column {tc!r}: invalid timestamp.") from e
    step = _integer(m.get("resolution_minutes", 60), "resolution_minutes")
    if m.get("stamp_convention") not in {"start", "end"}:
        raise EnvironmentProfileError("CSV mapping stamp_convention must be start or end.")
    if m.get("stamp_convention") == "start":
        stamps = [
            (_stamp(s) + timedelta(minutes=step)).isoformat(timespec="seconds").replace("+00:00", "Z") for s in stamps
        ]
    channels = {}
    mapping_channels = m.get("channels", {})
    if not isinstance(mapping_channels, dict) or len(mapping_channels) > len(CHANNELS):
        raise EnvironmentProfileError("CSV mapping channels must be a bounded object.")
    for channel, spec in mapping_channels.items():
        if not isinstance(spec, dict) or set(spec) - {"column", "unit"}:
            raise EnvironmentProfileError(f"CSV mapping for {channel} contains unsupported fields.")
        col = spec.get("column")
        if channel not in CHANNELS or col not in cols:
            raise EnvironmentProfileError(f"CSV mapping: invalid column for {channel}.")
        col = str(col)
        unit = spec.get("unit")
        allowed_units = {CHANNELS[channel][0]}
        if channel in {"ghi", "dni", "dhi"}:
            allowed_units.add("Wh m-2 interval-1")
        if channel in {"air_temperature", "sea_temperature"}:
            allowed_units.add("°C")
        if unit not in allowed_units:
            raise EnvironmentProfileError(f"CSV mapping: unsupported unit {unit!r} for {channel}.")
        values = []
        for line, row in enumerate(rows, 2):
            raw_value = row.get(col)
            if raw_value is None:
                raise EnvironmentProfileError(f"CSV line {line}, column {col!r}: missing value.")
            raw = raw_value.strip()
            try:
                v = None if raw in {"", "null", "NA"} else float(raw)
            except ValueError as e:
                raise EnvironmentProfileError(f"CSV line {line}, column {col!r}: invalid number.") from e
            if channel in {"ghi", "dni", "dhi"} and unit == "Wh m-2 interval-1":
                v = None if v is None else v * 60 / step
            elif channel in {"air_temperature", "sea_temperature"} and unit == "°C":
                v = None if v is None else v + 273.15
            values.append(v)
        channels[channel] = values
    return create_profile(
        wid,
        name=str(m.get("name") or Path(filename).name),
        timestamps=stamps,
        channels=channels,
        resolution_minutes=step,
        provenance={
            "kind": "import",
            "filename": Path(filename).name,
            "original_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "format": "csv",
            "column_mapping": m,
            "parser": PARSER_VERSION,
        },
    )


def _csv_decode_location(path: Path, error: UnicodeDecodeError) -> str:
    with path.open("rb") as stream:
        prefix = stream.read(error.start)
    line = prefix.count(b"\n") + 1
    last_newline = prefix.rfind(b"\n")
    column = error.start - last_newline
    return f"CSV line {line}, column {column}: invalid UTF-8 input."


def _enforce_csv_shape(path: Path) -> None:
    """Reject oversized rows and records before csv.reader allocates their field lists."""
    physical_line = 0
    record = 1
    data_rows = 0
    columns = 1
    in_quotes = False
    at_field_start = True
    last_line = ""
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            for line in stream:
                last_line = line
                physical_line += 1
                index = 0
                while index < len(line):
                    character = line[index]
                    if in_quotes:
                        if character == '"':
                            if index + 1 < len(line) and line[index + 1] == '"':
                                index += 2
                                continue
                            in_quotes = False
                    elif character == '"' and at_field_start:
                        in_quotes = True
                        at_field_start = False
                    elif character == ",":
                        columns += 1
                        at_field_start = True
                        if columns > MAX_CSV_COLUMNS:
                            raise EnvironmentProfileError(
                                f"CSV line {physical_line}, column {columns}: column cap is {MAX_CSV_COLUMNS}."
                            )
                    elif character == "\n" and not in_quotes:
                        if record > 1:
                            data_rows += 1
                            if data_rows > MAX_POINTS:
                                raise EnvironmentProfileError(
                                    f"CSV line {physical_line}, column 1: row cap is {MAX_POINTS}."
                                )
                        record += 1
                        columns = 1
                        at_field_start = True
                    elif character not in "\r\n":
                        at_field_start = False
                    index += 1
        if last_line and not last_line.endswith("\n") and record > 1:
            data_rows += 1
            if data_rows > MAX_POINTS:
                raise EnvironmentProfileError(f"CSV line {physical_line}, column 1: row cap is {MAX_POINTS}.")
    except UnicodeDecodeError as error:
        raise EnvironmentProfileError(_csv_decode_location(path, error)) from error
