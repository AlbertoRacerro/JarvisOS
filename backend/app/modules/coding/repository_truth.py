from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import json
import logging
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote, unquote

from app.modules.secrets.storage import resolve_secret_ref

API_HOST = "api.github.com"
# Optional read-only credential for private repositories (fine-grained token: Contents, Pull requests,
# Checks and Metadata read on the configured repository only). Unset keeps the public-only behaviour.
GITHUB_READ_TOKEN_REF = "env:JARVISOS_GITHUB_READ_TOKEN"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]{20,255}$")
NAV_HOST = "github.com"
PROVIDER = "github"

MAX_FILE_PREVIEW_BYTES = 262_144
MAX_AGGREGATE_BYTES = 1_048_576
MAX_TREE_ENTRIES = 1_000
MAX_SEARCH_CANDIDATES = 100
MAX_SEARCH_MATCHES = 100
MAX_COMPARE_FILES = 100
MAX_COLLECTION_ITEMS = 100
MAX_COMMENT_BODY_CHARS = 8_192
MAX_PAGES = 10
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 10.0
MAX_TRANSIENT_RETRIES = 1

_ALLOWED_OPERATIONS = frozenset(
    {
        "repository_ref_truth",
        "commit_truth",
        "path_list",
        "file_preview",
        "literal_search",
        "compare_truth",
        "pull_request_truth",
        "check_truth",
        "review_truth",
        "pull_request_comments_truth",
        "safe_github_url",
    }
)
_MUTATION_WORDS = frozenset(
    {
        "create",
        "update",
        "delete",
        "write",
        "merge",
        "dispatch",
        "label",
        "secret",
        "release",
        "branch",
        "commit_create",
        "review_create",
    }
)
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_REF_RE = re.compile(r"^[A-Za-z0-9._/@+-]{1,255}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
logger = logging.getLogger(__name__)

FailureCode = Literal[
    "unauthorized_repository",
    "unsupported_provider",
    "unsupported_host",
    "unsupported_operation",
    "invalid_ref",
    "invalid_path",
    "not_found",
    "authentication_required",
    "stale_ref",
    "rate_limited",
    "provider_unavailable",
    "timeout",
    "malformed_provider_response",
    "redirect_forbidden",
    "unsupported_content",
    "oversized",
    "partial",
]


class RepositoryTruthError(ValueError):
    def __init__(
        self,
        code: FailureCode,
        message: str,
        *,
        partial: bool = False,
        metadata: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.partial = partial
        self.metadata = metadata or {}


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


Transport = Callable[[str], HttpResponse]


@dataclass(frozen=True)
class RepositoryTruthResult:
    provider: str
    repository: str
    operation: str
    requested_ref: str | None
    resolved_sha: str | None
    partial: bool
    payload: dict[str, object]
    observed_at: str


@dataclass
class _Budget:
    bytes_seen: int = 0

    def consume(self, body: bytes) -> None:
        self.bytes_seen += len(body)
        if self.bytes_seen > MAX_AGGREGATE_BYTES:
            raise RepositoryTruthError(
                "partial",
                "aggregate provider payload bound reached",
                partial=True,
                metadata={"bytes_seen": self.bytes_seen, "limit": MAX_AGGREGATE_BYTES},
            )


def _github_read_token() -> str | None:
    """Optional read-only token for a private repository, via the existing secret boundary.

    Resolved with the same ``env:`` / systemd-credential seam every other provider credential uses; the
    value is only ever placed in the Authorization header of a fixed-host API request and is never logged,
    returned or embedded in a result. Absent or malformed means unauthenticated (public-only) reads.
    """
    try:
        value = resolve_secret_ref(GITHUB_READ_TOKEN_REF).value
    except (ValueError, OSError):
        return None
    if not value:
        return None
    token = value.strip()
    if not _TOKEN_RE.fullmatch(token):
        logger.warning("coding_repository_token_ignored reason=malformed")
        return None
    return token


def _github_request(path: str) -> HttpResponse:
    """Read one fixed-host GitHub API path without following redirects.

    Sends no credentials unless the optional read-only token (``GITHUB_READ_TOKEN_REF``) is configured.
    """
    if not path.startswith("/") or "://" in path or _CONTROL_RE.search(path):
        raise RepositoryTruthError("unsupported_host", "provider path is not a fixed-host API path")
    request_headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "JarvisOS-repository-truth/1",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = _github_read_token()
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    connection = http.client.HTTPSConnection(API_HOST, timeout=CONNECT_TIMEOUT_SECONDS)
    try:
        connection.request("GET", path, headers=request_headers)
        response = connection.getresponse()
        if connection.sock is not None:
            connection.sock.settimeout(READ_TIMEOUT_SECONDS)
        body = response.read(MAX_AGGREGATE_BYTES + 1)
        if len(body) > MAX_AGGREGATE_BYTES:
            raise RepositoryTruthError("oversized", "provider response exceeded aggregate byte bound")
        headers = {key.lower(): value for key, value in response.getheaders()}
        return HttpResponse(status=response.status, headers=headers, body=body)
    except TimeoutError as exc:
        raise RepositoryTruthError("timeout", "GitHub read timed out") from exc
    except (OSError, http.client.HTTPException) as exc:
        raise RepositoryTruthError("provider_unavailable", "GitHub read failed") from exc
    finally:
        connection.close()


def _repo_digest(repository: str) -> str:
    return hashlib.sha256(repository.encode("utf-8")).hexdigest()


def _validate_repository(repository: str) -> str:
    if not _REPOSITORY_RE.fullmatch(repository):
        raise RepositoryTruthError(
            "unauthorized_repository", "repository must be one configured owner/name identity"
        )
    return repository


def _validate_ref(ref: str) -> str:
    if not _REF_RE.fullmatch(ref) or ".." in ref or "://" in ref:
        raise RepositoryTruthError("invalid_ref", "ref is invalid or ambiguous")
    return ref


def _validate_sha(sha: object) -> str:
    if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
        raise RepositoryTruthError("malformed_provider_response", "provider returned invalid commit SHA")
    return sha.lower()


def _validate_path(path: str) -> str:
    if path == "":
        return path
    if path.startswith(("/", "\\")) or "\\" in path or _CONTROL_RE.search(path):
        raise RepositoryTruthError("invalid_path", "repository path is absolute or ambiguous")
    decoded = unquote(path)
    if decoded != path and ("/" in decoded.replace(path, "") or "\\" in decoded):
        raise RepositoryTruthError("invalid_path", "encoded path separators are forbidden")
    if _CONTROL_RE.search(decoded):
        raise RepositoryTruthError("invalid_path", "repository path contains control characters")
    segments = decoded.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise RepositoryTruthError("invalid_path", "repository path contains traversal")
    lowered = path.lower()
    if "%2f" in lowered or "%5c" in lowered or "%2e" in lowered:
        raise RepositoryTruthError("invalid_path", "encoded traversal or separators are forbidden")
    return path


def _validate_operation(operation: str) -> str:
    if operation not in _ALLOWED_OPERATIONS:
        code: FailureCode = "unsupported_operation"
        raise RepositoryTruthError(code, "operation is not in the read-only allowlist")
    if any(word in operation for word in _MUTATION_WORDS):
        raise RepositoryTruthError("unsupported_operation", "mutation operation is forbidden")
    return operation


def _json_object(response: HttpResponse) -> dict[str, Any]:
    try:
        value = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RepositoryTruthError(
            "malformed_provider_response", "provider returned malformed JSON"
        ) from exc
    if not isinstance(value, dict):
        raise RepositoryTruthError(
            "malformed_provider_response", "provider response was not an object"
        )
    return value


def _json_list(response: HttpResponse) -> list[Any]:
    try:
        value = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RepositoryTruthError(
            "malformed_provider_response", "provider returned malformed JSON"
        ) from exc
    if not isinstance(value, list):
        raise RepositoryTruthError(
            "malformed_provider_response", "provider response was not a list"
        )
    return value


def _project_text(value: object, *, max_chars: int = 8_192) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RepositoryTruthError(
            "malformed_provider_response", "provider text field had the wrong type"
        )
    return value[:max_chars]


def _decode_github_base64(encoded: str) -> bytes:
    compact = encoded.replace("\n", "").replace("\r", "")
    try:
        return base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RepositoryTruthError(
            "malformed_provider_response", "file base64 payload is malformed"
        ) from exc


class RepositoryTruthService:
    def __init__(
        self,
        configured_repositories: Iterable[str],
        *,
        transport: Transport = _github_request,
    ) -> None:
        repositories = frozenset(_validate_repository(item) for item in configured_repositories)
        self._repositories = repositories
        self._transport = transport

    def _authorize(self, repository: str) -> str:
        repository = _validate_repository(repository)
        if repository not in self._repositories:
            raise RepositoryTruthError(
                "unauthorized_repository", "repository is not configured for Coding inspection"
            )
        return repository

    def _read(
        self,
        repository: str,
        operation: str,
        path: str,
        *,
        budget: _Budget,
        resolved_sha: str | None = None,
    ) -> HttpResponse:
        attempts = 0
        outcome = "provider_error"
        response_bytes = 0
        try:
            while True:
                try:
                    response = self._transport(path)
                    response_bytes = len(response.body)
                    budget.consume(response.body)
                except RepositoryTruthError as exc:
                    if exc.code in {"timeout", "provider_unavailable"} and attempts < MAX_TRANSIENT_RETRIES:
                        attempts += 1
                        continue
                    outcome = exc.code
                    raise

                if 300 <= response.status < 400:
                    outcome = "validation_error"
                    raise RepositoryTruthError(
                        "redirect_forbidden", "GitHub redirect responses are not followed"
                    )
                if response.status in {502, 503, 504} and attempts < MAX_TRANSIENT_RETRIES:
                    attempts += 1
                    continue
                if response.status in {429} or (
                    response.status == 403
                    and response.headers.get("x-ratelimit-remaining") == "0"
                ):
                    outcome = "rate_limited"
                    raise RepositoryTruthError("rate_limited", "GitHub read is rate limited")
                if response.status in {401, 403}:
                    outcome = "unauthorized"
                    raise RepositoryTruthError(
                        "authentication_required",
                        "GitHub refused the read: no read credential is configured, or it is expired or lacks access",
                    )
                if response.status == 404:
                    outcome = "not_found"
                    raise RepositoryTruthError(
                        "not_found", "repository/ref/path/PR was not found or is private"
                    )
                if response.status >= 500:
                    outcome = "provider_error"
                    raise RepositoryTruthError(
                        "provider_unavailable", "GitHub provider is unavailable"
                    )
                if not 200 <= response.status < 300:
                    outcome = "provider_error"
                    raise RepositoryTruthError(
                        "malformed_provider_response",
                        f"unexpected GitHub HTTP status {response.status}",
                    )
                outcome = "success"
                return response
        finally:
            logger.info(
                "coding_repository_read provider=%s operation=%s repository_digest=%s "
                "resolved_sha=%s outcome=%s response_bytes=%d aggregate_bytes=%d",
                PROVIDER,
                operation,
                _repo_digest(repository),
                resolved_sha or "unknown",
                outcome,
                response_bytes,
                budget.bytes_seen,
            )

    def _resolve_sha(
        self,
        repository: str,
        ref: str,
        operation: str,
        *,
        budget: _Budget,
    ) -> str:
        safe_ref = quote(_validate_ref(ref), safe="")
        response = self._read(
            repository,
            operation,
            f"/repos/{repository}/commits/{safe_ref}",
            budget=budget,
        )
        payload = _json_object(response)
        return _validate_sha(payload.get("sha", ""))

    def _result(
        self,
        repository: str,
        operation: str,
        *,
        requested_ref: str | None,
        resolved_sha: str | None,
        payload: dict[str, object],
        partial: bool = False,
    ) -> RepositoryTruthResult:
        return RepositoryTruthResult(
            provider=PROVIDER,
            repository=repository,
            operation=operation,
            requested_ref=requested_ref,
            resolved_sha=resolved_sha,
            partial=partial,
            payload=payload,
            observed_at=datetime.now(UTC).isoformat(),
        )

    def repository_ref_truth(self, repository: str, ref: str) -> RepositoryTruthResult:
        operation = _validate_operation("repository_ref_truth")
        repository = self._authorize(repository)
        budget = _Budget()
        sha = self._resolve_sha(repository, ref, operation, budget=budget)
        return self._result(
            repository,
            operation,
            requested_ref=ref,
            resolved_sha=sha,
            payload={"sha": sha},
        )

    def commit_truth(self, repository: str, ref: str) -> RepositoryTruthResult:
        operation = _validate_operation("commit_truth")
        repository = self._authorize(repository)
        budget = _Budget()
        sha = self._resolve_sha(repository, ref, operation, budget=budget)
        response = self._read(
            repository,
            operation,
            f"/repos/{repository}/commits/{sha}",
            budget=budget,
            resolved_sha=sha,
        )
        data = _json_object(response)
        commit = data.get("commit")
        if not isinstance(commit, dict):
            raise RepositoryTruthError(
                "malformed_provider_response", "commit payload lacks commit metadata"
            )
        author = commit.get("author")
        if author is not None and not isinstance(author, dict):
            raise RepositoryTruthError(
                "malformed_provider_response", "commit author metadata is malformed"
            )
        return self._result(
            repository,
            operation,
            requested_ref=ref,
            resolved_sha=sha,
            payload={
                "sha": sha,
                "message": _project_text(commit.get("message")),
                "author_name": _project_text(author.get("name")) if author else None,
                "author_date": _project_text(author.get("date")) if author else None,
                "url": self.safe_github_url(repository, commit_sha=sha).payload["url"],
            },
        )

    def path_list(
        self,
        repository: str,
        ref: str,
        path: str = "",
    ) -> RepositoryTruthResult:
        operation = _validate_operation("path_list")
        repository = self._authorize(repository)
        path = _validate_path(path)
        budget = _Budget()
        sha = self._resolve_sha(repository, ref, operation, budget=budget)
        suffix = f"/{quote(path, safe='/')}" if path else ""
        response = self._read(
            repository,
            operation,
            f"/repos/{repository}/contents{suffix}?ref={sha}",
            budget=budget,
            resolved_sha=sha,
        )
        entries = _json_list(response)
        partial = len(entries) >= MAX_TREE_ENTRIES
        projected: list[dict[str, object]] = []
        for entry in entries[:MAX_TREE_ENTRIES]:
            if not isinstance(entry, dict):
                raise RepositoryTruthError(
                    "malformed_provider_response", "directory entry is malformed"
                )
            entry_path = entry.get("path")
            entry_type = entry.get("type")
            if not isinstance(entry_path, str) or not isinstance(entry_type, str):
                raise RepositoryTruthError(
                    "malformed_provider_response", "directory entry lacks path/type"
                )
            _validate_path(entry_path)
            projected.append(
                {
                    "path": entry_path,
                    "type": entry_type,
                    "sha": _project_text(entry.get("sha"), max_chars=64),
                    "size": entry.get("size") if isinstance(entry.get("size"), int) else None,
                }
            )
        return self._result(
            repository,
            operation,
            requested_ref=ref,
            resolved_sha=sha,
            payload={
                "path": path,
                "entries": projected,
                "limit": MAX_TREE_ENTRIES,
            },
            partial=partial,
        )

    def file_preview(
        self,
        repository: str,
        ref: str,
        path: str,
    ) -> RepositoryTruthResult:
        operation = _validate_operation("file_preview")
        repository = self._authorize(repository)
        path = _validate_path(path)
        budget = _Budget()
        sha = self._resolve_sha(repository, ref, operation, budget=budget)
        response = self._read(
            repository,
            operation,
            f"/repos/{repository}/contents/{quote(path, safe='/')}?ref={sha}",
            budget=budget,
            resolved_sha=sha,
        )
        data = _json_object(response)
        if data.get("type") != "file" or data.get("encoding") != "base64":
            raise RepositoryTruthError(
                "unsupported_content", "path is not a directly previewable regular file"
            )
        encoded = data.get("content")
        if not isinstance(encoded, str):
            raise RepositoryTruthError(
                "malformed_provider_response", "file content is missing"
            )
        raw = _decode_github_base64(encoded)
        if len(raw) > MAX_FILE_PREVIEW_BYTES:
            raise RepositoryTruthError(
                "oversized",
                "decoded file exceeds preview byte bound",
                metadata={"size": len(raw), "limit": MAX_FILE_PREVIEW_BYTES},
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RepositoryTruthError(
                "unsupported_content", "file is binary or invalid UTF-8"
            ) from exc
        return self._result(
            repository,
            operation,
            requested_ref=ref,
            resolved_sha=sha,
            payload={
                "path": path,
                "blob_sha": _project_text(data.get("sha"), max_chars=64),
                "size": len(raw),
                "text": text,
            },
        )

    def literal_search(
        self,
        repository: str,
        ref: str,
        literal: str,
    ) -> RepositoryTruthResult:
        operation = _validate_operation("literal_search")
        repository = self._authorize(repository)
        if not literal or len(literal) > 512 or _CONTROL_RE.search(literal):
            raise RepositoryTruthError("invalid_path", "literal query is invalid")
        budget = _Budget()
        sha = self._resolve_sha(repository, ref, operation, budget=budget)
        tree_response = self._read(
            repository,
            operation,
            f"/repos/{repository}/git/trees/{sha}?recursive=1",
            budget=budget,
            resolved_sha=sha,
        )
        tree_data = _json_object(tree_response)
        tree = tree_data.get("tree")
        if not isinstance(tree, list):
            raise RepositoryTruthError(
                "malformed_provider_response", "tree response lacks entries"
            )
        partial = bool(tree_data.get("truncated")) or len(tree) > MAX_TREE_ENTRIES
        candidates: list[tuple[str, str]] = []
        for entry in tree[:MAX_TREE_ENTRIES]:
            if not isinstance(entry, dict):
                raise RepositoryTruthError(
                    "malformed_provider_response", "tree entry is malformed"
                )
            if entry.get("type") != "blob":
                continue
            path = entry.get("path")
            blob_sha = entry.get("sha")
            if not isinstance(path, str) or not isinstance(blob_sha, str):
                raise RepositoryTruthError(
                    "malformed_provider_response", "tree blob lacks path/SHA"
                )
            _validate_path(path)
            size = entry.get("size")
            if isinstance(size, int) and size > MAX_FILE_PREVIEW_BYTES:
                partial = True
                continue
            if len(candidates) >= MAX_SEARCH_CANDIDATES:
                partial = True
                break
            candidates.append((path, _validate_sha(blob_sha)))

        matches: list[dict[str, object]] = []
        examined = 0
        for path, blob_sha in candidates:
            if len(matches) >= MAX_SEARCH_MATCHES:
                partial = True
                break
            try:
                blob_response = self._read(
                    repository,
                    operation,
                    f"/repos/{repository}/git/blobs/{quote(blob_sha, safe='')}",
                    budget=budget,
                    resolved_sha=sha,
                )
            except RepositoryTruthError as exc:
                if exc.code == "partial":
                    partial = True
                    break
                raise
            blob = _json_object(blob_response)
            encoded = blob.get("content")
            if blob.get("encoding") != "base64":
                partial = True
                continue
            if not isinstance(encoded, str):
                raise RepositoryTruthError(
                    "malformed_provider_response", "blob content is missing"
                )
            raw = _decode_github_base64(encoded)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if len(raw) > MAX_FILE_PREVIEW_BYTES:
                partial = True
                continue
            examined += 1
            start = 0
            while len(matches) < MAX_SEARCH_MATCHES:
                index = text.find(literal, start)
                if index < 0:
                    break
                line = text.count("\n", 0, index) + 1
                matches.append({"path": path, "line": line, "offset": index})
                start = index + max(1, len(literal))
            if len(matches) >= MAX_SEARCH_MATCHES:
                partial = True
                break
        return self._result(
            repository,
            operation,
            requested_ref=ref,
            resolved_sha=sha,
            payload={
                "query_digest": hashlib.sha256(literal.encode("utf-8")).hexdigest(),
                "matches": matches,
                "candidate_files_examined": examined,
                "candidate_limit": MAX_SEARCH_CANDIDATES,
                "match_limit": MAX_SEARCH_MATCHES,
            },
            partial=partial,
        )

    def compare_truth(
        self,
        repository: str,
        base_ref: str,
        head_ref: str,
    ) -> RepositoryTruthResult:
        operation = _validate_operation("compare_truth")
        repository = self._authorize(repository)
        budget = _Budget()
        base_sha = self._resolve_sha(repository, base_ref, operation, budget=budget)
        head_sha = self._resolve_sha(repository, head_ref, operation, budget=budget)
        response = self._read(
            repository,
            operation,
            f"/repos/{repository}/compare/{base_sha}...{head_sha}",
            budget=budget,
            resolved_sha=head_sha,
        )
        data = _json_object(response)
        files = data.get("files")
        commits = data.get("commits")
        total_commits = data.get("total_commits")
        if not isinstance(files, list):
            raise RepositoryTruthError(
                "malformed_provider_response", "compare files payload is malformed"
            )
        if not isinstance(commits, list):
            raise RepositoryTruthError(
                "malformed_provider_response", "compare commits payload is malformed"
            )
        if (
            isinstance(total_commits, bool)
            or not isinstance(total_commits, int)
            or total_commits < 0
        ):
            raise RepositoryTruthError(
                "malformed_provider_response", "compare total_commits is malformed"
            )
        partial = (
            len(files) > MAX_COMPARE_FILES
            or 'rel="next"' in response.headers.get("link", "")
            or total_commits > len(commits)
        )
        projected: list[dict[str, object]] = []
        for item in files[:MAX_COMPARE_FILES]:
            if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
                raise RepositoryTruthError(
                    "malformed_provider_response", "compare file is malformed"
                )
            filename = _validate_path(item["filename"])
            patch = _project_text(item.get("patch"), max_chars=32_768)
            projected.append(
                {
                    "filename": filename,
                    "status": _project_text(item.get("status"), max_chars=32),
                    "additions": item.get("additions") if isinstance(item.get("additions"), int) else 0,
                    "deletions": item.get("deletions") if isinstance(item.get("deletions"), int) else 0,
                    "patch": patch,
                }
            )
        return self._result(
            repository,
            operation,
            requested_ref=f"{base_ref}...{head_ref}",
            resolved_sha=head_sha,
            payload={
                "base_sha": base_sha,
                "head_sha": head_sha,
                "status": _project_text(data.get("status"), max_chars=32),
                "ahead_by": data.get("ahead_by") if isinstance(data.get("ahead_by"), int) else None,
                "behind_by": data.get("behind_by") if isinstance(data.get("behind_by"), int) else None,
                "files": projected,
                "file_limit": MAX_COMPARE_FILES,
            },
            partial=partial,
        )

    def _pull_request(
        self,
        repository: str,
        pr_number: int,
        operation: str,
        budget: _Budget,
    ) -> dict[str, Any]:
        if pr_number <= 0:
            raise RepositoryTruthError("invalid_ref", "PR number must be positive")
        response = self._read(
            repository,
            operation,
            f"/repos/{repository}/pulls/{pr_number}",
            budget=budget,
        )
        data = _json_object(response)
        head = data.get("head")
        base = data.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise RepositoryTruthError(
                "malformed_provider_response", "PR response lacks head/base metadata"
            )
        head_sha = _validate_sha(head.get("sha", ""))
        base_sha = _validate_sha(base.get("sha", ""))
        return {
            "number": pr_number,
            "state": _project_text(data.get("state"), max_chars=32),
            "draft": bool(data.get("draft")),
            "merged": bool(data.get("merged")),
            "head_ref": _project_text(head.get("ref"), max_chars=255),
            "head_sha": head_sha,
            "base_ref": _project_text(base.get("ref"), max_chars=255),
            "base_sha": base_sha,
        }

    def pull_request_truth(
        self,
        repository: str,
        pr_number: int,
    ) -> RepositoryTruthResult:
        operation = _validate_operation("pull_request_truth")
        repository = self._authorize(repository)
        budget = _Budget()
        payload = self._pull_request(repository, pr_number, operation, budget)
        return self._result(
            repository,
            operation,
            requested_ref=f"pr:{pr_number}",
            resolved_sha=str(payload["head_sha"]),
            payload=payload,
        )

    def check_truth(
        self,
        repository: str,
        pr_number: int,
        *,
        expected_head_sha: str,
    ) -> RepositoryTruthResult:
        operation = _validate_operation("check_truth")
        repository = self._authorize(repository)
        expected = _validate_sha(expected_head_sha)
        budget = _Budget()
        pr = self._pull_request(repository, pr_number, operation, budget)
        head_sha = str(pr["head_sha"])
        if head_sha != expected:
            raise RepositoryTruthError(
                "stale_ref",
                "expected PR head no longer matches provider head",
                metadata={"expected_head_sha": expected, "current_head_sha": head_sha},
            )
        response = self._read(
            repository,
            operation,
            f"/repos/{repository}/commits/{head_sha}/check-runs?per_page={MAX_COLLECTION_ITEMS}",
            budget=budget,
            resolved_sha=head_sha,
        )
        data = _json_object(response)
        runs = data.get("check_runs")
        if not isinstance(runs, list):
            raise RepositoryTruthError(
                "malformed_provider_response", "check-runs payload is malformed"
            )
        total_count = data.get("total_count", len(runs))
        if isinstance(total_count, bool) or not isinstance(total_count, int) or total_count < 0:
            raise RepositoryTruthError(
                "malformed_provider_response", "check-runs total_count is malformed"
            )
        partial = len(runs) > MAX_COLLECTION_ITEMS or total_count > len(runs)
        projected = []
        for run in runs[:MAX_COLLECTION_ITEMS]:
            if not isinstance(run, dict):
                raise RepositoryTruthError(
                    "malformed_provider_response", "check run is malformed"
                )
            run_head = _validate_sha(run.get("head_sha", head_sha))
            projected.append(
                {
                    "id": run.get("id") if isinstance(run.get("id"), int) else None,
                    "name": _project_text(run.get("name"), max_chars=256),
                    "status": _project_text(run.get("status"), max_chars=64),
                    "conclusion": _project_text(run.get("conclusion"), max_chars=64),
                    "head_sha": run_head,
                    "stale": run_head != head_sha,
                }
            )
        return self._result(
            repository,
            operation,
            requested_ref=f"pr:{pr_number}",
            resolved_sha=head_sha,
            payload={
                "pr": pr,
                "expected_head_sha": expected,
                "check_runs": projected,
                "collection_limit": MAX_COLLECTION_ITEMS,
            },
            partial=partial,
        )

    def review_truth(
        self,
        repository: str,
        pr_number: int,
        *,
        expected_head_sha: str,
    ) -> RepositoryTruthResult:
        operation = _validate_operation("review_truth")
        repository = self._authorize(repository)
        expected = _validate_sha(expected_head_sha)
        budget = _Budget()
        pr = self._pull_request(repository, pr_number, operation, budget)
        head_sha = str(pr["head_sha"])
        if head_sha != expected:
            raise RepositoryTruthError(
                "stale_ref",
                "expected PR head no longer matches provider head",
                metadata={"expected_head_sha": expected, "current_head_sha": head_sha},
            )
        response = self._read(
            repository,
            operation,
            f"/repos/{repository}/pulls/{pr_number}/reviews?per_page={MAX_COLLECTION_ITEMS}",
            budget=budget,
            resolved_sha=head_sha,
        )
        reviews = _json_list(response)
        partial = (
            len(reviews) > MAX_COLLECTION_ITEMS
            or 'rel="next"' in response.headers.get("link", "")
        )
        projected = []
        for review in reviews[:MAX_COLLECTION_ITEMS]:
            if not isinstance(review, dict):
                raise RepositoryTruthError(
                    "malformed_provider_response", "review payload is malformed"
                )
            user = review.get("user")
            reviewer = user.get("login") if isinstance(user, dict) else None
            commit_id = review.get("commit_id")
            review_sha = None
            if isinstance(commit_id, str) and _SHA_RE.fullmatch(commit_id):
                review_sha = commit_id.lower()
            projected.append(
                {
                    "id": review.get("id") if isinstance(review.get("id"), int) else None,
                    "reviewer": _project_text(reviewer, max_chars=128),
                    "state": _project_text(review.get("state"), max_chars=64),
                    "commit_sha": review_sha,
                    "stale": review_sha is not None and review_sha != head_sha,
                }
            )
        return self._result(
            repository,
            operation,
            requested_ref=f"pr:{pr_number}",
            resolved_sha=head_sha,
            payload={
                "pr": pr,
                "expected_head_sha": expected,
                "reviews": projected,
                "thread_state": "unavailable",
                "semantic_approval": "not_decided_by_repository_truth",
                "collection_limit": MAX_COLLECTION_ITEMS,
            },
            partial=partial,
        )

    def pull_request_comments_truth(
        self,
        repository: str,
        pr_number: int,
        *,
        expected_head_sha: str,
    ) -> RepositoryTruthResult:
        operation = _validate_operation("pull_request_comments_truth")
        repository = self._authorize(repository)
        expected = _validate_sha(expected_head_sha)
        budget = _Budget()
        pr = self._pull_request(repository, pr_number, operation, budget)
        head_sha = str(pr["head_sha"])
        if head_sha != expected:
            raise RepositoryTruthError(
                "stale_ref",
                "expected PR head no longer matches provider head",
                metadata={"expected_head_sha": expected, "current_head_sha": head_sha},
            )
        response = self._read(
            repository,
            operation,
            f"/repos/{repository}/issues/{pr_number}/comments?per_page={MAX_COLLECTION_ITEMS}",
            budget=budget,
            resolved_sha=head_sha,
        )
        comments = _json_list(response)
        partial = (
            len(comments) > MAX_COLLECTION_ITEMS
            or 'rel="next"' in response.headers.get("link", "")
        )
        projected: list[dict[str, object]] = []
        for comment in comments[:MAX_COLLECTION_ITEMS]:
            if not isinstance(comment, dict):
                raise RepositoryTruthError(
                    "malformed_provider_response", "issue comment payload is malformed"
                )
            user = comment.get("user")
            author = user.get("login") if isinstance(user, dict) else None
            raw_body = comment.get("body")
            if raw_body is not None and not isinstance(raw_body, str):
                raise RepositoryTruthError(
                    "malformed_provider_response", "provider text field had the wrong type"
                )
            body_truncated = isinstance(raw_body, str) and len(raw_body) > MAX_COMMENT_BODY_CHARS
            if body_truncated:
                partial = True
            projected.append(
                {
                    "id": comment.get("id") if isinstance(comment.get("id"), int) else None,
                    "author": _project_text(author, max_chars=128),
                    "body": _project_text(raw_body, max_chars=MAX_COMMENT_BODY_CHARS),
                }
            )
        return self._result(
            repository,
            operation,
            requested_ref=f"pr:{pr_number}",
            resolved_sha=head_sha,
            payload={
                "pr": pr,
                "expected_head_sha": expected,
                "comments": projected,
                "collection_limit": MAX_COLLECTION_ITEMS,
            },
            partial=partial,
        )

    def safe_github_url(
        self,
        repository: str,
        *,
        commit_sha: str | None = None,
        pr_number: int | None = None,
        path: str | None = None,
    ) -> RepositoryTruthResult:
        operation = _validate_operation("safe_github_url")
        repository = self._authorize(repository)
        if pr_number is not None:
            if pr_number <= 0 or commit_sha is not None or path is not None:
                raise RepositoryTruthError("invalid_ref", "invalid PR URL target")
            url = f"https://{NAV_HOST}/{repository}/pull/{pr_number}"
            resolved_sha = None
        else:
            if commit_sha is None:
                raise RepositoryTruthError("invalid_ref", "exact commit SHA is required")
            sha = _validate_sha(commit_sha)
            if path is None:
                url = f"https://{NAV_HOST}/{repository}/commit/{sha}"
            else:
                clean_path = _validate_path(path)
                url = f"https://{NAV_HOST}/{repository}/blob/{sha}/{quote(clean_path, safe='/')}"
            resolved_sha = sha
        return self._result(
            repository,
            operation,
            requested_ref=None,
            resolved_sha=resolved_sha,
            payload={"url": url},
        )

    def dispatch(self, operation: str, **kwargs: object) -> RepositoryTruthResult:
        _validate_operation(operation)
        method = getattr(self, operation, None)
        if method is None or not callable(method):
            raise RepositoryTruthError("unsupported_operation", "operation is unavailable")
        return method(**kwargs)