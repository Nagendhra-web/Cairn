# Roadmap

Items are ordered by expected value. Nothing here is implemented unless it appears in the
CHANGELOG.

## Next (0.2)

- **Postgres backends** for the journal, memory store and work queue (multi-node deployments
  beyond a shared SQLite file).
- **`cairn eval` CLI** wrapping `EvalRunner` for JSONL datasets, with regression gates for CI.
- **Neural embeddings by default when configured** (`ProviderEmbedder` wired from config) and an
  approximate nearest neighbor `VectorIndex` adapter.
- **Native tool-calling nodes** (a bounded ReAct-style node whose tool calls still pass through the
  policy engine and the journal).
- **Speech input** (transcription as a journaled effect) alongside the existing image and document
  content parts.

## Later

- Container-level sandbox for `code.python` (gVisor or Firecracker) with no network by default.
- Fine-grained secrecy policies (per-destination allowlists for egress tools).
- Distributed tracing context propagation into MCP servers.
- Multi-tenant API with per-tenant budgets and storage isolation.
- Browser automation tool with page content labeled untrusted by origin.

## Research directions

- Declassification workflows: operator-approved promotion of untrusted values to trusted, recorded
  in the journal.
- Learning planners from procedural memory and replayed trajectories.
- Replay-based A/B evaluation of model upgrades on recorded production traffic.
