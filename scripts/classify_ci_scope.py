#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys

FULL_SCOPE = {
    "docs_only": False,
    "run_backend": True,
    "run_frontend": True,
    "run_bluecad": True,
    "full_required": True,
}

DOCS_ONLY_SCOPE = {
    "docs_only": True,
    "run_backend": False,
    "run_frontend": False,
    "run_bluecad": False,
    "full_required": False,
}

# These backend locations are shared integration/control boundaries. A change
# here can affect multiple runtime domains, so fail closed to the complete suite.
SHARED_BACKEND_PREFIXES = (
    "backend/app/api/",
    "backend/app/core/",
    "backend/app/schemas/",
)
SHARED_BACKEND_PATHS = {
    "backend/app/__init__.py",
    "backend/app/main.py",
    "backend/pyproject.toml",
    "backend/requirements.txt",
    "backend/requirements-dev.txt",
}

# BLUECAD is an explicit specialist domain. Ordinary backend modules do not pay
# its geometry/FEM regression cost unless the diff touches this domain or a
# shared/dependency boundary above. Engineering is included conservatively
# because it is the adjacent integration owner for later process/solver work.
BLUECAD_PREFIXES = (
    "backend/app/modules/bluecad/",
    "backend/app/modules/engineering/",
    "backend/tests/bluecad/",
)


def _is_backend_root_control(path: str) -> bool:
    if not path.startswith("backend/"):
        return False
    remainder = path[len("backend/") :]
    return "/" not in remainder


def classify_scope(paths: list[str]) -> dict[str, bool]:
    # Paths are Git evidence, not user prose: preserve every character.
    # Lossy normalization could turn a non-doc path such as " docs/a.md"
    # into an apparent docs/ path and incorrectly grant a fast path.
    changed = [path for path in paths if path]

    # Empty/unknown input fails closed to the complete suite.
    if not changed:
        return dict(FULL_SCOPE)

    if all(path.startswith("docs/") for path in changed):
        return dict(DOCS_ONLY_SCOPE)

    # Only docs/backend/frontend are independently classifiable. Root files,
    # workflows, scripts, or unfamiliar paths force the complete suite. Rename
    # collapsing is disabled by the workflow, so cross-domain moves are seen as
    # delete+add and cannot gain a narrower lane.
    if not all(path.startswith(("docs/", "backend/", "frontend/")) for path in changed):
        return dict(FULL_SCOPE)

    # Dependency manifests and shared backend roots can influence BLUECAD and
    # other domains indirectly, so they also fail closed instead of relying on
    # fragile source-file-to-test impact inference.
    if any(
        path in SHARED_BACKEND_PATHS
        or path.startswith(SHARED_BACKEND_PREFIXES)
        or _is_backend_root_control(path)
        for path in changed
    ):
        return dict(FULL_SCOPE)

    run_backend = any(path.startswith("backend/") for path in changed)
    frontend_changed = any(path.startswith("frontend/") for path in changed)
    run_bluecad = any(path.startswith(BLUECAD_PREFIXES) for path in changed)

    return {
        "docs_only": False,
        "run_backend": run_backend,
        # Backend contracts are still mirrored manually in the frontend until
        # the accepted contract-codegen hardening lands. Preserve frontend build
        # as a cheap parallel regression gate for every backend change.
        "run_frontend": run_backend or frontend_changed,
        "run_bluecad": run_bluecad,
        "full_required": False,
    }


def self_test() -> None:
    assert classify_scope(["docs/specs/STATUS.md"]) == DOCS_ONLY_SCOPE

    frontend = classify_scope(["frontend/src/App.tsx"])
    assert frontend["run_frontend"]
    assert not frontend["run_backend"]
    assert not frontend["run_bluecad"]

    ai_backend = classify_scope(["backend/app/modules/ai/router.py"])
    assert ai_backend["run_backend"]
    assert ai_backend["run_frontend"]
    assert not ai_backend["run_bluecad"]
    assert not ai_backend["full_required"]

    bluecad = classify_scope(["backend/app/modules/bluecad/mesh_adapter.py"])
    assert bluecad["run_backend"]
    assert bluecad["run_frontend"]
    assert bluecad["run_bluecad"]
    assert not bluecad["full_required"]

    bluecad_test = classify_scope(["backend/tests/bluecad/test_capped_manifold.py"])
    assert bluecad_test["run_backend"] and bluecad_test["run_bluecad"]

    engineering = classify_scope(["backend/app/modules/engineering/service.py"])
    assert engineering["run_backend"] and engineering["run_bluecad"]

    mixed = classify_scope(
        ["docs/a.md", "backend/app/modules/ai/router.py", "frontend/src/App.tsx"]
    )
    assert mixed["run_backend"] and mixed["run_frontend"]
    assert not mixed["run_bluecad"]
    assert not mixed["full_required"]

    for paths in (
        [],
        ["README.md"],
        [".github/workflows/ci.yml"],
        ["scripts/check_spec_status.py"],
        ["backend/requirements.txt"],
        ["backend/pyproject.toml"],
        ["backend/app/main.py"],
        ["backend/app/core/config.py"],
        ["backend/app/schemas/common.py"],
        [" docs/a.md"],
        ["\tdocs/a.md"],
        ["docs/a.md", "scripts/check_spec_status.py"],
    ):
        scope = classify_scope(paths)
        assert scope == FULL_SCOPE

    print("ci-scope: self-test OK")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return 0

    scope = classify_scope(sys.stdin.read().splitlines())
    for key in ("docs_only", "run_backend", "run_frontend", "run_bluecad", "full_required"):
        print(f"{key}={'true' if scope[key] else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
