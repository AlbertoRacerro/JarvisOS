# Semantic judgment cases

`cases.json` is a version 1 object with a `cases` list. Each case has an `id`,
`category`, `title`, `context`, `question`, labelled `options`, an `answer`
(`verdict`, `options`, `missing`), and a `check` (`function`, `inputs`,
`comparison`, `threshold`, `expected`). For missing-input choices, mark
irrelevant candidates with `distractor: true`.

To add a case, state every check input and its units in the context, give the
solver's computed result, calculate it independently, set the answer labels,
and run the focused test. The check must prove the answer from the stated
quantities. Keep validity limits in `check`, except for the observed data range
of an empirical fit. In insufficient-data cases, list supplied values only;
put candidate missing inputs in the choices. For conservation cases, state the
flows and conditions without calculating the residual in the prompt.

From the repository root:

```bash
python eval/local/semantic_judgment/semantic_eval.py --dry-run
python eval/local/semantic_judgment/semantic_eval.py --emit-prompts prompts.json
python eval/local/semantic_judgment/semantic_eval.py --grade replies.json
```

`replies.json` maps case ids to raw reply strings or nonempty arrays of raw
reply strings. Array results include each attempt's parsed labels and score,
the majority `(verdict, options, missing)` answer and score, a disagreement
flag (including ties), and the number of attempts that scored 1. Grading also
prints per-category score/count totals. The exporter includes answers for
offline grading, so keep exported grading inputs separate from model prompts.

The host-only policy registers each evaluation cell and both sandboxed phases.
For example (replace paths, ports, unit, and model with host-owned values):

```json
{
  "ops": {
    "bench.run": {
      "benchmarks": {
        "semantic": {
          "cwd": "eval/local/semantic_judgment",
          "argv": ["python", "semantic_eval.py", "--emit-prompts", "prompts.json"],
          "grade_argv": ["python", "semantic_eval.py", "--grade", "{replies_file}"],
          "result_file": "prompts.json"
        }
      }
    },
    "service.control": {"units": ["candidate.service"]},
    "local_eval.run": {
      "evaluations": {
        "semantic": {
          "kind": "semantic_judgment",
          "model": "registered-model",
          "base_url": "http://127.0.0.1:18081/v1",
          "credential_file": "/host/config/semantic-key",
          "thinking": "off",
          "max_tokens": 4096,
          "per_request_timeout_s": 300,
          "timeout_s": 7200,
          "attempts": 1,
          "temperature": 0,
          "server_unit": "candidate.service",
          "stop_default": true,
          "backend_port": 18080
        }
      }
    }
  }
}
```

The candidate unit must be listed in the host's `service.control.units`
policy. The host sends only prompt text to the model and returns labels,
scores, usage, timing, and memory samples.
