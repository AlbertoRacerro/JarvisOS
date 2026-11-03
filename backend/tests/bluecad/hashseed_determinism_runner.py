from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from tests.bluecad.property_geometry_support import artifact_hashes, build_and_assert

CANONICAL_PYTHONHASHSEED = "0"
FIXTURE_ROOT = Path(__file__).parent / "fixtures"


def _child_snapshot(relative_path: str) -> dict[str, Any]:
    spec_path = FIXTURE_ROOT / relative_path
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    with TemporaryDirectory(prefix="bluecad-hashseed-child-") as directory:
        result = build_and_assert(spec, Path(directory))
    assert result.manifest is not None
    return {
        "hash_seed": os.environ.get("PYTHONHASHSEED"),
        "spec_id": result.spec_id,
        "manifest_digest": result.manifest["manifest_digest"],
        "artifact_sha256": artifact_hashes(result.manifest),
    }


def _run_pinned_child(relative_path: str) -> dict[str, Any]:
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = CANONICAL_PYTHONHASHSEED
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.bluecad.hashseed_determinism_runner",
            "child",
            relative_path,
        ],
        cwd=Path(__file__).parents[2],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return json.loads(completed.stdout)


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in {"parent", "child"}:
        raise SystemExit(
            "usage: python -m tests.bluecad.hashseed_determinism_runner "
            "{parent|child} <fixture-relative-path>"
        )

    mode, relative_path = sys.argv[1:]
    if mode == "child":
        payload: dict[str, Any] = _child_snapshot(relative_path)
    else:
        payload = {
            "parent_hash_seed": os.environ.get("PYTHONHASHSEED"),
            "child": _run_pinned_child(relative_path),
        }
    print(json.dumps(payload, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
