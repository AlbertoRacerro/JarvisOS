from __future__ import annotations

import base64
import json
import logging

import pytest

from app.modules.coding.repository_truth import (
    MAX_COLLECTION_ITEMS,
    MAX_FILE_PREVIEW_BYTES,
    MAX_SEARCH_CANDIDATES,
    HttpResponse,
    RepositoryTruthError,
    RepositoryTruthService,
)

REPOSITORY = "AlbertoRacerro/JarvisOS"
HEAD_SHA = "a" * 40
BASE_SHA = "b" * 40
OLD_SHA = "c" * 40


def response(payload: object, status: int = 200, **headers: str) -> HttpResponse:
    return HttpResponse(
        status=status,
        headers={key.lower(): value for key, value in headers.items()},
        body=json.dumps(payload).encode("utf-8"),
    )


class FakeTransport:
    def __init__(self, routes: dict[str, HttpResponse | RepositoryTruthError]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def __call__(self, path: str) -> HttpResponse:
        self.calls.append(path)
        value = self.routes.get(path)
        if value is None:
            raise AssertionError(f"unexpected transport path: {path}")
        if isinstance(value, RepositoryTruthError):
            raise value
        return value


def service(
    routes: dict[str, HttpResponse | RepositoryTruthError],
) -> tuple[RepositoryTruthService, FakeTransport]:
    transport = FakeTransport(routes)
    return RepositoryTruthService([REPOSITORY], transport=transport), transport


def commit_route(ref: str = "master", sha: str = HEAD_SHA) -> dict[str, HttpResponse]:
    return {f"/repos/{REPOSITORY}/commits/{ref}": response({"sha": sha})}


@pytest.mark.parametrize(
    "repository",
    ["other/repo", "https://github.com/AlbertoRacerro/JarvisOS", "../owner/repo"],
)
def test_unauthorized_repository_fails_before_dispatch(repository: str) -> None:
    subject, transport = service({})
    with pytest.raises(RepositoryTruthError) as exc:
        subject.repository_ref_truth(repository, "master")
    assert exc.value.code == "unauthorized_repository"
    assert transport.calls == []


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../secret",
        "docs/../secret",
        r"docs\secret",
        "docs/%2e%2e/secret",
        "docs/%2Fsecret",
        "docs/\x00secret",
    ],
)
def test_path_escape_variants_fail_before_content_dispatch(path: str) -> None:
    subject, transport = service(commit_route())
    with pytest.raises(RepositoryTruthError) as exc:
        subject.file_preview(REPOSITORY, "master", path)
    assert exc.value.code == "invalid_path"
    assert transport.calls == []


def test_mutable_ref_is_resolved_before_content_read() -> None:
    encoded = base64.b64encode(b"hello").decode("ascii")
    routes = {
        **commit_route(),
        f"/repos/{REPOSITORY}/contents/README.md?ref={HEAD_SHA}": response(
            {"type": "file", "encoding": "base64", "content": encoded, "sha": OLD_SHA}
        ),
    }
    subject, transport = service(routes)
    result = subject.file_preview(REPOSITORY, "master", "README.md")
    assert result.resolved_sha == HEAD_SHA
    assert result.payload["text"] == "hello"
    assert transport.calls == [
        f"/repos/{REPOSITORY}/commits/master",
        f"/repos/{REPOSITORY}/contents/README.md?ref={HEAD_SHA}",
    ]


def test_file_preview_accepts_github_wrapped_base64() -> None:
    encoded = base64.b64encode(b"hello wrapped provider payload").decode("ascii")
    wrapped = f"{encoded[:12]}\n{encoded[12:24]}\r\n{encoded[24:]}\n"
    routes = {
        **commit_route(),
        f"/repos/{REPOSITORY}/contents/README.md?ref={HEAD_SHA}": response(
            {"type": "file", "encoding": "base64", "content": wrapped, "sha": OLD_SHA}
        ),
    }
    subject, _ = service(routes)
    result = subject.file_preview(REPOSITORY, "master", "README.md")
    assert result.payload["text"] == "hello wrapped provider payload"


def test_binary_preview_is_typed_unsupported() -> None:
    encoded = base64.b64encode(b"\xff\xfe").decode("ascii")
    routes = {
        **commit_route(),
        f"/repos/{REPOSITORY}/contents/blob.bin?ref={HEAD_SHA}": response(
            {"type": "file", "encoding": "base64", "content": encoded, "sha": OLD_SHA}
        ),
    }
    subject, _ = service(routes)
    with pytest.raises(RepositoryTruthError) as exc:
        subject.file_preview(REPOSITORY, "master", "blob.bin")
    assert exc.value.code == "unsupported_content"


def test_oversized_decoded_preview_is_rejected() -> None:
    encoded = base64.b64encode(b"x" * (MAX_FILE_PREVIEW_BYTES + 1)).decode("ascii")
    routes = {
        **commit_route(),
        f"/repos/{REPOSITORY}/contents/large.txt?ref={HEAD_SHA}": response(
            {"type": "file", "encoding": "base64", "content": encoded, "sha": OLD_SHA}
        ),
    }
    subject, _ = service(routes)
    with pytest.raises(RepositoryTruthError) as exc:
        subject.file_preview(REPOSITORY, "master", "large.txt")
    assert exc.value.code == "oversized"


def test_literal_search_reads_exact_sha_tree_and_blobs_without_code_search() -> None:
    encoded = base64.b64encode(b"alpha\nneedle\nomega\nneedle").decode("ascii")
    routes = {
        **commit_route(),
        f"/repos/{REPOSITORY}/git/trees/{HEAD_SHA}?recursive=1": response(
            {
                "truncated": False,
                "tree": [{"type": "blob", "path": "a.txt", "sha": OLD_SHA, "size": 25}],
            }
        ),
        f"/repos/{REPOSITORY}/git/blobs/{OLD_SHA}": response(
            {"encoding": "base64", "content": encoded}
        ),
    }
    subject, transport = service(routes)
    result = subject.literal_search(REPOSITORY, "master", "needle")
    assert result.partial is False
    assert result.payload["matches"] == [
        {"path": "a.txt", "line": 2, "offset": 6},
        {"path": "a.txt", "line": 4, "offset": 19},
    ]
    assert all("/search/" not in path for path in transport.calls)


def test_literal_search_accepts_github_wrapped_blob_base64() -> None:
    encoded = base64.b64encode(b"alpha\nneedle\nomega").decode("ascii")
    wrapped = f"{encoded[:10]}\n{encoded[10:20]}\n{encoded[20:]}\n"
    routes = {
        **commit_route(),
        f"/repos/{REPOSITORY}/git/trees/{HEAD_SHA}?recursive=1": response(
            {
                "truncated": False,
                "tree": [{"type": "blob", "path": "a.txt", "sha": OLD_SHA, "size": 18}],
            }
        ),
        f"/repos/{REPOSITORY}/git/blobs/{OLD_SHA}": response(
            {"encoding": "base64", "content": wrapped}
        ),
    }
    subject, _ = service(routes)
    result = subject.literal_search(REPOSITORY, "master", "needle")
    assert result.payload["matches"] == [{"path": "a.txt", "line": 2, "offset": 6}]


def test_literal_search_candidate_bound_is_explicit_partial() -> None:
    tree = [
        {"type": "blob", "path": f"{index}.txt", "sha": f"{index:040x}", "size": 1}
        for index in range(MAX_SEARCH_CANDIDATES + 1)
    ]
    routes: dict[str, HttpResponse | RepositoryTruthError] = {
        **commit_route(),
        f"/repos/{REPOSITORY}/git/trees/{HEAD_SHA}?recursive=1": response(
            {"truncated": False, "tree": tree}
        ),
    }
    for index in range(MAX_SEARCH_CANDIDATES):
        routes[f"/repos/{REPOSITORY}/git/blobs/{index:040x}"] = response(
            {"encoding": "base64", "content": base64.b64encode(b"x").decode("ascii")}
        )
    subject, _ = service(routes)
    result = subject.literal_search(REPOSITORY, "master", "needle")
    assert result.partial is True
    assert result.payload["candidate_files_examined"] == MAX_SEARCH_CANDIDATES


def test_pull_request_truth_is_bound_to_exact_head_and_base() -> None:
    routes = {
        f"/repos/{REPOSITORY}/pulls/7": response(
            {
                "state": "open",
                "draft": False,
                "merged": False,
                "head": {"ref": "feature", "sha": HEAD_SHA},
                "base": {"ref": "master", "sha": BASE_SHA},
            }
        )
    }
    subject, _ = service(routes)
    result = subject.pull_request_truth(REPOSITORY, 7)
    assert result.resolved_sha == HEAD_SHA
    assert result.payload["head_sha"] == HEAD_SHA
    assert result.payload["base_sha"] == BASE_SHA


def test_check_truth_rejects_stale_expected_head_before_check_dispatch() -> None:
    routes = {
        f"/repos/{REPOSITORY}/pulls/7": response(
            {
                "state": "open",
                "head": {"ref": "feature", "sha": HEAD_SHA},
                "base": {"ref": "master", "sha": BASE_SHA},
            }
        )
    }
    subject, transport = service(routes)
    with pytest.raises(RepositoryTruthError) as exc:
        subject.check_truth(REPOSITORY, 7, expected_head_sha=OLD_SHA)
    assert exc.value.code == "stale_ref"
    assert transport.calls == [f"/repos/{REPOSITORY}/pulls/7"]


def test_check_truth_preserves_per_run_sha_staleness() -> None:
    routes = {
        f"/repos/{REPOSITORY}/pulls/7": response(
            {
                "state": "open",
                "head": {"ref": "feature", "sha": HEAD_SHA},
                "base": {"ref": "master", "sha": BASE_SHA},
            }
        ),
        f"/repos/{REPOSITORY}/commits/{HEAD_SHA}/check-runs?per_page={MAX_COLLECTION_ITEMS}": response(
            {
                "total_count": 2,
                "check_runs": [
                    {
                        "id": 1,
                        "name": "CI",
                        "status": "completed",
                        "conclusion": "success",
                        "head_sha": HEAD_SHA,
                    },
                    {
                        "id": 2,
                        "name": "old",
                        "status": "completed",
                        "conclusion": "success",
                        "head_sha": OLD_SHA,
                    },
                ],
            }
        ),
    }
    subject, _ = service(routes)
    result = subject.check_truth(REPOSITORY, 7, expected_head_sha=HEAD_SHA)
    assert result.payload["check_runs"][0]["stale"] is False
    assert result.payload["check_runs"][1]["stale"] is True


def test_review_truth_never_converts_reviews_or_checks_into_merge_authority() -> None:
    routes = {
        f"/repos/{REPOSITORY}/pulls/7": response(
            {
                "state": "open",
                "head": {"ref": "feature", "sha": HEAD_SHA},
                "base": {"ref": "master", "sha": BASE_SHA},
            }
        ),
        f"/repos/{REPOSITORY}/pulls/7/reviews?per_page={MAX_COLLECTION_ITEMS}": response(
            [
                {
                    "id": 4,
                    "state": "APPROVED",
                    "commit_id": HEAD_SHA,
                    "user": {"login": "reviewer"},
                }
            ]
        ),
    }
    subject, _ = service(routes)
    result = subject.review_truth(REPOSITORY, 7, expected_head_sha=HEAD_SHA)
    assert result.payload["reviews"][0]["state"] == "APPROVED"
    assert result.payload["thread_state"] == "unavailable"
    assert result.payload["semantic_approval"] == "not_decided_by_repository_truth"


def test_review_truth_marks_provider_next_page_as_partial() -> None:
    reviews = [
        {
            "id": index,
            "state": "COMMENTED",
            "commit_id": HEAD_SHA,
            "user": {"login": f"reviewer-{index}"},
        }
        for index in range(MAX_COLLECTION_ITEMS)
    ]
    routes = {
        f"/repos/{REPOSITORY}/pulls/7": response(
            {
                "state": "open",
                "head": {"ref": "feature", "sha": HEAD_SHA},
                "base": {"ref": "master", "sha": BASE_SHA},
            }
        ),
        f"/repos/{REPOSITORY}/pulls/7/reviews?per_page={MAX_COLLECTION_ITEMS}": response(
            reviews,
            link='<https://api.github.com/repos/owner/repo/pulls/7/reviews?page=2>; rel="next"',
        ),
    }
    subject, _ = service(routes)
    result = subject.review_truth(REPOSITORY, 7, expected_head_sha=HEAD_SHA)
    assert result.partial is True
    assert len(result.payload["reviews"]) == MAX_COLLECTION_ITEMS


@pytest.mark.parametrize(
    ("status", "headers", "code"),
    [
        (302, {}, "redirect_forbidden"),
        (401, {}, "authentication_required"),
        (403, {"x-ratelimit-remaining": "0"}, "rate_limited"),
        (404, {}, "not_found"),
        (429, {}, "rate_limited"),
    ],
)
def test_provider_failures_remain_typed(
    status: int, headers: dict[str, str], code: str
) -> None:
    subject, _ = service(
        {f"/repos/{REPOSITORY}/commits/master": response({}, status=status, **headers)}
    )
    with pytest.raises(RepositoryTruthError) as exc:
        subject.repository_ref_truth(REPOSITORY, "master")
    assert exc.value.code == code


def test_transient_transport_failure_retries_once() -> None:
    calls = 0

    def transport(path: str) -> HttpResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RepositoryTruthError("timeout", "temporary")
        return response({"sha": HEAD_SHA})

    subject = RepositoryTruthService([REPOSITORY], transport=transport)
    result = subject.repository_ref_truth(REPOSITORY, "master")
    assert result.resolved_sha == HEAD_SHA
    assert calls == 2


@pytest.mark.parametrize(
    "operation",
    [
        "create",
        "update_file",
        "delete_ref",
        "merge",
        "workflow_dispatch",
        "label",
        "release",
        "secret",
    ],
)
def test_write_capable_operation_names_are_rejected_without_dispatch(operation: str) -> None:
    subject, transport = service({})
    with pytest.raises(RepositoryTruthError) as exc:
        subject.dispatch(operation, repository=REPOSITORY)
    assert exc.value.code == "unsupported_operation"
    assert transport.calls == []


def test_safe_urls_use_only_authorized_repository_and_validated_exact_identifiers() -> None:
    subject, transport = service({})
    commit = subject.safe_github_url(REPOSITORY, commit_sha=HEAD_SHA)
    file_url = subject.safe_github_url(
        REPOSITORY, commit_sha=HEAD_SHA, path="docs/specs/README.md"
    )
    pr = subject.safe_github_url(REPOSITORY, pr_number=7)
    assert commit.payload["url"] == f"https://github.com/{REPOSITORY}/commit/{HEAD_SHA}"
    assert file_url.payload["url"] == (
        f"https://github.com/{REPOSITORY}/blob/{HEAD_SHA}/docs/specs/README.md"
    )
    assert pr.payload["url"] == f"https://github.com/{REPOSITORY}/pull/7"
    assert transport.calls == []


def test_safe_url_rejects_arbitrary_repository() -> None:
    subject, _ = service({})
    with pytest.raises(RepositoryTruthError) as exc:
        subject.safe_github_url("evil/repo", commit_sha=HEAD_SHA)
    assert exc.value.code == "unauthorized_repository"


def test_read_log_contains_digest_not_raw_repository_or_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    subject, _ = service(commit_route())
    with caplog.at_level(logging.INFO, logger="app.modules.coding.repository_truth"):
        subject.repository_ref_truth(REPOSITORY, "master")
    text = caplog.text
    assert "coding_repository_read" in text
    assert REPOSITORY not in text
    assert HEAD_SHA not in text
    assert "repository_digest=" in text


def test_malformed_json_is_typed() -> None:
    subject, _ = service(
        {
            f"/repos/{REPOSITORY}/commits/master": HttpResponse(
                status=200, headers={}, body=b"{not-json"
            )
        }
    )
    with pytest.raises(RepositoryTruthError) as exc:
        subject.repository_ref_truth(REPOSITORY, "master")
    assert exc.value.code == "malformed_provider_response"


def test_directory_bound_is_partial_not_complete() -> None:
    entries = [
        {"path": f"dir/{index}.txt", "type": "file", "sha": HEAD_SHA, "size": 1}
        for index in range(1_000)
    ]
    routes = {
        **commit_route(),
        f"/repos/{REPOSITORY}/contents?ref={HEAD_SHA}": response(entries),
    }
    subject, _ = service(routes)
    result = subject.path_list(REPOSITORY, "master")
    assert result.partial is True
    assert len(result.payload["entries"]) == 1_000
