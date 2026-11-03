"""Environment site, import, generator, and immutable profile API."""

from __future__ import annotations

import csv
import hashlib
import inspect
import json
import math
import re
import tempfile
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import anyio
from fastapi import APIRouter, HTTPException, Query, Request

from app.core.paths import build_paths
from app.modules.environment import profiles as svc

router = APIRouter(prefix="/workspaces/{workspace_id}/environment", tags=["environment"])
STAGING_TTL_SECONDS = 24 * 60 * 60


def _read_pvgis_tmy(pvlib, path: Path):
    """Use pvlib's coerce_year option when available, with 0.16's equivalent fallback."""
    reader = pvlib.iotools.read_pvgis_tmy
    if "coerce_year" in inspect.signature(reader).parameters:
        return reader(path, pvgis_format="csv", coerce_year=2001)
    return reader(path, pvgis_format="csv")


def _epw_month_years(path: Path) -> list[dict[str, int]]:
    pairs: set[tuple[int, int]] = set()
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream)
        for _ in range(8):
            next(reader, None)
        for row in reader:
            if len(row) >= 2:
                try:
                    pairs.add((int(row[1]), int(row[0])))
                except ValueError:
                    continue
    return [{"month": month, "year": year} for month, year in sorted(pairs)]


def _epw_records_per_hour(path: Path) -> int:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream)
        rows = [next(reader, []) for _ in range(8)]
    try:
        return int(rows[7][2])
    except (IndexError, ValueError) as error:
        raise ValueError("EPW line 8: invalid DATA PERIODS records-per-hour value.") from error


def _read_epw(pvlib, path: Path):
    """Read a TMY on the explicit non-leap nominal year, dropping source leap-day rows."""
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.reader(stream))
    leap_rows = [
        index for index, row in enumerate(rows[8:], 8)
        if len(row) > 2 and row[0].isdigit() and row[1:3] == ["2", "29"]
    ]
    if not leap_rows:
        return pvlib.iotools.read_epw(path, coerce_year=2001), 0
    leap_set = set(leap_rows)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", suffix=".epw", delete=False) as stream:
        temporary = Path(stream.name)
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerows(row for index, row in enumerate(rows) if index not in leap_set)
    try:
        return pvlib.iotools.read_epw(temporary, coerce_year=2001), len(leap_rows)
    finally:
        temporary.unlink(missing_ok=True)


def _hourly_resolution(stamps: list[str]) -> int | None:
    parsed = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in stamps]
    return 60 if all((b - a).total_seconds() == 3600 for a, b in zip(parsed, parsed[1:], strict=False)) else None


def _fill_hourly_gaps(
    stamps: list[str], channels: dict[str, list[float | None]]
) -> tuple[list[str], dict[str, list[float | None]], int]:
    """Make omitted hourly TMY intervals explicit nulls on the representative-year grid."""
    times = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in stamps]
    if len(times) < 2:
        return stamps, channels, 0
    seconds = [(right - left).total_seconds() for left, right in zip(times, times[1:], strict=False)]
    if any(delta <= 0 or delta % 3600 for delta in seconds):
        return stamps, channels, 0
    total = int((times[-1] - times[0]).total_seconds() // 3600) + 1
    if total > svc.MAX_POINTS:
        raise ValueError(f"PVGIS TMY exceeds the {svc.MAX_POINTS}-point profile limit.")
    source = {int((time - times[0]).total_seconds() // 3600): index for index, time in enumerate(times)}
    expanded_times = [times[0] + timedelta(hours=index) for index in range(total)]
    expanded_stamps = [
        value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z") for value in expanded_times
    ]
    expanded_channels = {
        channel: [values[source[index]] if index in source else None for index in range(total)]
        for channel, values in channels.items()
    }
    return expanded_stamps, expanded_channels, total - len(times)


def _cleanup_staging(root: Path) -> None:
    if not root.exists():
        return
    expiry = time.time() - STAGING_TTL_SECONDS
    for staged in root.glob("*.stage"):
        try:
            if staged.stat().st_mtime < expiry:
                staged.unlink()
                staged.with_suffix(".upload.json").unlink(missing_ok=True)
        except FileNotFoundError:
            continue


def _error(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=getattr(exc, "status_code", 400),
        detail={"error": str(exc)},
    )


@router.get("/site")
def get_site(workspace_id: str):
    try:
        return svc.get_site(workspace_id)
    except ValueError as e:
        raise _error(e) from e


@router.put("/site")
def put_site(workspace_id: str, payload: dict, expected_revision: int = Query(..., ge=0)):
    try:
        return svc.put_site(workspace_id, payload, expected_revision)
    except ValueError as e:
        raise _error(e) from e


@router.get("/profiles")
def list_profiles(workspace_id: str):
    try:
        return svc.list_profiles(workspace_id)
    except ValueError as e:
        raise _error(e) from e


@router.get("/profiles/{digest}")
def read_profile(
    workspace_id: str,
    digest: str,
    start: str | None = None,
    end: str | None = None,
    offset: int = 0,
    limit: int = 1000,
    resolution_minutes: int | None = Query(None),
):
    try:
        return svc.read_profile(workspace_id, digest, start, end, offset, limit, resolution_minutes)
    except ValueError as e:
        raise _error(e) from e


@router.post("/generate")
def generate(workspace_id: str, payload: dict):
    try:
        return svc.generate(workspace_id, payload)
    except (ValueError, KeyError, TypeError) as e:
        raise _error(e) from e


@router.post("/profiles/{digest}/derive-par")
def derive_par(workspace_id: str, digest: str, payload: dict):
    try:
        if "factor" not in payload:
            raise ValueError("An explicit PAR conversion factor is required.")
        return svc.derive_par(
            workspace_id, digest, float(payload["factor"]), payload.get("name"), bool(payload.get("replace", False))
        )
    except (ValueError, KeyError, TypeError) as e:
        raise _error(e) from e


@router.post("/profiles/{digest}/edit")
def edit_profile(workspace_id: str, digest: str, payload: dict):
    try:
        return svc.edit_profile(workspace_id, digest, payload)
    except (ValueError, KeyError, TypeError, IndexError) as e:
        raise _error(e) from e


@router.post("/uploads")
async def upload(workspace_id: str, request: Request, filename: str):
    try:
        svc.require_workspace(workspace_id)
    except ValueError as e:
        raise _error(e) from e
    safe = Path(filename.replace("\\", "/")).name
    if len(safe) > 255:
        raise HTTPException(400, detail={"error": "Filename must be at most 255 characters."})
    if Path(safe).suffix.lower() not in {".csv", ".epw"}:
        raise HTTPException(400, detail={"error": "Only .csv and .epw files are accepted."})
    length = request.headers.get("content-length")
    if length and (not length.isdigit() or int(length) > svc.MAX_UPLOAD):
        raise HTTPException(413, detail={"error": "Upload exceeds 20 MiB."})
    root = build_paths().environment_staging_dir(workspace_id)
    root.mkdir(parents=True, exist_ok=True)
    _cleanup_staging(root)
    if len(list(root.glob("*.stage"))) >= 10:
        raise HTTPException(429, detail={"error": "Workspace upload staging limit reached."})
    token = uuid4().hex
    path = root / (token + ".stage")
    size = 0
    hasher = hashlib.sha256()
    try:
        with path.open("xb") as stream:
            async for chunk in request.stream():
                size += len(chunk)
                if size > svc.MAX_UPLOAD:
                    raise HTTPException(413, detail={"error": "Upload exceeds 20 MiB."})
                await anyio.to_thread.run_sync(stream.write, chunk)
                hasher.update(chunk)
        path.with_suffix(".upload.json").write_text(json.dumps({"filename": safe}), encoding="utf-8")
        return {
            "upload_id": token,
            "filename": safe,
            "size": size,
            "sha256": hasher.hexdigest(),
        }
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _stage(workspace_id: str, upload_id: str) -> Path:
    try:
        svc.require_workspace(workspace_id)
    except ValueError as e:
        raise _error(e) from e
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        raise HTTPException(404, detail={"error": "Staged upload not found."})
    path = build_paths().environment_staging_dir(workspace_id) / (upload_id + ".stage")
    _cleanup_staging(path.parent)
    if not path.is_file():
        raise HTTPException(404, detail={"error": "Staged upload not found."})
    return path


def _bound_filename(path: Path, supplied: str) -> str:
    metadata = path.with_suffix(".upload.json")
    if not metadata.exists():
        return Path(supplied.replace("\\", "/")).name
    try:
        original = json.loads(metadata.read_text("utf-8"))["filename"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(400, detail={"error": "Staged upload metadata is invalid."}) from error
    if Path(supplied.replace("\\", "/")).name != original:
        raise HTTPException(400, detail={"error": "Filename does not match the staged upload."})
    return original


@router.post("/uploads/{upload_id}/preview")
def preview(workspace_id: str, upload_id: str, filename: str):
    import pvlib

    path = _stage(workspace_id, upload_id)
    safe = _bound_filename(path, filename)
    try:
        if safe.lower().endswith(".csv"):
            # PVGIS files are distinguished by their header, and parsed locally via pvlib.
            with path.open(encoding="utf-8-sig", errors="replace") as stream:
                header = stream.read(2048).upper()
            if "IRRADIANCE TIME OFFSET" in header or "LATITUDE (DECIMAL DEGREES)" in header:
                data, meta = pvlib.iotools.read_pvgis_tmy(path, pvgis_format="csv")
                data = data.loc[data.index.notna()]
                return {
                    "format": "pvgis_tmy",
                    "columns": list(data.columns),
                    "rows": data.head(5).reset_index().astype(str).values.tolist(),
                    "row_count": len(data),
                    "metadata": meta,
                }
            return {"format": "csv", "preview": svc.preview_csv(path)}
        (data, meta), _ = _read_epw(pvlib, path)
        return {
            "format": "epw",
            "columns": list(data.columns),
            "rows": data.head(5).reset_index().astype(str).values.tolist(),
            "row_count": len(data),
            "metadata": meta,
        }
    except Exception as e:
        location = "CSV line 1, column 1: " if safe.lower().endswith(".csv") else "EPW line 1, column 1: "
        raise HTTPException(400, detail={"error": f"{location}import preview failed: {e}"}) from e


@router.post("/uploads/{upload_id}/confirm")
def confirm(workspace_id: str, upload_id: str, filename: str, payload: dict):
    import pandas as pd
    import pvlib

    path = _stage(workspace_id, upload_id)
    safe = _bound_filename(path, filename)
    try:
        if safe.lower().endswith(".csv") and payload.get("format") != "pvgis_tmy":
            result = svc.confirm_csv(workspace_id, path, safe, payload["mapping"])
        elif safe.lower().endswith(".epw"):
            if _epw_records_per_hour(path) != 1:
                raise ValueError("EPW import supports one record per hour.")
            (data, meta), leap_day_rows = _read_epw(pvlib, path)
            index = data.index
            # EPW hour h labels the interval ending at h:00 local standard time; hour 24 rolls over.
            stamps = []
            for stamp in index:
                stamps.append((stamp.to_pydatetime() + timedelta(hours=1)).isoformat(timespec="minutes"))
            channels = {}
            for channel, col in (
                ("ghi", "ghi"),
                ("dni", "dni"),
                ("dhi", "dhi"),
                ("air_temperature", "temp_air"),
                ("wind_speed", "wind_speed"),
            ):
                if col in data.columns:
                    values = []
                    missing = {"ghi": 9999, "dni": 9999, "dhi": 9999, "air_temperature": 99.9, "wind_speed": 999}[
                        channel
                    ]
                    for value in data[col].tolist():
                        value = float(value)
                        values.append(
                            None
                            if not math.isfinite(value) or value == missing
                            else value + 273.15
                            if channel == "air_temperature"
                            else value
                        )
                    channels[channel] = values
            # EPW timestamps are local standard time with no DST; offset uses site's current standard offset.
            standard_offset = timezone(timedelta(hours=float(meta["TZ"])))
            tzstamp = []
            for raw in stamps:
                dt = datetime.fromisoformat(raw).replace(tzinfo=standard_offset, fold=0)
                tzstamp.append(dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"))
            result = svc.create_profile(
                workspace_id,
                name=str(payload.get("name") or safe),
                timestamps=tzstamp,
                channels=channels,
                resolution_minutes=_hourly_resolution(tzstamp),
                provenance={
                    "kind": "import",
                    "filename": safe,
                    "original_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "format": "epw",
                    "column_mapping": "pvlib.read_epw",
                    "parser": "pvlib.iotools.read_epw",
                    "pvlib_version": pvlib.__version__,
                    "metadata": meta,
                    "year_normalization": {
                        "method": "pvlib read_epw coerce_year",
                        "nominal_year": 2001,
                        "source_month_years": _epw_month_years(path),
                        "leap_day_rows_excluded": leap_day_rows,
                    },
                    "hour_ending_shift": "EPW hour h maps to interval end h:00 local standard time; hour 24 rolls over",
                },
                profile_label="representative year (TMY)" if len(_epw_month_years(path)) > 1 else None,
            )
        elif payload.get("format") == "pvgis_tmy":
            data, meta = _read_pvgis_tmy(pvlib, path)
            normalized_index = []
            keep_rows = []
            for index, stamp in enumerate(data.index):
                if pd.isna(stamp):
                    continue
                dt = stamp.to_pydatetime()
                if dt.month == 2 and dt.day == 29:
                    continue
                normalized_index.append(dt.replace(year=2001))
                keep_rows.append(index)
            data = data.iloc[keep_rows].copy()
            data.index = normalized_index
            zone_name = str(payload.get("timezone") or "UTC")
            stamps = []
            for t in data.index:
                dt = t.to_pydatetime()
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=ZoneInfo(zone_name))
                stamps.append(dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"))
            mapping = {"ghi": "ghi", "dni": "dni", "dhi": "dhi", "temp_air": "air_temperature"}
            channels = {}
            for col, ch in mapping.items():
                if col in data:
                    vals = data[col].tolist()
                    channels[ch] = [
                        None if pd.isna(v) else float(v) + (273.15 if ch == "air_temperature" else 0) for v in vals
                    ]
            stamps, channels, missing_hours_filled = _fill_hourly_gaps(stamps, channels)
            result = svc.create_profile(
                workspace_id,
                name=str(payload.get("name") or safe),
                timestamps=stamps,
                channels=channels,
                resolution_minutes=_hourly_resolution(stamps),
                provenance={
                    "kind": "import",
                    "filename": safe,
                    "original_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "format": "pvgis_tmy",
                    "parser": "pvlib.iotools.read_pvgis_tmy",
                    "pvlib_version": pvlib.__version__,
                    "metadata": meta,
                    "selected_month_year_pairs": meta.get("months_selected"),
                    "irradiance_time_offset": meta.get("inputs", {}).get("irradiance time offset"),
                    "year_normalization": {
                        "method": "pvlib coerce_year equivalent",
                        "nominal_year": 2001,
                        "missing_hourly_intervals_filled_with_null": missing_hours_filled,
                    },
                    "source_month_year_pairs": meta.get("months_selected"),
                    "irradiance_semantics": "hourly interval average; PVGIS timestamp offset preserved",
                },
                profile_label="representative year (TMY)",
            )
        else:
            raise ValueError("Unsupported import confirmation format.")
        path.unlink(missing_ok=True)
        path.with_suffix(".upload.json").unlink(missing_ok=True)
        return result
    except (ValueError, KeyError, TypeError, AttributeError, csv.Error) as error:
        message = str(error)
        if not message.lower().startswith(("csv line", "epw line")):
            prefix = "CSV" if safe.lower().endswith(".csv") else "EPW"
            message = f"{prefix} line 1, column 1: {message}"
        raise _error(svc.EnvironmentProfileError(message)) from error
