"""Bubu navigation guide: route-prefix selection, size bounds, drift against the frontend target registry, and the
multi-step pointing instruction in the visible-page block."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.modules.ai import thread_app_map
from app.modules.ai.context_builder import assemble_prompt
from app.modules.ai.thread_app_map import MAX_AREA_CHARS, MAX_GUIDE_CHARS, app_map_for_route
from app.modules.ai.thread_ui_context import (
    APP_MAP_HEADER,
    POINTING_INSTRUCTIONS,
    VISIBLE_PAGE_SOURCE,
    UiContext,
    render_visible_page_block,
)

ROUTES = [
    "/design/project", "/design/project?stage=plant", "/design/project?stage=dynamic", "/design/project?stage=cad",
    "/design/project?stage=mechanics", "/design/project?stage=cfd", "/design/project?stage=cfd&component=c1&node=results",
    "/design/project?stage=optics", "/design/project?stage=reduced-model", "/design/studio", "/design/process",
    "/design/studies", "/design/bluecad", "/memory/project-basis", "/memory/models", "/memory/literature",
    "/development/roadmap/timeline", "/coding/repository", "/coding/runtime",
    "/settings/appearance", "/settings/ai", "/settings/system", "/runs", "/engineering-data", "/", "", "/x",
]
# The frontend parser (pointerDirectives.ts) accepts ids of [\w.:-]+ and any label up to "]".
FRONTEND_DIRECTIVE = re.compile(r"\[\[\s*point\s*:\s*([\w.:-]+)\s*(?:\|([^\]\n]*))?\]\]", re.IGNORECASE)


def _ui(route: str, targets: int = 2) -> UiContext:
    return UiContext(route=route, title="T", targets=[{"id": f"t.{i}", "role": "button", "name": f"n{i}"} for i in range(targets)])


def test_guide_is_selected_by_route_prefix() -> None:
    design = app_map_for_route("/design/project")
    assert "Components tree" in design and "design.tree.node.<" in design and "Library fold" in design
    assert "Explanation & code" not in design

    studio = app_map_for_route("/design/studio")
    assert "Explanation & code" in studio and "design.workshop.view-<" in studio and "Components tree" not in studio

    assert "Solver console" in app_map_for_route("/design/process") and "Components tree" not in app_map_for_route("/design/process")
    assert "Literature" in app_map_for_route("/memory/models")
    assert "Roadmap" in app_map_for_route("/development/roadmap/timeline")
    assert "Repository" in app_map_for_route("/coding/runtime")
    assert "Appearance" in app_map_for_route("/settings/ai")

    # Pages without an area keep only the rail; they never get another area's text.
    for route in ("/runs", "/engineering-data", "/design/studies", "/x", "", "/designer"):
        guide = app_map_for_route(route)
        assert "Back/Forward" in guide and "Components tree" not in guide and "Explanation & code" not in guide


def test_every_page_gets_the_rail_with_the_way_back_to_the_home_project() -> None:
    for route in ROUTES:
        guide = app_map_for_route(route)
        assert "rail.back" in guide and "Back to <project>" in guide and "rail.design" in guide
        for section in ("Memory", "Development", "Coding", "Settings"):
            assert section in guide


def test_design_stage_addendum_follows_the_stage_query() -> None:
    cfd = app_map_for_route("/design/project?stage=cfd")
    assert "design.stage.cfd" in cfd and "design.tree.node.results" in cfd and "design.field.run" in cfd
    assert "NOT where simulation results are" in cfd  # the Library is not results
    assert "Solver console" in app_map_for_route("/design/project") and "Solver console" in app_map_for_route("/design/project?stage=plant")
    assert "design.dynamic.steps" in app_map_for_route("/design/project?stage=dynamic")
    assert "design.dynamic.steps" not in app_map_for_route("/design/project?stage=cfd")
    # A component in the query does not change which area the page belongs to.
    assert app_map_for_route("/design/project?stage=cfd&component=abc&node=results") == cfd
    # An unknown stage falls back to the plain Design area.
    assert app_map_for_route("/design/project?stage=nope") == app_map_for_route("/design/project?stage=mechanics")


def test_comsol_line_surfaces_are_described_with_their_real_target_ids() -> None:
    cad = app_map_for_route("/design/project?stage=cad")
    for word in ("Feature inspector", "design.panel.feature-inspector", "Add tube", "Enabled", "Rebuild geometry", "Undo",
                 "Placed freely", "Connected to previous outlet", "Heading", "Place in viewport", "design.tree.node.geometry"):
        assert word in cad, word
    cfd = app_map_for_route("/design/project?stage=cfd")
    for word in ("design.panel.cfd-field-3d", "Show full pipe", "symmetry", "not computed", "design.field.run"):
        assert word in cfd, word
    assert "design.panel.feature-inspector" not in cfd and "design.panel.cfd-field-3d" not in cad
    assert {"design.panel.feature-inspector", "design.panel.cfd-field-3d"} <= _registry_ids()


def test_guide_stays_short() -> None:
    texts = [thread_app_map._RAIL, thread_app_map._DESIGN, *thread_app_map._DESIGN_STAGES.values(), *(t for _, t in thread_app_map._AREAS)]
    for text in texts:
        assert 0 < len(text) <= MAX_AREA_CHARS, text[:60]
    for route in ROUTES:
        # strictly below the cap: the cap is a safety net, never a silent mid-sentence cut
        assert len(app_map_for_route(route)) < MAX_GUIDE_CHARS, route


def _registry_ids() -> set[str]:
    source = Path(__file__).resolve().parents[2] / "frontend" / "src" / "app" / "designTargets.ts"
    if not source.exists():
        pytest.skip("frontend sources not available")
    return set(re.findall(r'^\s*"((?:rail|design)\.[a-z0-9.-]+)":', source.read_text(encoding="utf-8"), re.MULTILINE))


def _ids_in(text: str) -> set[str]:
    """Full ids plus the families written as prefix.<a|b|c>, expanded; prefix.<name> (dynamic ids) is skipped."""
    found: set[str] = set()
    for prefix, options in re.findall(r"((?:design|rail)\.[a-z0-9.-]*)<([^>]+)>", text):
        if "|" in options:
            found |= {f"{prefix}{option}" for option in options.split("|")}
    text = re.sub(r"(?:design|rail)\.[a-z0-9.-]*<[^>]+>", " ", text)
    found |= set(re.findall(r"(?<![\w.])(?:design|rail)\.[a-z0-9-]+(?:\.[a-z0-9-]+)*", text))
    return found


def test_every_target_id_in_the_guide_exists_in_the_frontend_registry() -> None:
    registry = _registry_ids()
    assert len(registry) > 30
    mentioned: set[str] = set()
    for route in ROUTES:
        mentioned |= _ids_in(app_map_for_route(route))
    assert {"design.stage.cfd", "design.tree.node.results", "design.tree.node.mesh", "design.field.run", "design.tree", "rail.back"} <= mentioned
    assert mentioned - registry == set(), sorted(mentioned - registry)


def test_pointing_instruction_is_multi_step_first_step_ids_only() -> None:
    text = POINTING_INSTRUCTIONS
    assert "only ids listed above" in text and "never invent an id" in text
    assert "not on screen yet" in text and "steps in order" in text and "FIRST step" in text
    assert "name the later steps in words" in text
    assert "do not point at Library entries" in text
    assert "[[point:<id>|1. CFD]]" in text
    # Only the two protocol examples may look like directives (page text cannot add more either).
    assert text.count("[[point:") == 2
    # The directive syntax stays what the frontend parses, with or without a step number.
    assert FRONTEND_DIRECTIVE.search("[[point:design.stage.cfd]]").groups() == ("design.stage.cfd", None)
    assert FRONTEND_DIRECTIVE.search("[[point:design.stage.cfd|1. CFD]]").groups() == ("design.stage.cfd", "1. CFD")


def test_visible_page_block_order_and_bounds() -> None:
    ui = UiContext(route="/design/project?stage=cfd", title="Design", targets=[
        {"id": "design.stage.cfd", "role": "button", "name": "CFD"}, {"id": "design.tree.component.c1-cfd", "role": "treeitem", "name": "C1-CFD"}])
    block = render_visible_page_block(ui)
    assert block.index("- design.stage.cfd") < block.index(APP_MAP_HEADER) < block.index("design.tree.node.results") < block.index(POINTING_INSTRUCTIONS)
    assert block.endswith(POINTING_INSTRUCTIONS)
    assert "- design.tree.component.c1-cfd · treeitem · C1-CFD" in block
    # Target listing keeps its own budget; the guide and the rules come on top and stay bounded.
    full = UiContext(route="/design/project?stage=cfd", title="T", targets=[
        {"id": f"t.{i}", "role": "button", "name": "n" * 150} for i in range(150)])
    rendered = render_visible_page_block(full)
    assert len(rendered.split(APP_MAP_HEADER)[0]) <= 5100
    assert len(rendered) <= 5100 + MAX_GUIDE_CHARS + len(POINTING_INSTRUCTIONS) + len(APP_MAP_HEADER) + 4


def test_requests_without_ui_context_carry_no_guide() -> None:
    assert assemble_prompt([], "plain question") == "plain question"
    prompt = assemble_prompt([{"source": "jarvis:surface-brief", "content": "brief"}], "q")
    assert APP_MAP_HEADER not in prompt and "point:" not in prompt
    with_page = assemble_prompt([{"source": VISIBLE_PAGE_SOURCE, "content": render_visible_page_block(_ui("/design/project"))}], "q")
    assert APP_MAP_HEADER in with_page and with_page.rstrip().endswith("USER_REQUEST:\nq")
