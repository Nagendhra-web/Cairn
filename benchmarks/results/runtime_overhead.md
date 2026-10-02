# Runtime overhead benchmark

- Generated: 2026-10-02T16:03:59Z by `benchmarks/runtime_overhead.py`
- Git commit: `d7ded04eb3cf914538a483347338afedd7155054` (uncommitted changes: True)
- Python 3.11.15 (CPython) on Linux-6.18.44-fc-v51-x86_64-with-glibc2.39, 4 CPUs
- Repetitions per measurement: 7

## Journal append throughput

| store | events | batch | events/s mean | events/s p50 | events/s p95 |
|---|---|---|---|---|---|
| in-memory | 10000 | 50 | 76103 | 79044 | 81915 |
| sqlite | 2500 | 50 | 32061 | 31405 | 37513 |

## Executor overhead per node

Trivial tools (identity), so the time is runtime scheduling, policy checks, labeling and journaling, not tool work.

| shape | nodes | concurrency | wall s mean | ms/node mean | ms/node p50 |
|---|---|---|---|---|---|
| sequential | 10 | 1 | 0.005083 | 0.5083 | 0.4574 |
| sequential | 25 | 1 | 0.0172 | 0.6881 | 0.6408 |
| sequential | 50 | 1 | 0.02156 | 0.4311 | 0.4284 |
| parallel | 25 | 8 | 0.008835 | 0.3534 | 0.3468 |
| parallel | 50 | 8 | 0.01996 | 0.3991 | 0.3646 |
| parallel | 50 | 16 | 0.01811 | 0.3622 | 0.3512 |

## Replay speed (no tools or models called)

| nodes | effects replayed | effects/s mean | effects/s p50 |
|---|---|---|---|
| 25 | 25 | 2478.2 | 2480.7 |
| 50 | 50 | 2146.6 | 2197.9 |

## Fork reuse ratio

Patching the last node of a chain reuses every upstream effect; patching the first node invalidates the whole chain.

| nodes | reuse patching last | reuse patching first |
|---|---|---|
| 25 | 24/25 (96%) | 0/25 (0%) |
| 50 | 49/50 (98%) | 0/50 (0%) |
