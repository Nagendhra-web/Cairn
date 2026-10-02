# Plan files

Plans are JSON documents in Cairn's Plan IR (`cairn.runtime.Plan`). The CLI runs
them with `cairn exec`, the HTTP API accepts them on `POST /v1/runs`, and
`Plan.model_validate_json(...)` loads them in Python.

## research_digest.json

A tool-only plan (no models, no network) that:

1. computes three figures in parallel with a `map` node over `math.calculate`,
2. writes a Markdown digest with `fs.write`, interpolating the figures and the
   run input `topic` through `$tmpl`,
3. reads the file back with `fs.read` (its output is labeled `untrusted`, as all
   file contents are), and
4. checks the read-back text with a `verify` node.

Run it from a scratch directory so the journal (`.cairn/`) and the sandbox
(`workspace/`) that `cairn exec` creates in the current directory stay out of
the repository:

```bash
cd "$(mktemp -d)"
cairn exec /path/to/New-Repo/examples/plans/research_digest.json --input topic="Pricing plans"
cat workspace/digest/research_digest.md
cairn runs                      # the run is journaled in ./.cairn/cairn.db
cairn replay <run_id>           # strict replay: zero live tool calls
```

`--input topic=...` is required: the plan reads it as `{{$input.topic}}`.
Add `--json` to print the run result as JSON instead of the rendered report.

## Writing your own

- Every node needs an `id` and a `kind` (`tool`, `llm`, `retrieve`, `memory`,
  `agent`, `approval`, `verify`, `map`, `loop`).
- Reference earlier outputs with `{"$ref": "node_id.field[0]"}` or interpolate
  them with `{"$tmpl": "... {{node_id}} ..."}`; references add dependencies
  automatically.
- `cairn exec` validates the plan before running anything: unknown tools,
  missing grants, bad references and cycles are reported without side effects.
