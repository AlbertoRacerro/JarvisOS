from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_architecture_enforcement.py"


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    config = root / "configs" / "architecture_enforcement.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"exceptions": []}), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "--config", str(config)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_ae002_constructor_binding_preserves_same_line_source_order(tmp_path: Path) -> None:
    source = tmp_path / "backend/app/same_line.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import httpx\nimport requests\n"
        "def dispatch():\n"
        "    h = httpx.Client(); h.send(None)\n"
        "    r = requests.Session(); r.send(None)\n",
        encoding="utf-8",
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert result.stdout.count("AE002 backend/app/same_line.py::dispatch") == 2


def test_ae002_parameters_and_local_shadows_kill_import_and_module_provenance(tmp_path: Path) -> None:
    source = tmp_path / "backend/app/shadowed.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import httpx\nfrom requests import Session as RS\n"
        "class Other:\n"
        "    def get(self, *args): pass\n"
        "    def send(self, *args): pass\n"
        "def parameter(httpx, client, /, *, RS):\n"
        "    httpx.get('https://example.invalid')\n"
        "    client.send(None)\n"
        "    RS().send(None)\n"
        "def reassigned():\n"
        "    httpx = Other()\n"
        "    httpx.get('https://example.invalid')\n"
        "    RS = Other\n"
        "    RS().send(None)\n",
        encoding="utf-8",
    )
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_ae002_constructor_alias_still_detects_when_not_shadowed(tmp_path: Path) -> None:
    source = tmp_path / "backend/app/aliases.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "from httpx import Client as HC\nfrom requests import Session as RS\n"
        "def dispatch():\n"
        "    h = HC(); h.send(None)\n"
        "    r = RS(); r.send(None)\n",
        encoding="utf-8",
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert result.stdout.count("AE002 backend/app/aliases.py::dispatch") == 2


def test_ae002_chained_assignment_preserves_constructor_provenance(tmp_path: Path) -> None:
    source = tmp_path / "backend/app/chained.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import httpx\nimport requests\n"
        "def dispatch():\n"
        "    first = second = httpx.Client()\n"
        "    first.get('https://example.invalid')\n"
        "    second.send(None)\n"
        "    left = right = requests.Session()\n"
        "    left.post('https://example.invalid')\n"
        "    right.send(None)\n",
        encoding="utf-8",
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert result.stdout.count("AE002 backend/app/chained.py::dispatch") == 4


def test_ae002_context_manager_and_walrus_bindings_preserve_provenance(tmp_path: Path) -> None:
    source = tmp_path / "backend/app/contextual.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import aiohttp\nimport httpx\nimport requests\n"
        "def sync_dispatch():\n"
        "    with requests.Session() as session:\n"
        "        session.post('https://example.invalid')\n"
        "    if (client := httpx.Client()):\n"
        "        client.request('GET', 'https://example.invalid')\n"
        "async def async_dispatch():\n"
        "    async with aiohttp.ClientSession() as session:\n"
        "        await session.ws_connect('https://example.invalid')\n",
        encoding="utf-8",
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert result.stdout.count("AE002 backend/app/contextual.py::sync_dispatch") == 2
    assert result.stdout.count("AE002 backend/app/contextual.py::async_dispatch") == 1


def test_ae002_sibling_local_import_does_not_overwrite_module_alias(tmp_path: Path) -> None:
    source = tmp_path / "backend/app/sibling_shadow.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import httpx\n"
        "def network():\n"
        "    httpx.get('https://example.invalid')\n"
        "def shadow():\n"
        "    import custom_transport as httpx\n"
        "    httpx.get('not-network')\n",
        encoding="utf-8",
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert result.stdout.count("AE002 backend/app/sibling_shadow.py::network") == 1
    assert "AE002 backend/app/sibling_shadow.py::shadow" not in result.stdout


def test_ae002_reverse_sibling_network_import_does_not_contaminate_other_scope(tmp_path: Path) -> None:
    source = tmp_path / "backend/app/reverse_shadow.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import custom_transport as httpx\n"
        "def safe():\n"
        "    httpx.get('not-network')\n"
        "def network():\n"
        "    import httpx\n"
        "    httpx.get('https://example.invalid')\n",
        encoding="utf-8",
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "AE002 backend/app/reverse_shadow.py::safe" not in result.stdout
    assert result.stdout.count("AE002 backend/app/reverse_shadow.py::network") == 1


def test_ae002_local_from_import_shadows_module_constructor_alias(tmp_path: Path) -> None:
    source = tmp_path / "backend/app/from_shadow.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "from httpx import Client as HC\n"
        "def safe():\n"
        "    from custom_transport import Client as HC\n"
        "    HC().send(None)\n"
        "def network():\n"
        "    HC().send(None)\n",
        encoding="utf-8",
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "AE002 backend/app/from_shadow.py::safe" not in result.stdout
    assert result.stdout.count("AE002 backend/app/from_shadow.py::network") == 1


def test_ae002_late_local_import_kills_global_alias_before_import(tmp_path: Path) -> None:
    source = tmp_path / "backend/app/late_import.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import httpx\n"
        "def invalid_runtime_reference():\n"
        "    httpx.get('https://example.invalid')\n"
        "    import custom_transport as httpx\n",
        encoding="utf-8",
    )
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
