# Coding Runtime integrated terminal — future approved direction — 2026-08-27

Status: maintainer-approved future product direction; not runtime implementation authority.

## Decision

The lower `Coding > Runtime` utility area may evolve from a logs-only surface into a shared:

`Terminal | Logs`

surface.

The terminal should be a **real integrated terminal**, not a visual mock: PowerShell is the default Windows shell on the maintainer workstation, while the implementation must keep a replaceable PTY/process adapter for CI and future platform support.

## User-facing intent

The maintainer should not need to close/minimize JarvisOS and open a separate PowerShell window for ordinary repository/runtime commands.

Useful interactions include:

- run normal PowerShell commands inside JarvisOS;
- keep current working directory/session state while working;
- interrupt a running process with `Ctrl+C`;
- scroll terminal output/history;
- `Open terminal here` from a selected Repository Inspector path/worktree;
- send selected/current terminal output to Jarvis as explicit context;
- receive a Jarvis-proposed command with `Insert in terminal` / copy, without silent execution;
- retain Logs as an adjacent tab rather than deleting runtime evidence.

## Safety boundary

This feature must not weaken JarvisOS authority or the repository-wide no-secret frontend-response invariant.

- Frontend receives no direct shell/filesystem/process authority.
- PTY/process creation is mediated by a typed local backend/runtime service.
- The service must be local-only/appropriately authenticated and must not become a remote shell server.
- Working-directory/path targets are validated by backend policy; protected credential/config/secret-store locations require explicit denial/isolation according to the accepted terminal security spec.
- The terminal child process uses a deliberately scrubbed/minimum environment and must not inherit provider API keys, repository tokens, credential-store secrets or other protected values by default.
- Raw PTY bytes are not a frontend contract. Before any stdout/stderr/control stream becomes a frontend response, it passes through a backend-owned secret-safe display/isolation/redaction boundary whose target-OS failure modes are explicitly tested.
- If adequate secret isolation/redaction cannot be proven on the target OS/runtime, arbitrary interactive PTY streaming remains unavailable/`DEFER_TRIGGERED` rather than weakening the no-secret invariant.
- Secrets must not be copied into Jarvis context or durable logs; `Send output to Jarvis` applies a separate bounded context/egress policy on top of the mandatory frontend display boundary.
- Jarvis command suggestions are proposals. Default behavior is not auto-execution.
- High-risk/destructive command classes require explicit confirmation/policy and cannot rely on prompt text alone for enforcement.
- Terminal access never authorizes bypassing Git/spec/review/update boundaries for JarvisOS self-modification.
- Linux CI uses fake/controlled adapters and does not depend on Windows PowerShell being present.

## Architecture direction

Conceptually:

`Runtime UI terminal emulator -> typed local terminal session API -> secret-safe display/policy boundary -> PTY/process adapter -> pwsh.exe / powershell.exe on Windows`

The full specification must choose the minimum safe implementation after auditing current local runtime/server boundaries. It must explicitly define local authentication, process/environment isolation, cwd/path policy, output redaction, interrupt/session lifecycle and destructive-command confirmation before exposing a real shell. Do not select a terminal library, WebSocket protocol, process supervisor, persistence model or command policy solely from this visual note.

## Relation to final Coding reference

This note supplements `CODING_BETA_APPROVED_2026-08-27.md` and PD-08. It does not alter the approved first-screen Runtime hierarchy: local-vs-GitHub version divergence remains the dominant concept; the terminal occupies the lower utility surface and should not displace version alignment, semantic delta, health or safe-update controls.
