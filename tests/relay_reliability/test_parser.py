"""Bounded regression coverage for successful streams with large token fields."""

import hashlib
import random
import runpy
import subprocess
import sys
from pathlib import Path

import pytest
from agent_relay import run_errors

ROOT = Path(__file__).resolve().parents[2]
REPAIR = runpy.run_path(str(ROOT / "scripts/relay_safe.py"))


@pytest.fixture(autouse=True, scope="module")
def apply_repair():
    REPAIR["apply_repair"]()


def user_facing_run_error(*args):
    return run_errors.user_facing_run_error(*args)


@pytest.mark.parametrize(
    "message",
    [
        "The gpt-5.3-codex-spark model is not supported for this account",
        "'gpt-5.3-codex-spark' model is not supported for this account",
        '"gpt-5.3-codex-spark" model is not supported for this account',
        "The\t\n'gpt-5.3-codex-spark'\tmodel\nis\tnot supported for this account",
        "prefix: gpt-5.3-codex-spark model is not supported for this account",
    ],
)
def test_unsupported_model_message_still_recognized(message):
    assert user_facing_run_error("codex", message) == (
        "GPT-5.3 Codex Spark isn't available for this Codex account. "
        "Choose another model and try again."
    )


@pytest.mark.parametrize("shape", ["token", "whitespace", "quotes", "prefixes", "sandbox"])
def test_large_successful_stream_finishes_within_bounded_timeout(shape):
    # Isolate the regex in an owned child: a regression cannot wedge pytest's
    # own GIL or prevent its timeout machinery from receiving Python signals.
    script = r'''
import json
import sys
import runpy
runpy.run_path("scripts/relay_safe.py")["apply_repair"]()
from agent_relay.run_errors import user_facing_run_error
n = 1_000_000
shape = sys.argv[1]
value = {
    "token": "a" * n,
    "whitespace": "a" + " " * n + "end",
    "quotes": '"' * n,
    "prefixes": "the " * (n // 4) + "end",
    "sandbox": "sandbox " * (n // 8),
}[shape]
stream = json.dumps({"type": "assistant", "message": {
    "content": [{"type": "image", "source": {"type": "base64", "data": value}}]
}})
assert user_facing_run_error("claude", stream) is None
'''
    completed = subprocess.run(
        [sys.executable, "-c", script, shape],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize("separator", [" ", "\t", "\r", "\v", "\f", "\u2028"])
@pytest.mark.parametrize("words", [("sandbox", "denied"), ("ſandbox", "denıed")])
def test_sandbox_permission_detection_preserves_regex_semantics(separator, words):
    first, second = words
    assert user_facing_run_error("claude", first + separator + second) == (
        "Claude Code was blocked by a permission setting. Review /permissions and try again."
    )
    assert user_facing_run_error("claude", second + separator + first) is None
    assert user_facing_run_error("claude", first + "\n" + second) is None


def test_unrecognized_upstream_source_refused():
    source = Path(run_errors.__file__).read_bytes()
    with pytest.raises(RuntimeError, match="differs from audited"):
        REPAIR["patched_source"](source + b"\n")


def test_installation_source_unchanged():
    path = Path(run_errors.__file__)
    before = path.read_bytes()
    REPAIR["apply_repair"]()
    assert path.read_bytes() == before
    assert hashlib.sha256(before).hexdigest() == REPAIR["SOURCE_SHA256"]


def test_original_and_repaired_public_outputs_match():
    namespace = {}
    source = Path(run_errors.__file__).read_bytes()
    # Execute only the hash-verified installed source, preserving an original oracle.
    exec(compile(source, "original-run-errors", "exec"), namespace)  # noqa: S102
    original = namespace["user_facing_run_error"]
    parts = [
        "sandbox", "denied", "permission denied", "operation not permitted",
        "the", "a", "b", "'", '"', " ", "\t", "\n", "\r", "ſandbox", "denıed",
        "account", " model is not supported",
    ]
    rng = random.Random(847)
    for _ in range(2000):
        case = "".join(rng.choice(parts) for _ in range(rng.randrange(20)))
        assert user_facing_run_error("claude", case) == original("claude", case)
