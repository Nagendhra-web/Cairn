# Runtime overhead benchmark

- Generated: 2026-10-02T15:35:17Z by `benchmarks/runtime_overhead.py`
- Git commit: `3e74817bf39fec26a4dd3172f81d9de1c2e5a0bb` (uncommitted changes: True)
- Python 3.11.15 (CPython) on Linux-6.18.44-fc-v51-x86_64-with-glibc2.39, 4 CPUs
- Repetitions per measurement: 7

## Journal append throughput

| store | events | batch | events/s mean | events/s p50 | events/s p95 |
|---|---|---|---|---|---|
| in-memory | 10000 | 50 | 67967 | 68105 | 82509 |
| sqlite | 2500 | 50 | 32461 | 32590 | 36640 |

## Executor overhead per node

Trivial tools (identity), so the time is runtime scheduling, policy checks, labeling and journaling, not tool work.

| shape | nodes | concurrency | wall s mean | ms/node mean | ms/node p50 |
|---|---|---|---|---|---|
| sequential | 10 | 1 | 0.004794 | 0.4794 | 0.4592 |
| sequential | 25 | 1 | 0.012 | 0.4799 | 0.4184 |
| sequential | 50 | 1 | 0.02354 | 0.4708 | 0.4841 |
| parallel | 25 | 8 | 0.01058 | 0.4231 | 0.4111 |
| parallel | 50 | 8 | 0.02061 | 0.4122 | 0.3854 |
| parallel | 50 | 16 | 0.01848 | 0.3695 | 0.365 |

## Replay speed (no tools or models called)

| nodes | effects replayed | effects/s mean | effects/s p50 |
|---|---|---|---|
| 25 | 25 | 2092.9 | 2142.5 |
| 50 | 50 | 1804.3 | 1824 |

## Fork reuse ratio

Patching the last node of a chain reuses every upstream effect; patching the first node invalidates the whole chain.

| nodes | reuse patching last | reuse patching first |
|---|---|---|
| 25 | 24/25 (96%) | 0/25 (0%) |
| 50 | 49/50 (98%) | 0/50 (0%) |
