# Provider Selection Policy (Stage 5-PRE)

Status: **design contract**. No provider execution, no registry, no API keys, no model
calls. Defines the *order of gates* and the *provider classes* as a deterministic policy.

## Decision order (deterministic, fail-closed)

Provider choice is NOT "most intelligent" or "cheapest" first. The order is fixed:

```
1. sensitivity gate        -> classify material (S0..S4); S2+ blocks raw external
2. consent / confirmation  -> user approval where policy requires it
3. task intelligence level -> how capable a model the task actually needs
4. economic envelope       -> cost-quality budget (RouteLLM-style discipline)
5. provider class          -> pick the cheapest class that satisfies 1-4
6. execution               -> only after 1-5 pass
```

If any earlier gate fails, later gates are not consulted. A cheap or smart provider can
never override the sensitivity gate.

## Provider classes (conceptual, not executable)

These are **classes**, not configured providers. No current provider is named as an
executable choice here; mapping concrete models to classes is a later, separate slice.

| Class | Eligible material | Notes |
|---|---|---|
| `local_only` | S0..S3 (S3 only redacted) | never leaves machine; default for sensitive work |
| `local_preferred` | S0..S2 | prefer local; cloud only if task needs it AND material allows |
| `sanitized_cloud_cheap` | S0, S1, sanitized-S2 | cost-optimized; requires sanitized derivative for S2 |
| `sanitized_cloud_frontier` | S0, S1, sanitized-S2 | high-capability; same sanitization rule |
| `deep_research_public` | S0 only | public research; never private/IP |
| `blocked_requires_user_review` | S3, S4, unknown-for-cloud | no automatic path; user must act |

## Authority model (from routing handoff)

- **Deterministic-first.** A model-based router (RouteLLM-style) or any LLM recommendation
  is **advisory only**. Policy, tests, and schema are the authority.
- **Explainability (Wayfinder-style).** Every routing decision must be explainable as a
  trace of which gate selected which class and why — no opaque scoring as final authority.
- **Economic discipline (RouteLLM-style).** The economic envelope is `cost_per_success`
  thinking with baselines (always-cheap / always-frontier / oracle) — but only *after* the
  sensitivity and consent gates pass.
- **Local capability (DeepSparkInference-style).** Local backend capability is a
  *capability contract*, not a runtime to launch here. No vLLM/registry/execution in any
  PRE slice.

## CURRENT vs TARGET

| Aspect | CURRENT | TARGET |
|---|---|---|
| default policy mode | `FAST_DEV` (`schema.py:262`), fail-open on ambiguous | sensitivity gate first; ambiguous → blocked for cloud |
| provider gating | `ai/budget.py` + `ai/settings.py` flags, provider-shaped (Scaleway/DeepSeek) | provider-neutral classes; concrete providers mapped separately |
| egress decision | `decide_for_smoke_console` / `decide_for_external_smoke_test` (divergent) | single sensitivity gate feeding the order above |

The provider-shaped settings (`scaleway_*`, DeepSeek-specific status) stay as
compatibility fields; provider-neutral classes are introduced *over* them, not by
rewriting them in this slice.

## Frozen rule

```
S2/S3/S4 do not exit automatically.
A hard but sensitive task does not lower its sensitivity.
Produce a sanitized S1 derivative (with provenance), then optionally use cloud/frontier.
```
