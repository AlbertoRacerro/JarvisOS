# Runner trust boundary

JarvisOS currently runs as a single-operator, loopback-first application. The
runner cleanup removes caller authority over executable source so the API can
later be exposed to additional product surfaces without inheriting an
unnecessary code-upload contract.

> JarvisOS runner execution is a trusted-code execution boundary, not a hostile-code sandbox. Static policy checks, digest verification, path confinement, a clean subprocess launch, output contracts, and timeouts reduce mistakes and preserve evidence for reviewed implementations. They do not safely contain malicious Python. Caller-controlled and automatically AI-generated Python therefore remain non-executable unless and until a separate OS-isolation specification is implemented and independently proven.

No runtime status, error code, API field, UI label, runbook, test name, or
implementation report may imply stronger isolation. The AST policy remains a
lint and defense-in-depth check over reviewed bundled code.

## Final invariant

> JarvisOS may execute exact reviewed Python that the server and maintainer already trust. It may not execute Python merely because a caller, model, route, old database row, AST checker, or successful previous run says that it is trusted.

A future feature that executes untrusted or automatically AI-generated code
requires a separate specification with real OS-level isolation, hostile-code
tests against that boundary, resource and network controls, and explicit
maintainer acceptance.

## Studio user code, spec 187

`studio_runtime` is a separate, explicitly invoked boundary for operator-authored
Python. It is off until `JARVISOS_STUDIO_USER_CODE_ENABLED=1`. On Linux/WSL2 it
requires bubblewrap and a successful namespace probe; otherwise execution is
unavailable, with no unisolated fallback. Each run gets separate network, PID,
mount and user namespaces, a read-only Python environment, one temporary writable
scratch directory, a cleared environment, resource limits and bounded output.
The temporary directory is removed after each run. The host validates the JSON
result before returning it. Native Windows is unavailable in this slice.

This is single-operator isolation, not a multi-tenant or internet-facing security
claim. It does not grant the AI Sidecar execution authority. The existing runner
continues to execute only exact reviewed bundled implementations; its AST policy
remains lint and does not become a sandbox.

Spec 188 adds one non-bundled kind to the runner-job flow, `studio_calc_v1`: an
operator-published Studio calculation. The runner refuses it unless the version is
published, its source artifact matches the hash recorded at publication, and the
Studio runtime is enabled and available. Its source executes only inside
`studio_runtime`, never through the runner's own subprocess path. Every other
non-bundled kind stays refused.

## Reopen triggers

Reopen measured runner-isolation work only when at least one condition becomes
real:

- JarvisOS listens on a non-loopback interface;
- a second user can reach the application;
- a remote agent can reach runner mutation or execution routes;
- Hermes or MCP receives runner execution authority;
- a genuinely non-bundled implementation must execute and git-based bundling is
  demonstrably inadequate.

The Origin check on mutating runner routes is a browser/CSRF guard. It is not
authentication: origin-less local clients remain valid, and a local process can
construct its own HTTP request.
