# Semantic judgment cases

`cases.json` is a version 1 object with a `cases` list. Each case has an `id`,
`category`, `title`, `context`, `question`, labelled `options`, an `answer`
(`verdict`, `options`, `missing`), and a `check` (`function`, `inputs`,
`comparison`, `threshold`, `expected`). For missing-input choices, mark
irrelevant candidates with `distractor: true`.

To add a case, state every check input and its units in the context, calculate
the result independently, set the answer labels, and run the focused test. The
check must prove the answer from the stated quantities.

From the repository root:

```bash
python eval/local/semantic_judgment/semantic_eval.py --dry-run
python eval/local/semantic_judgment/semantic_eval.py --emit-prompts prompts.json
python eval/local/semantic_judgment/semantic_eval.py --grade replies.json
```

`replies.json` maps case ids to raw reply strings. Grading prints per-case
results and per-category score/count totals. The exporter includes answers for
offline grading, so keep exported grading inputs separate from model prompts.
