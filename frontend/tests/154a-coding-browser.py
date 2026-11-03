"""Real-browser Repository regression for the operator launcher.

Run on the maintainer's machine against a started JarvisOS instance:
    python frontend/tests/154a-coding-browser.py --base-url http://127.0.0.1:8000

Requires Playwright Python and a Chromium browser. --cdp-url connects to an
already running browser, which is useful when the test runs from Windows.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import expect, sync_playwright


SOURCE_FILES = ("AGENTS.md", "docs/specs/STATUS.md", "backend/app/main.py", "frontend/src/App.tsx")


def fixture_route(route, root: Path, sha: str) -> None:
    url = urlparse(route.request.url)
    query = parse_qs(url.query)
    path = query.get("path", [""])[0]
    operation = url.path.rsplit("/", 1)[-1]
    payload: dict = {}
    if operation == "tree":
        children: set[str] = set()
        prefix = f"{path}/" if path else ""
        for source in SOURCE_FILES:
            if source.startswith(prefix):
                remainder = source[len(prefix):]
                first = remainder.split("/", 1)[0]
                children.add(first)
        payload["entries"] = [
            {"path": prefix + child, "type": "dir" if any(
                source.startswith(prefix + child + "/") for source in SOURCE_FILES
            ) else "file"}
            for child in sorted(children)
        ]
    elif operation == "file":
        assert path in SOURCE_FILES, f"Unexpected file requested: {path}"
        payload["text"] = (root / path).read_text(encoding="utf-8")
    elif operation == "url":
        payload["url"] = f"https://github.com/AlbertoRacerro/JarvisOS/blob/{sha}/{path}"
    elif operation == "search":
        literal = query.get("literal", [""])[0]
        payload["matches"] = [
            {"path": source, "line": next((n for n, line in enumerate((root / source).read_text(encoding="utf-8").splitlines(), 1) if literal in line), 1), "offset": 0}
            for source in SOURCE_FILES if literal in (root / source).read_text(encoding="utf-8")
        ]
    elif operation != "ref":
        raise AssertionError(f"Unexpected repository operation: {operation}")
    route.fulfill(json={"provider": "local-browser-fixture", "repository": "AlbertoRacerro/JarvisOS", "operation": operation,
                        "requested_ref": "master", "resolved_sha": sha, "partial": False, "payload": payload,
                        "observed_at": "2026-09-26T00:00:00Z"})


def run(base_url: str, cdp_url: str | None, screenshots: Path | None, mock_repository: bool, repo_root: Path, fixture_sha: str | None, width: int) -> None:
    with sync_playwright() as playwright:
        browser = (
            playwright.chromium.connect_over_cdp(cdp_url)
            if cdp_url else playwright.chromium.launch(headless=True)
        )
        page = browser.new_page(viewport={"width": width, "height": 1000}, device_scale_factor=1)
        failures: list[str] = []
        page.on("pageerror", lambda error: failures.append(str(error)))
        if mock_repository:
            sha = fixture_sha or subprocess.check_output(["git", "-C", str(repo_root), "rev-parse", "HEAD"], text=True).strip()
            assert re.fullmatch(r"[0-9a-f]{40}", sha)
            page.route("**/api/coding/repository/**", lambda route: fixture_route(route, repo_root, sha))
        page.goto(f"{base_url.rstrip('/')}/coding/repository", wait_until="networkidle")
        surface = page.get_by_test_id("coding-repository-surface")
        expect(surface).to_be_visible()
        viewport = surface.locator(".repository-viewport")

        def choose_folder(name: str) -> None:
            row = surface.get_by_role("button", name=re.compile(rf"^Folder {re.escape(name)}(?:,|$)"))
            expect(row).to_have_count(1)
            row.click()

        def choose_file(name: str) -> None:
            row = surface.get_by_role("button", name=re.compile(rf"^File {re.escape(name)}(?:,|$)"))
            expect(row).to_have_count(1)
            row.click()

        def open_path(path: str, expected: str) -> None:
            root_crumb = surface.get_by_role("navigation", name="Repository breadcrumb").get_by_role("button", name="JarvisOS", exact=True)
            if root_crumb.is_visible():
                root_crumb.click()
            parts = path.split("/")
            for part in parts[:-1]:
                choose_folder(part)
            if len(parts) > 2:
                expect(surface.get_by_role("navigation", name="Repository breadcrumb")).to_contain_text(parts[-2])
                page.go_back()
                expect(surface.get_by_role("navigation", name="Repository breadcrumb")).to_contain_text(parts[-3])
                page.go_forward()
                expect(surface.get_by_role("navigation", name="Repository breadcrumb")).to_contain_text(parts[-2])
            choose_file(parts[-1])
            expect(surface.get_by_role("navigation", name="Repository breadcrumb").get_by_text(parts[-1], exact=True)).to_be_visible()
            expect(surface.get_by_label("File content")).to_contain_text(expected)
            page.go_back()
            expect(surface.get_by_role("list", name="Repository files")).to_be_visible()
            page.go_forward()
            expect(surface.get_by_label("File content")).to_contain_text(expected)
            surface.get_by_role("button", name="Back to directory").click()
            expect(surface.get_by_role("list", name="Repository files")).to_be_visible()

        open_path("AGENTS.md", "JarvisOS")
        open_path("docs/specs/STATUS.md", "STATUS")
        open_path("backend/app/main.py", "FastAPI")
        open_path("frontend/src/App.tsx", "Layout")

        surface.get_by_text("Search repository contents", exact=True).click()
        surface.get_by_label("Literal repository search").fill("JarvisOS")
        surface.get_by_role("button", name="Search", exact=True).click()
        expect(surface.get_by_role("status")).to_contain_text("matches", timeout=60000)
        if screenshots:
            screenshots.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshots / f"coding-repository-{width}x1000.png"), full_page=True)
        assert not failures, f"Browser errors: {failures}"
        print(f"Coding Repository browser regression passed ({'local repository fixture' if mock_repository else 'live remote'}): four files, nested tree, search, no page errors")
        browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--cdp-url")
    parser.add_argument("--screenshots", type=Path)
    parser.add_argument("--mock-repository", action="store_true", help="Intercept Coding repository reads with current local file contents for repeatable UI proof")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--fixture-sha", help="Exact local commit for fixture mode when Windows Git cannot read a WSL UNC worktree")
    parser.add_argument("--width", type=int, default=1600, help="Browser CSS viewport width (default: 1600)")
    args = parser.parse_args()
    run(args.base_url, args.cdp_url, args.screenshots, args.mock_repository, args.repo_root, args.fixture_sha, args.width)
