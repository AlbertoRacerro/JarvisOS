from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_architecture_enforcement.py"


def _write_config(root: Path, exceptions: list[dict[str, str]] | None = None) -> Path:
    path = root / "configs" / "architecture_enforcement.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"exceptions": exceptions or []}), encoding="utf-8")
    return path


def _run(root: Path, config: Path | None = None) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(SCRIPT), "--root", str(root)]
    if config is not None:
        command += ["--config", str(config)]
    return subprocess.run(command, check=False, capture_output=True, text=True)


def _exception(rule_id: str, exact_match: str, owner: str = "test-owner") -> dict[str, str]:
    return {
        "rule_id": rule_id,
        "exact_match": exact_match,
        "classification": "accepted_test_fixture",
        "owner_or_removal_spec": owner,
        "rationale": "focused deterministic fixture",
    }


def test_self_test() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--self-test"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "PASS" in result.stdout


def test_ae001_accepts_exact_owner_and_rejects_aliased_connection(tmp_path: Path) -> None:
    owner = tmp_path / "backend/app/core/database.py"
    owner.parent.mkdir(parents=True)
    owner.write_text(
        "import sqlite3\n\ndef open_sqlite_connection():\n    return sqlite3.connect('ok')\n",
        encoding="utf-8",
    )
    bad = tmp_path / "backend/app/feature.py"
    bad.write_text(
        "import sqlite3 as db\n\ndef open_other():\n    return db.connect('bad')\n",
        encoding="utf-8",
    )
    config = _write_config(
        tmp_path,
        [_exception("AE001", "backend/app/core/database.py::open_sqlite_connection", "core database boundary")],
    )
    result = _run(tmp_path, config)
    assert result.returncode == 1
    assert "AE001 backend/app/feature.py::open_other" in result.stdout
    assert "database.py::open_sqlite_connection" not in result.stdout


def test_ae001_exact_exception_does_not_cover_sibling_symbol(tmp_path: Path) -> None:
    source = tmp_path / "scripts/data_root_recovery/snapshot.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import sqlite3\n"
        "def create_snapshot():\n    return sqlite3.connect('accepted')\n"
        "def unexpected_snapshot_owner():\n    return sqlite3.connect('bad')\n",
        encoding="utf-8",
    )
    config = _write_config(
        tmp_path,
        [_exception("AE001", "scripts/data_root_recovery/snapshot.py::create_snapshot", "021b")],
    )
    result = _run(tmp_path, config)
    assert result.returncode == 1
    assert "AE001 scripts/data_root_recovery/snapshot.py::unexpected_snapshot_owner" in result.stdout
    assert "snapshot.py::create_snapshot" not in result.stdout


def test_ae002_preserves_exact_debt_and_rejects_new_dispatch(tmp_path: Path) -> None:
    debt = tmp_path / "backend/app/modules/ai/deepseek_provider_smoke.py"
    debt.parent.mkdir(parents=True)
    debt.write_text(
        "from app.modules.ai.providers.deepseek import DeepSeekProviderAdapter\n"
        "def run_provider_smoke(a):\n    return a.complete('x')\n",
        encoding="utf-8",
    )
    bad = tmp_path / "backend/app/new_network.py"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(
        "import httpx as h\n\ndef send():\n    return h.post('https://example.invalid')\n",
        encoding="utf-8",
    )
    config = _write_config(
        tmp_path,
        [_exception("AE002", "backend/app/modules/ai/deepseek_provider_smoke.py::run_provider_smoke", "129")],
    )
    result = _run(tmp_path, config)
    assert result.returncode == 1
    assert "AE002 backend/app/new_network.py::send" in result.stdout
    assert "deepseek_provider_smoke.py::run_provider_smoke" not in result.stdout


def test_ae002_exact_exception_does_not_cover_sibling_symbol(tmp_path: Path) -> None:
    source = tmp_path / "backend/app/modules/ai/execution.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "from app.modules.ai.providers.deepseek import DeepSeekProviderAdapter\n"
        "def run_ai_task(adapter):\n    return adapter.complete('accepted')\n"
        "def bypass(adapter):\n    return adapter.complete('bad')\n",
        encoding="utf-8",
    )
    config = _write_config(
        tmp_path,
        [_exception("AE002", "backend/app/modules/ai/execution.py::run_ai_task", "059b")],
    )
    result = _run(tmp_path, config)
    assert result.returncode == 1
    assert "AE002 backend/app/modules/ai/execution.py::bypass" in result.stdout
    assert "execution.py::run_ai_task" not in result.stdout


def test_ae002_detects_frozen_concrete_network_families(tmp_path: Path) -> None:
    source = tmp_path / "backend/app/network_families.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import aiohttp\nimport http.client\nimport httpx\nimport requests\nimport socket\nimport urllib.request\nimport urllib3\nimport websockets\n"
        "def dispatch():\n"
        "    httpx.head('https://example.invalid')\n"
        "    requests.options('https://example.invalid')\n"
        "    urllib.request.urlopen('https://example.invalid')\n"
        "    urllib3.request('GET', 'https://example.invalid')\n"
        "    socket.create_connection(('example.invalid', 443))\n"
        "    websockets.connect('wss://example.invalid')\n"
        "    aiohttp.request('GET', 'https://example.invalid')\n",
        encoding="utf-8",
    )
    result = _run(tmp_path, _write_config(tmp_path))
    assert result.returncode == 1
    assert result.stdout.count("AE002 backend/app/network_families.py::dispatch") == 7


def test_ae002_detects_constructor_bound_methods_and_ignores_unrelated_names(tmp_path: Path) -> None:
    source = tmp_path / "backend/app/bound_network.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import aiohttp\nimport http.client\nimport httpx\nimport requests\nimport socket\nimport urllib.request\nimport urllib3\n"
        "class Other:\n"
        "    def send(self): pass\n"
        "    def urlopen(self): pass\n"
        "    def ws_connect(self): pass\n"
        "def dispatch():\n"
        "    h = httpx.Client(); h.send(None)\n"
        "    r = requests.Session(); r.send(None)\n"
        "    p = urllib3.PoolManager(); p.urlopen('GET', 'https://example.invalid')\n"
        "    a = aiohttp.ClientSession(); a.ws_connect('https://example.invalid')\n"
        "    c = http.client.HTTPConnection('example.invalid'); c.send(b'x'); c.endheaders()\n"
        "    s = socket.socket(); s.connect(('example.invalid', 443)); s.sendto(b'x', ('example.invalid', 443))\n"
        "    o = urllib.request.build_opener(); o.open('https://example.invalid')\n"
        "    x = Other(); x.send(); x.urlopen(); x.ws_connect()\n",
        encoding="utf-8",
    )
    result = _run(tmp_path, _write_config(tmp_path))
    assert result.returncode == 1
    assert result.stdout.count("AE002 backend/app/bound_network.py::dispatch") == 9


def test_ae002_binding_provenance_is_lexical_and_source_ordered(tmp_path: Path) -> None:
    source = tmp_path / "backend/app/scoped.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import httpx\n"
        "class Other:\n    def send(self, value): return value\n"
        "def first():\n    c = httpx.Client()\n    c.send(None)\n"
        "def second():\n    c = Other()\n    c.send(None)\n"
        "def third():\n    c = Other()\n    c.send(None)\n    c = httpx.Client()\n    c.send(None)\n",
        encoding="utf-8",
    )
    result = _run(tmp_path, _write_config(tmp_path))
    assert result.returncode == 1
    assert "AE002 backend/app/scoped.py::first" in result.stdout
    assert "AE002 backend/app/scoped.py::third" in result.stdout
    assert "AE002 backend/app/scoped.py::second" not in result.stdout


def test_ae002_class_namespace_aliases_do_not_leak_into_methods(tmp_path: Path) -> None:
    source = tmp_path / "backend/app/class_scopes.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import httpx\n"
        "class Namespace:\n"
        "    import unrelated as httpx\n"
        "    httpx.get('safe')\n"
        "    def method(self):\n"
        "        httpx.get('https://example.invalid')\n"
        "class NetworkNamespace:\n"
        "    import requests as transport\n"
        "    transport.get('https://example.invalid')\n",
        encoding="utf-8",
    )
    result = _run(tmp_path, _write_config(tmp_path))
    assert result.returncode == 1
    assert "AE002 backend/app/class_scopes.py::method" in result.stdout
    assert "AE002 backend/app/class_scopes.py::NetworkNamespace" in result.stdout
    assert "class_scopes.py::Namespace" not in result.stdout


def test_ae002_retains_heterogeneous_possible_clients_across_control_flow(tmp_path: Path) -> None:
    source = tmp_path / "backend/app/possible_clients.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import aiohttp\nimport http.client\nimport httpx\nimport requests\nimport urllib3\n"
        "def conditional(flag):\n"
        "    if flag:\n        client = httpx.Client()\n"
        "    else:\n        client = requests.Session()\n"
        "    client.get('https://example.invalid')\n"
        "def loop(items):\n"
        "    client = urllib3.PoolManager()\n"
        "    for item in items:\n        client = aiohttp.ClientSession()\n"
        "    client.request('GET', 'https://example.invalid')\n"
        "async def async_loop(items):\n"
        "    client = httpx.AsyncClient()\n"
        "    async for item in items:\n        client = object()\n"
        "    client.get('https://example.invalid')\n"
        "def repeated(flag):\n"
        "    client = requests.Session()\n"
        "    while flag:\n        client = object()\n"
        "    client.get('https://example.invalid')\n"
        "def guarded():\n"
        "    try:\n        client = http.client.HTTPConnection('example.invalid')\n"
        "    except Exception:\n        client = urllib3.PoolManager()\n"
        "    client.request('GET', '/')\n"
        "def grouped():\n"
        "    try:\n        raise ExceptionGroup('x', [ValueError()])\n"
        "    except* ValueError:\n        client = httpx.Client()\n"
        "    client.get('https://example.invalid')\n"
        "def matched(value):\n"
        "    match value:\n"
        "        case 1:\n            client = aiohttp.ClientSession()\n"
        "        case _:\n            client = object()\n"
        "    client.get('https://example.invalid')\n"
        "def definite():\n"
        "    client = httpx.Client()\n"
        "    client = object()\n"
        "    client.get('safe')\n",
        encoding="utf-8",
    )
    result = _run(tmp_path, _write_config(tmp_path))
    assert result.returncode == 1
    for symbol in (
        "conditional",
        "loop",
        "async_loop",
        "repeated",
        "guarded",
        "grouped",
        "matched",
    ):
        assert f"AE002 backend/app/possible_clients.py::{symbol}" in result.stdout
    assert "possible_clients.py::definite" not in result.stdout


def test_ae002_recognizes_only_public_websockets_connect_paths(tmp_path: Path) -> None:
    source = tmp_path / "backend/app/websocket_paths.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import websockets.sync.client as sync_ws\n"
        "from websockets.asyncio.client import connect as async_connect\n"
        "import unrelated\n"
        "def sync_dispatch():\n    sync_ws.connect('wss://example.invalid')\n"
        "def async_dispatch():\n    async_connect('wss://example.invalid')\n"
        "def unrelated_dispatch():\n    unrelated.client.connect('safe')\n",
        encoding="utf-8",
    )
    result = _run(tmp_path, _write_config(tmp_path))
    assert result.returncode == 1
    assert "AE002 backend/app/websocket_paths.py::sync_dispatch" in result.stdout
    assert "AE002 backend/app/websocket_paths.py::async_dispatch" in result.stdout
    assert "websocket_paths.py::unrelated_dispatch" not in result.stdout


def test_ae002_ambiguous_exact_owner_fails_closed(tmp_path: Path) -> None:
    source = tmp_path / "backend/app/owner.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import httpx\n"
        "def accepted():\n    httpx.get('https://example.invalid')\n"
        "class Wrapper:\n    def accepted(self):\n        httpx.get('https://example.invalid')\n",
        encoding="utf-8",
    )
    config = _write_config(tmp_path, [_exception("AE002", "backend/app/owner.py::accepted")])
    result = _run(tmp_path, config)
    assert result.returncode == 2
    assert "ambiguous exception symbol" in result.stderr


def test_ae003_preserves_exact_debt_and_rejects_new_canonical_sql(tmp_path: Path) -> None:
    debt = tmp_path / "backend/app/modules/modeling/service.py"
    debt.parent.mkdir(parents=True)
    debt.write_text(
        "def create_model_spec(c):\n    c.execute('INSERT INTO model_specs (id) VALUES (?)')\n",
        encoding="utf-8",
    )
    bad = tmp_path / "backend/app/modules/other/service.py"
    bad.parent.mkdir(parents=True)
    bad.write_text(
        "def mutate(c):\n    c.execute('UPDATE requirements SET statement = ?')\n",
        encoding="utf-8",
    )
    config = _write_config(
        tmp_path,
        [_exception("AE003", "backend/app/modules/modeling/service.py::create_model_spec", "127")],
    )
    result = _run(tmp_path, config)
    assert result.returncode == 1
    assert "AE003 backend/app/modules/other/service.py::mutate" in result.stdout
    assert "modeling/service.py::create_model_spec" not in result.stdout


def test_ae003_exact_exception_does_not_cover_sibling_symbol(tmp_path: Path) -> None:
    source = tmp_path / "backend/app/modules/runner/service.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def create_runner_job(c):\n    c.execute('INSERT INTO simulation_runs (id) VALUES (?)')\n"
        "def unexpected_owner(c):\n    c.execute('UPDATE simulation_runs SET status = ?')\n",
        encoding="utf-8",
    )
    config = _write_config(
        tmp_path,
        [_exception("AE003", "backend/app/modules/runner/service.py::create_runner_job", "runner lifecycle")],
    )
    result = _run(tmp_path, config)
    assert result.returncode == 1
    assert "AE003 backend/app/modules/runner/service.py::unexpected_owner" in result.stdout
    assert "runner/service.py::create_runner_job" not in result.stdout


def test_ae003_ignores_sql_looking_text_passed_to_non_sql_call(tmp_path: Path) -> None:
    source = tmp_path / "scripts/check_architecture_enforcement.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "from pathlib import Path\n"
        "def _self_test(path: Path):\n"
        "    path.write_text(\"c.execute('UPDATE requirements SET statement = ?')\")\n",
        encoding="utf-8",
    )
    config = _write_config(tmp_path)
    result = _run(tmp_path, config)
    assert result.returncode == 0, result.stdout + result.stderr


def test_ae004_inert_text_passes_but_issue_comment_mutator_fails(tmp_path: Path) -> None:
    script = tmp_path / "scripts/note.py"
    script.parent.mkdir(parents=True)
    script.write_text("TEXT='JARVIS_COORD_V2 WORKPACK only'\n", encoding="utf-8")
    workflow = tmp_path / ".github/workflows/apply.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: apply\non:\n  issue_comment:\njobs: {}\n", encoding="utf-8")
    config = _write_config(tmp_path)
    result = _run(tmp_path, config)
    assert result.returncode == 1
    assert "AE004 .github/workflows/apply.yml::on.issue_comment" in result.stdout
    assert "scripts/note.py" not in result.stdout


def test_config_fails_closed_on_wildcard_unknown_duplicate_and_stale_target(tmp_path: Path) -> None:
    target = tmp_path / "backend/app/x.py"
    target.parent.mkdir(parents=True)
    target.write_text("def f():\n    pass\n", encoding="utf-8")
    cases = [
        [_exception("AE001", "backend/app/*::f")],
        [_exception("AE999", "backend/app/x.py::f")],
        [_exception("AE001", "backend/app/x.py::f"), _exception("AE001", "backend/app/x.py::f")],
        [_exception("AE001", "backend/app/x.py::missing")],
    ]
    for entries in cases:
        result = _run(tmp_path, _write_config(tmp_path, entries))
        assert result.returncode == 2
        assert "ARCH_CONFIG_ERROR" in result.stderr


def test_diagnostics_are_stable(tmp_path: Path) -> None:
    source = tmp_path / "backend/app/x.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "import sqlite3 as s\nimport httpx as h\ndef z():\n"
        "    s.connect('x')\n    h.get('https://example.invalid')\n",
        encoding="utf-8",
    )
    config = _write_config(tmp_path)
    first = _run(tmp_path, config)
    second = _run(tmp_path, config)
    assert first.returncode == second.returncode == 1
    assert first.stdout == second.stdout
