import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
VIEWER = REPO_ROOT / "frontend/src/components/BluecadGlbViewer.tsx"
SELECTION = REPO_ROOT / "frontend/src/components/bluecad/sceneSelection.ts"
WORKBENCH = REPO_ROOT / "frontend/src/components/bluecad/BluecadWorkbench.tsx"
APP = REPO_ROOT / "frontend/src/App.tsx"
SIDECAR = REPO_ROOT / "frontend/src/components/shell/ContextualSidecar.tsx"
SELECTION_TYPES = REPO_ROOT / "frontend/src/app/selection.ts"


def _source(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def test_viewer_semantic_candidate_is_strict_and_ancestor_ambiguous_fail_closed() -> None:
    source = _source(VIEWER)
    assert 'const BLUECAD_SEMANTIC_KEY = /^bluecad-part-sha256-[0-9a-f]{64}$/' in source
    assert "while (current)" in source
    assert "keys.add(name)" in source
    assert "current = current.parent" in source
    assert "if (keys.size !== 1) return null" in source
    assert "semanticKey: semanticKeyCandidate(mesh)" in source


def test_async_resolution_requires_every_current_scene_precondition() -> None:
    source = _source(SELECTION)
    for field in (
        "workspaceId",
        "candidateId",
        "artifactId",
        "viewerSessionId",
        "meshKey",
        "semanticKey",
    ):
        assert f"current.{field} === captured.{field}" in source
    assert 'return { state: "unresolved" }' in source
    assert 'return { state: "ambiguous" }' in source
    assert "artifact.roles.includes(MANIFEST_ROLE)" in source


def test_workbench_clears_semantic_target_before_late_resolution_can_publish() -> None:
    source = _source(WORKBENCH)
    assert "currentSceneSelection.current = null" in source
    assert "publishSelection(targetWorkspaceId, targetCandidateId)" in source
    assert "currentSceneSelection.current = captured" in source
    assert "if (!acceptsSceneSelectionResolution(currentSceneSelection.current, captured)) return" in source
    assert 'if (resolution.state !== "resolved") {' in source
    assert "setSceneBindingPresentation(resolution.state)" in source
    assert "publishBindingStatus(resolution.state)" in source
    assert 'kind: "bluecad-part"' in source
    assert "partId: resolution.part.partId" in source
    assert "partKind: resolution.part.partKind" in source


def test_unresolved_and_ambiguous_bindings_are_operator_visible_without_fabricated_identity() -> None:
    workbench = _source(WORKBENCH)
    sidecar = _source(SIDECAR)
    selection_types = _source(SELECTION_TYPES)
    assert 'type SceneBindingPresentation = "idle" | "resolving" | "unresolved" | "ambiguous"' in workbench
    assert 'setSceneBindingPresentation("resolving")' in workbench
    assert 'kind: "bluecad-binding-status"' in workbench
    assert 'state: "resolving" | "unresolved" | "ambiguous"' in selection_types
    assert "Unresolved engineering binding. Geometry remains viewable; candidate authority is unchanged." in workbench
    assert "Ambiguous engineering binding. Geometry remains viewable; no engineering object was selected." in workbench
    assert 'selection.kind === "bluecad-binding-status"' in sidecar
    assert "Unresolved engineering binding" in sidecar
    assert "Ambiguous engineering binding" in sidecar
    assert "No engineering object was selected." in sidecar
    assert 'role="status"' in sidecar
    assert "selection.partId" not in sidecar.split("function BindingStatusContext", 1)[1].split("function PropertiesFallback", 1)[0]


def test_resolved_part_has_human_context_and_machine_details_remain_secondary() -> None:
    app = _source(APP)
    sidecar = _source(SIDECAR)
    assert 'selection?.kind === "bluecad-part"' in app
    assert "selection.partId" in app
    assert "selection.partKind" in app
    assert 'selection?.kind === "bluecad-part"' in sidecar
    assert "selection.partId" in sidecar
    assert "selection.partKind" in sidecar
    assert "Technical details" in sidecar
    for machine_field in ("viewerSessionId", "meshKey", "semanticKey"):
        assert machine_field in sidecar
