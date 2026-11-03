"""Jarvis-owned sandbox for Relay-launched coding agents (spec 157).

Relay starts its worker as ``/bin/sh -c "cd <workspace> && claude ..."`` and resolves
the agent by name on ``PATH``. Jarvis puts a shim named after each agent first on the
``PATH`` of the Relay processes it launches; the shim runs this file. The worker then
executes inside bubblewrap with fresh namespaces, a cleared environment and only the
mounts listed in a Jarvis-written manifest. The only network path is a CONNECT proxy
running here, outside the sandbox, that allows configured public hosts.

Standard library only: this file runs as the shim, outside the backend virtualenv.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

MANIFEST_VERSION = 1
SANDBOX_HOME = "/home/agent"
SANDBOX_CONTEXT = "/workspace/context"
SANDBOX_RUNTIME = "/run/jarvis"
PROXY_PORT = 3128
MANIFEST_MISSING_EXIT = 126
_MAX_REQUEST_HEAD = 8192
_SYSTEM_ETC = (
    "/etc/ssl",
    "/etc/ca-certificates",
    "/etc/alternatives",
    "/etc/passwd",
    "/etc/group",
    "/etc/nsswitch.conf",
    "/etc/localtime",
    "/etc/gitconfig",
    "/etc/ld.so.cache",
)


class ManifestError(RuntimeError):
    pass


def manifest_name(workspace: str) -> str:
    return hashlib.sha256(os.path.realpath(workspace).encode("utf-8")).hexdigest()[:32] + ".json"


def load_manifest(manifest_dir: str, workspace: str, agent: str) -> dict[str, Any]:
    """Return the manifest Jarvis wrote for this exact workspace, or fail closed."""
    path = Path(manifest_dir) / manifest_name(workspace)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise ManifestError("no Jarvis sandbox manifest for this workspace") from exc
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise ManifestError("sandbox manifest ownership or mode is unsafe")
        manifest = json.loads(handle.read(1 << 20))
    if manifest.get("version") != MANIFEST_VERSION:
        raise ManifestError("unsupported sandbox manifest version")
    if manifest.get("workspace") != os.path.realpath(workspace):
        raise ManifestError("sandbox manifest does not match the workspace")
    if agent not in manifest.get("agents", {}):
        raise ManifestError(f"agent {agent!r} is not admitted for this workspace")
    return manifest


def build_bwrap_args(manifest: dict[str, Any], agent: str, runtime_dir: str, argv: list[str]) -> list[str]:
    """Pure construction of the bubblewrap command line for one worker invocation."""
    spec = manifest["agents"][agent]
    workspace = manifest["workspace"]
    args = [
        manifest.get("bwrap", "/usr/bin/bwrap"),
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/sbin", "/sbin",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--tmpfs", "/run",
        "--dir", "/etc",
    ]
    for path in _SYSTEM_ETC:
        if os.path.exists(path):
            args += ["--ro-bind", path, path]
    for root in spec.get("tool_roots", []):
        args += ["--ro-bind", root, root]
    args += ["--bind", manifest["home"], SANDBOX_HOME]
    for credential in spec.get("credentials", []):
        target = f"{SANDBOX_HOME}/{credential['target']}"
        args += ["--bind", credential["source"], target]
    args += ["--bind", workspace, workspace]
    if manifest.get("git_config"):
        # Host-side git (Relay's and Jarvis's) later runs in this repository; the agent must not
        # be able to plant config (core.fsmonitor, filters, includes) or hooks that it executes.
        args += ["--ro-bind", manifest["git_config"], f"{workspace}/.git/config"]
        args += ["--tmpfs", f"{workspace}/.git/hooks"]
    for masked in manifest.get("masked", []):
        args += ["--tmpfs", f"{workspace}/{masked}"]
    for mount in manifest.get("dependency_mounts", []):
        args += ["--ro-bind", mount["source"], f"{workspace}/{mount['target']}"]
    args += ["--ro-bind", manifest["context_dir"], SANDBOX_CONTEXT]
    args += ["--bind", os.path.join(runtime_dir, "sock"), f"{SANDBOX_RUNTIME}/egress"]
    args += ["--ro-bind", os.path.abspath(__file__), f"{SANDBOX_RUNTIME}/sandbox.py"]
    proxy = f"http://127.0.0.1:{PROXY_PORT}"
    path_entries = [*spec.get("path_prefix", []), "/usr/local/bin", "/usr/bin", "/bin"]
    environment = {
        "HOME": SANDBOX_HOME,
        "PATH": ":".join(path_entries),
        "LANG": "C.UTF-8",
        "TERM": "xterm-256color",
        "HTTPS_PROXY": proxy,
        "HTTP_PROXY": proxy,
        "https_proxy": proxy,
        "http_proxy": proxy,
        "NO_PROXY": "localhost,127.0.0.1",
        "no_proxy": "localhost,127.0.0.1",
        "JARVIS_RELAY_CONTEXT": SANDBOX_CONTEXT,
        "GIT_AUTHOR_NAME": "Jarvis Relay Agent",
        "GIT_AUTHOR_EMAIL": "relay-agent@jarvisos.invalid",
        "GIT_COMMITTER_NAME": "Jarvis Relay Agent",
        "GIT_COMMITTER_EMAIL": "relay-agent@jarvisos.invalid",
        **spec.get("environment", {}),
    }
    for key, value in sorted(environment.items()):
        args += ["--setenv", key, value]
    # Relay passes no permission flag; the sandbox is the boundary, so the manifest may add the
    # agent's own non-interactive permission options here, for Jarvis-launched runs only.
    args += ["--chdir", workspace, "/usr/bin/python3", f"{SANDBOX_RUNTIME}/sandbox.py", "inner", "--",
             spec["binary"], *spec.get("sandbox_args", []), *argv]
    return args


# ---- egress proxy (outside the sandbox) -------------------------------------------------


def host_allowed(host: str, allow: list[str]) -> bool:
    host = host.lower().rstrip(".")
    for pattern in allow:
        pattern = pattern.lower()
        if pattern.startswith("*.") and host.endswith(pattern[1:]):
            return True
        if host == pattern:
            return True
    return False


def resolve_public(host: str, port: int) -> tuple[str, int]:
    """Resolve and refuse any non-global address so allowed names cannot reach local services."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise PermissionError("literal IP destinations are not allowed")
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses = [str(info[4][0]) for info in infos]
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise PermissionError("destination resolves to a non-public address")
    return addresses[0], port


def _pipe(source: socket.socket, sink: socket.socket) -> None:
    try:
        while data := source.recv(65536):
            sink.sendall(data)
    except OSError:
        pass
    finally:
        for end in (source, sink):
            try:
                end.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class EgressProxy:
    def __init__(self, socket_path: str, allow: list[str], log_path: str) -> None:
        self.allow = allow
        self.log_path = log_path
        self._lock = threading.Lock()
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(socket_path)
        os.chmod(socket_path, 0o600)
        self.server.listen(64)

    def serve_forever(self) -> None:
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _log(self, host: str, port: int, decision: str) -> None:
        line = json.dumps({"at": time.time(), "host": host, "port": port, "decision": decision})
        with self._lock, open(self.log_path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def _handle(self, client: socket.socket) -> None:
        head = b""
        try:
            while b"\r\n\r\n" not in head:
                chunk = client.recv(4096)
                if not chunk or len(head) + len(chunk) > _MAX_REQUEST_HEAD:
                    client.close()
                    return
                head += chunk
            method, target, _ = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ", 2)
            host, _, port_text = target.rpartition(":")
            host = host.strip("[]")
            port = int(port_text)
            if method != "CONNECT" or port != 443 or not host_allowed(host, self.allow):
                self._log(host, port, "denied")
                client.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                client.close()
                return
            upstream = socket.create_connection(resolve_public(host, port), timeout=30)
            upstream.settimeout(None)
        except (OSError, ValueError, PermissionError):
            try:
                client.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            except OSError:
                pass
            client.close()
            return
        self._log(host, port, "allowed")
        client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        threading.Thread(target=_pipe, args=(client, upstream), daemon=True).start()
        _pipe(upstream, client)


# ---- git metadata trust boundary ---------------------------------------------------------


def _remove(path: str) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    elif os.path.lexists(path):
        os.unlink(path)


def restore_workspace_git(workspace: str, trusted_config: str) -> None:
    """Undo agent changes to git metadata that host-side git would read or execute.

    Runs on the host after the sandbox exits, before Relay or Jarvis runs git in the workspace:
    the trusted config replaces ``.git/config``, hooks are emptied, gitdir redirects are
    removed, and nested repositories (whose own config a recursing ``git status`` would read)
    are deleted. A ``.git`` that is not a real directory is removed, so git finds no repository.
    """
    root = os.path.join(workspace, ".git")
    if os.path.islink(root) or (os.path.lexists(root) and not os.path.isdir(root)):
        os.unlink(root)
        return
    if not os.path.isdir(root):
        return
    with open(trusted_config, "rb") as handle:
        trusted = handle.read()
    config = os.path.join(root, "config")
    current = None
    if os.path.isfile(config) and not os.path.islink(config):
        with open(config, "rb") as handle:
            current = handle.read()
    if current != trusted:
        _remove(config)
        descriptor = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(trusted)
    for name in ("commondir", "config.worktree", "hooks"):
        _remove(os.path.join(root, name))
    os.mkdir(os.path.join(root, "hooks"), 0o700)
    for directory, dirnames, filenames in os.walk(workspace):
        if directory == workspace:
            dirnames[:] = [name for name in dirnames if name != ".git"]
            continue
        if ".git" in dirnames or ".git" in filenames:
            _remove(os.path.join(directory, ".git"))
            dirnames[:] = [name for name in dirnames if name != ".git"]


# ---- entry points ---------------------------------------------------------------------


def run_shim(agent: str, manifest_dir: str, argv: list[str]) -> int:
    try:
        manifest = load_manifest(manifest_dir, os.getcwd(), agent)
    except (ManifestError, ValueError) as exc:
        print(f"jarvis-sandbox: refused: {exc}", file=sys.stderr)
        return MANIFEST_MISSING_EXIT
    runs_dir = Path(manifest["runtime_root"])
    runs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime_dir = tempfile.mkdtemp(prefix="run-", dir=runs_dir)
    os.mkdir(os.path.join(runtime_dir, "sock"), 0o700)
    socket_path = os.path.join(runtime_dir, "sock", "egress.sock")
    proxy = EgressProxy(socket_path, manifest["egress_allow"],
                        os.path.join(runtime_dir, "egress.jsonl"))
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    try:
        return subprocess.call(build_bwrap_args(manifest, agent, runtime_dir, argv))
    finally:
        if manifest.get("git_config"):
            restore_workspace_git(manifest["workspace"], manifest["git_config"])
        proxy.server.close()
        try:
            os.unlink(socket_path)
        except OSError:
            pass


def _forward(listener: socket.socket) -> None:
    while True:
        client, _ = listener.accept()
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            upstream.connect(f"{SANDBOX_RUNTIME}/egress/egress.sock")
        except OSError:
            client.close()
            continue
        threading.Thread(target=_pipe, args=(client, upstream), daemon=True).start()
        threading.Thread(target=_pipe, args=(upstream, client), daemon=True).start()


def run_inner(argv: list[str]) -> int:
    """Inside the sandbox: bridge loopback to the proxy socket, then become the agent."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", PROXY_PORT))
    listener.listen(64)
    if os.fork() == 0:
        try:
            _forward(listener)
        finally:
            os._exit(0)
    listener.close()
    os.execv(argv[0], argv)
    return 127


def main(args: list[str]) -> int:
    if len(args) >= 3 and args[0] == "shim":
        agent, manifest_dir, rest = args[1], args[2], args[3:]
        if rest[:1] == ["--"]:
            rest = rest[1:]
        return run_shim(agent, manifest_dir, rest)
    if len(args) >= 2 and args[0] == "inner" and args[1] == "--":
        return run_inner(args[2:])
    print("usage: sandbox.py shim <agent> <manifest-dir> -- <args> | inner -- <argv>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
