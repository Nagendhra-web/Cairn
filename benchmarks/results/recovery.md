# Fault-injection recovery benchmark

- Generated: 2026-10-02T15:35:18Z by `benchmarks/recovery.py`
- Git commit: `3e74817bf39fec26a4dd3172f81d9de1c2e5a0bb` (uncommitted changes: True)
- Python 3.11.15 (CPython) on Linux-6.18.44-fc-v51-x86_64-with-glibc2.39, 4 CPUs
- Repetitions per scenario: 5

Faults are injected deterministically (scripted model `fail_times`, a tool that fails its first N calls). Each scenario pairs a fault with a recovery policy; controls with no policy show the fault is real. 'Extra attempts' counts node attempts beyond a clean run.

Recovery rate where a policy should recover: 100.0%.

| scenario | faults | expected | success | mean extra attempts | mean recovered nodes |
|---|---|---|---|---|---|
| tool_no_retry_1fault | 1 | fail | 0.0% | 0 | 0 |
| tool_retry_2faults | 2 | ok | 100.0% | 2 | 1 |
| tool_retry_exhausted_5faults | 5 | fail | 0.0% | 3 | 0 |
| tool_default_1fault | 1 | ok | 100.0% | 0 | 0 |
| model_no_retry_1fault | 1 | fail | 0.0% | 0 | 0 |
| model_retry_2faults | 2 | ok | 100.0% | 2 | 1 |
| model_fallback_1fault | 1 | ok | 100.0% | 1 | 1 |

