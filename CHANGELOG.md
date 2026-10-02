# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-02

First public release.

### Added

- Event-sourced execution journal with per-run hash chains, optimistic concurrency and SQLite
  storage with namespaced migrations.
- Plan IR (tool, llm, retrieve, memory, agent, approval, verify, map, loop nodes) with static
  validation, conditional branches, retries with backoff, fallbacks, timeouts and `on_error`.
- Concurrent executor with budgets (tokens, cost, calls, wall time, depth, concurrency),
  cancellation, human approval gates and crash-safe resume.
- Strict model-free replay with divergence reports, and counterfactual forks that reuse
  unchanged effects.
- Provenance labels (integrity, sources, secrecy) with data and control (implicit flow)
  propagation, and a flow policy engine enforced at every tool call.
- Model router with tiers, capability filtering, fallback, circuit breakers and rate limits;
  providers for Claude (official SDK), any OpenAI-compatible server (including Ollama and vLLM)
  and a deterministic scripted provider.
- Tool registry with typed `@tool` contracts, discovery, secrets scoping and redaction; built-in
  sandboxed filesystem, allowlisted HTTP, isolated Python execution, calculator and email tools.
- MCP client and server over stdio with tool pinning and rug-pull quarantine.
- Multi-layer memory with importance scoring, decay, deduplication, consolidation and a
  provenance-enforced write policy.
- Hybrid (BM25 + dense) retrieval with RRF, knowledge-graph multi-hop expansion, reranking and an
  adaptive self-correcting loop.
- Planner with budgeted provenance-aware context and a repair loop, critic, agents with bounded
  replanning, supervisor compiling delegation into plan IR, durable sub-agent runs.
- Evaluation framework (metrics, trajectories, versioned datasets, LLM judge, experiment tracking,
  replay regression) and four benchmarks with measured results.
- Run reports, trace trees, HTML timelines, OTLP export and JSON logging.
- CLI, HTTP API with SSE and WebSocket streaming and a dashboard, SQLite work queue and workers.
