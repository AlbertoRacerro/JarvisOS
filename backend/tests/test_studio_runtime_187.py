"""Real sandbox qualification, intentionally never skipped on the WSL2 host."""

import os
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.paths import build_paths
from app.modules.studio_runtime import runtime


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    if os.getenv("JARVISOS_STUDIO_CI_SANDBOX_UNAVAILABLE") == "1":
        pytest.skip("CI runner does not permit bubblewrap user namespaces; WSL2 qualification is required")
    monkeypatch.setenv("JARVISOS_STUDIO_USER_CODE_ENABLED", "1")
    get_settings.cache_clear()
    assert runtime.status()["available"], "qualification host must run bubblewrap"
    yield
    get_settings.cache_clear()


def execute(body: str, **kwargs):
    return runtime.run(runtime.RunRequest(source="def main(inputs):\n" + body, **kwargs))


def test_scientific_stack_and_typed_result():
    run = execute(" import numpy as np\n import scipy.special as sp\n import pint\n return {'value': float(np.sum([1,2])) + sp.gamma(3), 'unit': str(pint.UnitRegistry().meter)}")
    assert run.classification == "ok", run.stderr
    assert run.result == {"value": 5.0, "unit": "meter"}
    assert len(run.source_sha256) == 64


def test_network_isolated_for_public_and_host_loopback():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    try:
        run = execute(" import socket\n results=[]\n for address in [('1.1.1.1',443),('127.0.0.1',inputs['port'])]:\n  try:\n   socket.create_connection(address, timeout=.2)\n   results.append('connected')\n  except OSError:\n   results.append('blocked')\n return results", inputs={"port": listener.getsockname()[1]})
        assert run.classification == "ok" and run.result == ["blocked", "blocked"]
    finally:
        listener.close()


def test_host_paths_and_environment_hidden(tmp_path, monkeypatch):
    repository = Path(__file__).resolve().parents[2]
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "data-root"))
    get_settings.cache_clear()
    data_root = build_paths().data_root
    data_root.mkdir()
    with tempfile.TemporaryDirectory(dir=Path.home()) as home_dir, tempfile.TemporaryDirectory(dir=repository) as repo_dir:
        canaries = [Path(home_dir) / "canary", Path(repo_dir) / "canary", data_root / "canary"]
        for canary in canaries:
            canary.write_text("secret")
        monkeypatch.setenv("STUDIO_HOST_SECRET", "never-expose")
        run = execute(" import os\n from pathlib import Path\n return {'visible': [Path(p).exists() for p in inputs['paths']], 'secret': os.getenv('STUDIO_HOST_SECRET'), 'write': Path(inputs['paths'][0]).parent.exists()}", inputs={"paths": list(map(str, canaries))})
        assert run.classification == "ok" and run.result == {"visible": [False] * 3, "secret": None, "write": False}


def test_write_outside_scratch_and_python_environment_fails(tmp_path):
    target = tmp_path / "escape"
    run = execute(" import sys\n from pathlib import Path\n paths=[inputs['target'],str(Path(sys.prefix)/'escape'),'/tmp/local-file']\n out=[]\n for name in paths:\n  try:\n   Path(name).write_text('x')\n   out.append(True)\n  except OSError:\n   out.append(False)\n return out", inputs={"target": str(target)})
    assert run.classification == "ok" and run.result == [False, False, True]
    assert not target.exists()


def test_process_namespace_cannot_see_host_pid():
    host = subprocess.Popen(["sleep", "5"])
    try:
        run = execute(" import os, signal\n visible=os.path.exists('/proc/' + str(inputs['pid']))\n try:\n  os.kill(inputs['pid'], signal.SIGTERM)\n  signaled=True\n except OSError:\n  signaled=False\n return {'visible':visible,'signaled':signaled}", inputs={"pid": host.pid})
        assert run.classification == "ok" and run.result == {"visible": False, "signaled": False}
        assert host.poll() is None
    finally:
        host.terminate()
        host.wait()


def test_timeout_kills_sandbox():
    run = execute(" while True: pass", limits=runtime.Limits(wall_seconds=1, cpu_seconds=8))
    assert run.classification == "timeout"


def test_memory_pressure_does_not_kill_backend():
    run = execute(" x=bytearray(2_000_000_000)\n return len(x)", limits=runtime.Limits(memory_bytes=256 * 1024 * 1024))
    assert run.classification in {"error", "limit"}


def test_process_cap():
    run = execute(" import subprocess\n children=[]\n try:\n  for _ in range(100):\n   children.append(subprocess.Popen(['sleep','2']))\n except OSError:\n  pass\n for p in children: p.terminate()\n return len(children)", limits=runtime.Limits(processes=4, wall_seconds=4))
    assert run.classification == "ok" and run.result < 100


def test_output_caps_and_invalid_results():
    flood = execute(" print('x'*200_000)\n return {'ok':True}")
    assert len(flood.stdout.encode()) <= runtime.MAX_LOG_BYTES
    huge = execute(" return 'x'*100_000")
    assert huge.classification == "invalid_result"
    non_finite = execute(" return float('nan')")
    assert non_finite.classification == "invalid_result"
    missing = execute(" import os\n os._exit(0)")
    assert missing.classification == "invalid_result"
    deeply_nested = execute(" import os\n from pathlib import Path\n Path('/work/result.json').write_text('['*2000+'0'+']'*2000)\n os._exit(0)")
    assert deeply_nested.classification == "invalid_result"


def test_scratch_aggregate_is_capped():
    run = execute(" from pathlib import Path\n count=0\n try:\n  for n in range(100):\n   (Path('/work') / str(n)).write_bytes(b'x'*100_000)\n   count+=1\n except OSError:\n  pass\n return count")
    assert run.classification == "ok" and run.result < 100


def test_disabled_and_missing_bwrap_fail_closed(monkeypatch, tmp_path):
    marker = tmp_path / "never-created"
    source = f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\ndef main(inputs): return 1"
    monkeypatch.setenv("JARVISOS_STUDIO_USER_CODE_ENABLED", "0")
    get_settings.cache_clear()
    assert runtime.run(runtime.RunRequest(source=source)).classification == "disabled"
    monkeypatch.setenv("JARVISOS_STUDIO_USER_CODE_ENABLED", "1")
    get_settings.cache_clear()
    monkeypatch.setattr(runtime.shutil, "which", lambda _: None)
    assert runtime.status()["reason"] == "bubblewrap_or_python_unavailable"
    assert runtime.run(runtime.RunRequest(source=source)).classification == "unavailable"
    assert not marker.exists()


def test_result_symlink_is_rejected(tmp_path):
    source = "import os\nos.symlink('/etc/passwd','/work/result.json')\ndef main(inputs): return 1"
    run = runtime.run(runtime.RunRequest(source=source))
    assert run.classification == "error"


def test_backend_only_passes_source_to_sandbox_launcher(monkeypatch, tmp_path):
    marker = tmp_path / "host-execution"
    source = f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\ndef main(inputs): return 1"
    launches = []
    monkeypatch.setattr(runtime, "_launch", lambda args, scratch, limits: (launches.append(args), (0, False))[1])
    run = runtime.run(runtime.RunRequest(source=source))
    assert launches and run.classification == "invalid_result"
    assert not marker.exists()


def test_explicit_route_and_origin_gate():
    from app.main import create_app

    client = TestClient(create_app())
    assert client.get("/studio/runtime/status").json()["available"] is True
    payload = {"source": "def main(inputs): return {'answer': 42}"}
    refused = client.post("/studio/runtime/runs", json=payload, headers={"Origin": "https://evil.invalid"})
    assert refused.status_code == 403
    accepted = client.post("/studio/runtime/runs", json=payload)
    assert accepted.status_code == 200 and accepted.json()["result"] == {"answer": 42}
