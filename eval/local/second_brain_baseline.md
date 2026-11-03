# Second-brain E5 repository benchmark - baseline (historical run, kept as reference)

Re-run with `python eval/local/second_brain_benchmark.py --embedder e5` (see eval/local/README.md).

## Run identity

- Code SHA under test: `b49feddf4a887b32a63354743e8db0ceaf26bce5`
- Command, from `backend/`: `PYTHONPATH=. /home/user/jarvis-control/work/venvs/q148/bin/python eval/local/second_brain_benchmark.py --embedder e5 (originally `-m app.modules.ai.retrieval_benchmark`)`
- Python: 3.11.16
- Platform: Linux 6.18.33.2 Microsoft WSL2, x86_64, glibc 2.39
- Model: `intfloat/multilingual-e5-small`
- sentence-transformers 6.1.0; sqlite-vec 0.1.9; torch 2.14.0+cpu; transformers 5.17.0
- The benchmark used the local model and sqlite-vec accelerator. It made no provider calls.

## Scope and results

The repository case reads tracked file contents at the exact code SHA from `backend/app`, `backend/tests`, `docs`, and `scripts`; it does not read uncommitted working-tree content. The corpus contained 1,207 files and 7,941 indexed full documents (Git files plus top-level Python symbols). It ran 18 pre-registered repository queries and measured bundles with a 4,000-token budget. Rebuild time is the measured `SQLiteIndexStore.rebuild()` duration, not end-to-end process time.

The separate `seeded_workspace` case uses 10 synthetic fixture records and 10 matching queries. It is a deterministic smoke case, not repository or product retrieval evidence.

```json
{"cases":[{"case":"seeded_workspace","documents":10,"expanded_token_estimate_median":199.5,"expanded_token_estimate_p90":209,"expanded_token_estimates":[208,208,196,180,213,209,180,185,201,198],"index_revision":"sha256:c6098feb9e502c0829e85909a388275684c399083e9b1b4e1d5efc7836d3b71f","misses":[],"queries":10,"rebuild_seconds":2.531,"recall_at_10":"10/10","sqlite_vec_used":true,"token_estimate_median":132.0,"token_estimate_p90":134,"token_estimates":[132,132,132,114,134,133,114,133,134,132]},{"case":"repository_at_head","documents":7941,"expanded_token_estimate_median":3242.0,"expanded_token_estimate_p90":3970,"expanded_token_estimates":[3304,2348,2190,1595,2291,1041,3066,891,3952,907,3583,3969,3980,3935,3980,3875,3180,3970],"index_revision":"sha256:d14157bd5a903e1252cf5fd1384e7a39fcb207d03eca8e102915849ae37894c9","misses":["backend/app/modules/ai/retrieval_index.py","scripts/check_typecheck_ratchet.py","backend/app/modules/modeling/parameter_lifecycle.py","docs/specs/STATUS.md"],"queries":18,"rebuild_seconds":1640.614,"recall_at_10":"14/18","sqlite_vec_used":true,"token_estimate_median":861.0,"token_estimate_p90":988,"token_estimates":[868,837,931,737,854,790,768,891,883,907,916,814,805,1110,822,847,1036,988]}],"embedder":"e5","indexed_roots":["backend/app","backend/tests","docs","scripts"],"repository_files":1207,"sha":"b49feddf4a887b32a63354743e8db0ceaf26bce5"}
```

## Interpretation and limits

- Repository bundle size was below the packet's 2,000-token median target and 4,000-token ordinary-case target: median 861 tokens, p90 988 tokens. Expansion remained under the configured 4,000-token budget: median 3,242, p90 3,970 tokens.
- The four misses correspond to these queries: “reciprocal rank fusion of lexical and vector search”; “fail when type errors increase compared to a base commit”; “parameter supersedes lifecycle”; and “which spec is active and its hard dependencies”. These are measured recall gaps, not hidden or excluded cases.
- The 1,640.614-second rebuild is a material cost for the full repository corpus on this CPU-only WSL2 environment.
- This run measures Git repository retrieval only. It does not establish SQL workspace recall, scientific validity, or Hermes operational-memory retrieval quality. Canonical SQL authoritative rereads, stale/deleted refusal, bundle expansion, and typed graph behavior are covered by deterministic tests. The Hermes adapter uses the 146 runtime's data-root home but this run creates no Hermes transcript or memory store.
- Follow-up adapter scope (separate from this unchanged benchmark): Hermes 0.21.4 built-in memory entries are stored at `HERMES_HOME/memories/MEMORY.md` and `USER.md`, split by the literal `\n§\n` delimiter. The Hermes-owned session store is `HERMES_HOME/state.db` with `sessions` and `messages` tables; its transcripts remain behind Hermes `session_search` and are not copied into JarvisOS retrieval. JarvisOS derives at most 128 entries / 256 KB from files modified within 365 days, redacts secret-shaped values, labels them operational and non-canonical, and rereads the files when building a bundle. These remain operational context, never canonical JarvisOS state. The E5 numbers and corpus above are unchanged and do not measure this adapter.
- Retrieval relevance is not evidence of scientific validity. Source refs and digests remain subject to authoritative reread before bundle inclusion.
