"""Evaluation runner, experiment tracking and regression comparison.

Design choices, and why:

* **A fresh Runtime per case.** The runner takes a factory, not a runtime.
  Cases share nothing (journal, tool state, scripted model counters, circuit
  breakers), so one case cannot leak into another and cases can run
  concurrently without changing each other's results.
* **Scores come from the journal.** After each run the runner reads the
  run's events and extracts a :class:`~cairn.eval.trajectory.Trajectory`;
  tool-selection, safety and recovery metrics are computed from what the
  runtime recorded, not from what the agent claims.
* **Results are tied to code and data.** Each experiment file records the
  dataset content hash, the git commit (when available), Python version,
  platform, timestamp and the runner configuration.
* **Regression gates are explicit.** :func:`compare` reports per-metric deltas
  with a direction (higher or lower is better) and a tolerance; the
  :meth:`Comparison.exit_code` helper turns that into a CI exit status.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import inspect
import json
import os
import platform
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from cairn.core.errors import error_payload
from cairn.core.ids import stable_hash
from cairn.eval.dataset import Dataset, EvalCase
from cairn.eval.judge import LLMJudge
from cairn.eval.metrics import (
    aggregate_usage,
    contains_match,
    exact_match,
    groundedness,
    latency_summary,
    mean,
    token_f1,
    tool_selection,
    trajectory_metrics,
    unsupported_claim_rate,
)
from cairn.eval.trajectory import Trajectory, check_trajectory, extract_trajectory
from cairn.journal.events import Event
from cairn.runtime.engine import RunResult, Runtime
from cairn.runtime.plan import Plan

RuntimeFactory = Callable[[EvalCase], Runtime | Awaitable[Runtime]]
PlanFactory = Callable[[EvalCase], Plan]
MetricMap = Mapping[str, float | None]

#: Trajectory counters copied into every case's metrics (averaged in aggregates).
TRAJECTORY_KEYS = (
    "model_calls", "tool_calls", "retries", "fallbacks_used", "recovered_failures",
    "approvals_requested", "policy_denials",
)

#: Metrics where a smaller value is better; everything else is higher-is-better.
LOWER_IS_BETTER = frozenset({
    "unsupported_claim_rate", "model_calls", "tool_calls", "retries", "fallbacks_used",
    "approvals_requested", "policy_denials", "cost_usd", "tokens", "failed_effects",
    "attack_success_rate", "error_rate",
})


@dataclass
class CaseContext:
    """Everything a scorer may look at for one case."""

    case: EvalCase
    runtime: Runtime | None
    result: RunResult | None
    events: list[Event]
    trajectory: Trajectory | None
    latency_s: float


Scorer = Callable[[CaseContext], MetricMap | Awaitable[MetricMap]]


@dataclass
class CaseResult:
    case_id: str
    tags: list[str]
    status: str
    passed: bool
    metrics: dict[str, float | None]
    failures: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None
    latency_s: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None
    trajectory: dict[str, Any] | None = None
    judge: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExperimentResult:
    experiment_id: str
    name: str
    created_at: str
    dataset: dict[str, Any]
    environment: dict[str, Any]
    config: dict[str, Any]
    aggregate: dict[str, float | None]
    usage: dict[str, Any]
    latency_s: dict[str, Any]
    cases: list[CaseResult]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["schema"] = "cairn.eval.experiment/1"
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ExperimentResult:
        cases = [CaseResult(**c) for c in data.get("cases", [])]
        return cls(
            experiment_id=data["experiment_id"], name=data["name"], created_at=data["created_at"],
            dataset=dict(data.get("dataset", {})), environment=dict(data.get("environment", {})),
            config=dict(data.get("config", {})), aggregate=dict(data.get("aggregate", {})),
            usage=dict(data.get("usage", {})), latency_s=dict(data.get("latency_s", {})),
            cases=cases,
        )


# ------------------------------------------------------------------ environment


def git_commit(cwd: str | Path | None = None) -> str | None:
    """Current ``git rev-parse HEAD``, or ``None`` outside a repository or without git."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = out.stdout.strip()
    return commit if out.returncode == 0 and commit else None


def git_dirty(cwd: str | Path | None = None) -> bool | None:
    """Whether the working tree has uncommitted changes (``None`` if unknown)."""
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True, timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return bool(out.stdout.strip()) if out.returncode == 0 else None


def environment_info(cwd: str | Path | None = None) -> dict[str, Any]:
    """Facts about where a result was produced, recorded in every result file."""
    return {
        "git_commit": git_commit(cwd),
        "git_dirty": git_dirty(cwd),
        "python": sys.version.split()[0],
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor() or None,
        "cpu_count": _cpu_count(),
    }


def _cpu_count() -> int | None:
    return os.cpu_count()


def utc_now() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------- scoring


def default_scores(ctx: CaseContext) -> tuple[dict[str, float | None], list[str]]:
    """Score a case against its declarative :class:`Expectations`.

    Returns ``(metrics, failures)``. A metric is only emitted when the case
    declares the expectation it measures, so aggregates average over the
    cases where a metric is meaningful.
    """
    exp = ctx.case.expectations
    metrics: dict[str, float | None] = {}
    failures: list[str] = []
    status = ctx.result.status if ctx.result else "error"
    output = ctx.result.output if ctx.result else None
    if exp.status is not None:
        ok = status == exp.status
        metrics["status_ok"] = float(ok)
        if not ok:
            failures.append(f"status '{status}' != expected '{exp.status}'")
    if exp.output_equals is not None:
        em = exact_match(output, exp.output_equals)
        metrics["exact_match"] = em
        if em < 1:
            failures.append("output does not equal the expected value")
    if exp.output_contains:
        cm = contains_match(output, exp.output_contains)
        metrics["contains"] = cm
        if cm < 1:
            failures.append(f"output is missing some of {exp.output_contains}")
    if exp.reference is not None:
        f1 = token_f1(output, exp.reference)
        metrics["token_f1"] = f1
        if exp.min_f1 is not None and f1 < exp.min_f1:
            failures.append(f"token F1 {f1:.3f} < {exp.min_f1}")
    traj = ctx.trajectory
    if traj is not None:
        if exp.expected_tools is not None:
            prf = tool_selection(exp.expected_tools, traj.tools)
            metrics["tool_precision"] = prf.precision
            metrics["tool_recall"] = prf.recall
            metrics["tool_f1"] = prf.f1
        has_traj_expectation = (
            exp.tool_sequence is not None or bool(exp.forbidden_tools)
            or exp.max_model_calls is not None or exp.max_tool_calls is not None
        )
        if has_traj_expectation:
            problems = check_trajectory(
                traj, tool_sequence=exp.tool_sequence, forbidden_tools=exp.forbidden_tools,
                max_model_calls=exp.max_model_calls, max_tool_calls=exp.max_tool_calls,
            )
            metrics["trajectory_ok"] = float(not problems)
            failures.extend(problems)
        counters = trajectory_metrics(ctx.events)
        for key in TRAJECTORY_KEYS:
            metrics[key] = float(counters[key])
    if exp.evidence:
        g = groundedness(output, exp.evidence)
        metrics["groundedness"] = g
        metrics["unsupported_claim_rate"] = unsupported_claim_rate(output, exp.evidence)
        if exp.min_groundedness is not None and (g is None or g < exp.min_groundedness):
            failures.append(f"groundedness {g} < {exp.min_groundedness}")
    return metrics, failures


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


class EvalRunner:
    """Run a :class:`Dataset` against fresh runtimes and collect scored results.

    ``plan_factory`` defaults to validating ``case.plan`` as Plan IR.
    ``run_kwargs`` lets a caller pass per-case ``Runtime.run`` options (budget,
    grants, label). Extra ``scorers`` add metrics; a scorer returning a key
    named ``passed`` with value 0 fails the case.
    """

    def __init__(
        self,
        runtime_factory: RuntimeFactory,
        *,
        name: str = "eval",
        plan_factory: PlanFactory | None = None,
        scorers: Sequence[Scorer] = (),
        judge: LLMJudge | None = None,
        concurrency: int = 4,
        case_timeout_s: float | None = 120.0,
        config: Mapping[str, Any] | None = None,
        run_kwargs: Callable[[EvalCase], dict[str, Any]] | None = None,
        repo_dir: str | Path | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        self.runtime_factory = runtime_factory
        self.name = name
        self.plan_factory = plan_factory or _plan_from_case
        self.scorers = list(scorers)
        self.judge = judge
        self.concurrency = concurrency
        self.case_timeout_s = case_timeout_s
        self.config = dict(config or {})
        self.run_kwargs = run_kwargs
        self.repo_dir = repo_dir

    async def run_case(self, case: EvalCase) -> CaseResult:
        started = time.perf_counter()
        runtime: Runtime | None = None
        result: RunResult | None = None
        events: list[Event] = []
        error: dict[str, Any] | None = None
        try:
            runtime = await _maybe_await(self.runtime_factory(case))
            assert runtime is not None
            plan = self.plan_factory(case)
            kwargs = {"inputs": case.inputs, **(self.run_kwargs(case) if self.run_kwargs else {})}
            coro = runtime.run(plan, **kwargs)
            result = await (asyncio.wait_for(coro, self.case_timeout_s)
                            if self.case_timeout_s else coro)
            events = await runtime.journal.read(result.run_id)
        except Exception as exc:
            error = error_payload(exc)
        latency = time.perf_counter() - started
        traj = extract_trajectory(events, result.run_id) if result is not None else None
        ctx = CaseContext(case, runtime, result, events, traj, latency)
        metrics, failures = default_scores(ctx)
        if error is not None:
            failures.insert(0, f"case raised {error.get('code')}: {error.get('message')}")
        judge_payload: dict[str, Any] | None = None
        if self.judge is not None and case.expectations.judge and result is not None:
            verdict = await self.judge.judge(
                case.goal or (case.plan or {}).get("goal", ""), result.output,
                reference=case.expectations.reference, evidence=case.expectations.evidence or None,
            )
            judge_payload = verdict.to_dict()
            metrics["judge_score"] = verdict.score
            if not verdict.passed:
                failures.append(f"judge: {verdict.error or '; '.join(verdict.reasons) or 'failed'}")
        for scorer in self.scorers:
            try:
                extra = dict(await _maybe_await(scorer(ctx)))
            except Exception as exc:
                failures.append(f"scorer {getattr(scorer, '__name__', scorer)!r} raised: {exc}")
                continue
            if extra.get("passed") == 0:
                failures.append(f"scorer {getattr(scorer, '__name__', 'custom')} failed the case")
            metrics.update(extra)
        return CaseResult(
            case_id=case.id,
            tags=list(case.tags),
            status=result.status if result else "error",
            passed=not failures,
            metrics=metrics,
            failures=failures,
            error=error or (result.error if result else None),
            latency_s=latency,
            usage=dict(result.usage) if result else {},
            run_id=result.run_id if result else None,
            trajectory=traj.summary() if traj else None,
            judge=judge_payload,
        )

    async def run(self, dataset: Dataset) -> ExperimentResult:
        sem = asyncio.Semaphore(self.concurrency)

        async def guarded(case: EvalCase) -> CaseResult:
            async with sem:
                return await self.run_case(case)

        cases = list(await asyncio.gather(*(guarded(c) for c in dataset.cases)))
        return self._assemble(dataset, cases)

    def _assemble(self, dataset: Dataset, cases: list[CaseResult]) -> ExperimentResult:
        config = {
            "concurrency": self.concurrency,
            "case_timeout_s": self.case_timeout_s,
            "judge": self.judge.rubric.name if self.judge else None,
            "scorers": [getattr(s, "__name__", repr(s)) for s in self.scorers],
            **self.config,
        }
        created = utc_now()
        exp_id = f"{self.name}-{created.replace(':', '').replace('-', '')}-" + stable_hash(
            {"dataset": dataset.hash, "config": config, "created": created}, length=6
        )
        return ExperimentResult(
            experiment_id=exp_id,
            name=self.name,
            created_at=created,
            dataset=dataset.ref(),
            environment=environment_info(self.repo_dir),
            config=config,
            aggregate=aggregate_cases(cases),
            usage=aggregate_usage(c.usage for c in cases if c.usage),
            latency_s=latency_summary([c.latency_s for c in cases]),
            cases=cases,
        )


def _plan_from_case(case: EvalCase) -> Plan:
    if case.plan is None:
        raise ValueError(f"case '{case.id}' has no plan; pass plan_factory to EvalRunner")
    return Plan.model_validate(case.plan)


def aggregate_cases(cases: Sequence[CaseResult]) -> dict[str, float | None]:
    """Pass rate, error rate, mean of every metric (skipping undefined), latency percentiles."""
    agg: dict[str, float | None] = {
        "cases": float(len(cases)),
        "pass_rate": mean(float(c.passed) for c in cases),
        "error_rate": mean(float(c.status == "error") for c in cases),
    }
    keys = sorted({k for c in cases for k in c.metrics})
    for key in keys:
        agg[key] = mean(c.metrics.get(key) for c in cases)
    lat = latency_summary([c.latency_s for c in cases])
    agg["latency_p50_s"] = lat["p50"]
    agg["latency_p95_s"] = lat["p95"]
    return agg


# ------------------------------------------------------------- persistence


def save_experiment(result: ExperimentResult, results_dir: str | Path) -> dict[str, str]:
    """Write ``<id>.json`` and ``<id>.md`` and add the experiment to ``index.json``.

    The index is the experiment log: one entry per saved run with its dataset
    identity and headline numbers, so a history of results can be browsed and
    compared without opening every file.
    """
    root = Path(results_dir)
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / f"{result.experiment_id}.json"
    md_path = root / f"{result.experiment_id}.md"
    json_path.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True, default=str) + "\n",
                         encoding="utf-8")
    md_path.write_text(render_markdown(result), encoding="utf-8")
    index_path = root / "index.json"
    index: dict[str, Any] = {"experiments": []}
    if index_path.exists():
        index = json.loads(index_path.read_text(encoding="utf-8"))
    entries = [e for e in index.get("experiments", []) if e.get("id") != result.experiment_id]
    entries.append({
        "id": result.experiment_id,
        "name": result.name,
        "created_at": result.created_at,
        "dataset": result.dataset.get("name"),
        "dataset_version": result.dataset.get("version"),
        "dataset_hash": result.dataset.get("hash"),
        "git_commit": result.environment.get("git_commit"),
        "pass_rate": result.aggregate.get("pass_rate"),
        "cases": len(result.cases),
        "json": json_path.name,
        "markdown": md_path.name,
    })
    index["experiments"] = entries
    index_path.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"json": str(json_path), "markdown": str(md_path), "index": str(index_path)}


def load_experiment(path: str | Path) -> ExperimentResult:
    return ExperimentResult.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def list_experiments(results_dir: str | Path) -> list[dict[str, Any]]:
    index_path = Path(results_dir) / "index.json"
    if not index_path.exists():
        return []
    return list(json.loads(index_path.read_text(encoding="utf-8")).get("experiments", []))


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(result: ExperimentResult) -> str:
    ds = result.dataset
    env = result.environment
    lines = [
        f"# Experiment `{result.experiment_id}`",
        "",
        f"- Name: {result.name}",
        f"- Created: {result.created_at}",
        f"- Dataset: {ds.get('name')} v{ds.get('version')} ({ds.get('rows')} cases), "
        f"sha256 `{ds.get('hash')}`",
        f"- Git commit: `{env.get('git_commit')}` (dirty: {env.get('git_dirty')})",
        f"- Python {env.get('python')} on {env.get('platform')}",
        f"- Config: `{json.dumps(result.config, sort_keys=True, default=str)}`",
        "",
        "## Aggregate",
        "",
        "| metric | value |",
        "|---|---|",
    ]
    lines += [f"| {k} | {_fmt(v)} |" for k, v in result.aggregate.items()]
    usage = result.usage
    lines += [
        "",
        f"Usage: {usage.get('model_calls', 0)} model calls, {usage.get('tokens', 0)} tokens "
        f"(estimated where providers do not report), cost "
        f"{_fmt(usage.get('cost_usd'))} USD (complete: {usage.get('cost_complete')}).",
        "",
        "## Cases",
        "",
        "| case | status | passed | latency s | failures |",
        "|---|---|---|---|---|",
    ]
    for c in result.cases:
        lines.append(
            f"| {_cell(c.case_id)} | {c.status} | {'yes' if c.passed else 'no'} | "
            f"{c.latency_s:.4f} | {_cell('; '.join(c.failures)) or '-'} |"
        )
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------- comparison


@dataclass
class MetricDelta:
    metric: str
    baseline: float | None
    candidate: float | None
    delta: float | None
    direction: Literal["higher", "lower"]
    tolerance: float | None
    regressed: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Comparison:
    baseline_id: str
    candidate_id: str
    dataset_match: bool
    deltas: list[MetricDelta]

    @property
    def regressions(self) -> list[MetricDelta]:
        return [d for d in self.deltas if d.regressed]

    def exit_code(self, *, require_same_dataset: bool = True) -> int:
        """0 when nothing regressed (and datasets match, if required), else 1."""
        if require_same_dataset and not self.dataset_match:
            return 1
        return 1 if self.regressions else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline_id,
            "candidate": self.candidate_id,
            "dataset_match": self.dataset_match,
            "regressions": [d.metric for d in self.regressions],
            "deltas": [d.to_dict() for d in self.deltas],
        }

    def to_markdown(self) -> str:
        lines = [
            f"# Comparison: `{self.baseline_id}` -> `{self.candidate_id}`",
            "",
            f"Dataset identical: {self.dataset_match}",
            "",
            "| metric | baseline | candidate | delta | better | tolerance | regressed |",
            "|---|---|---|---|---|---|---|",
        ]
        for d in self.deltas:
            lines.append(
                f"| {d.metric} | {_fmt(d.baseline)} | {_fmt(d.candidate)} | {_fmt(d.delta)} | "
                f"{d.direction} | {_fmt(d.tolerance)} | {'YES' if d.regressed else 'no'} |"
            )
        return "\n".join(lines) + "\n"


def compare(
    baseline: ExperimentResult,
    candidate: ExperimentResult,
    *,
    thresholds: Mapping[str, float] | None = None,
    default_tolerance: float = 0.0,
    directions: Mapping[str, Literal["higher", "lower"]] | None = None,
) -> Comparison:
    """Per-metric deltas (candidate minus baseline) with regression flags.

    A metric regresses when it moves in the worse direction by more than its
    tolerance (``thresholds[metric]``, else ``default_tolerance``). Latency
    metrics are wall-clock and machine dependent, so they are reported but
    never flagged unless a threshold for them is given explicitly.
    """
    thresholds = dict(thresholds or {})
    directions = dict(directions or {})
    keys = sorted(set(baseline.aggregate) | set(candidate.aggregate))
    deltas: list[MetricDelta] = []
    for key in keys:
        if key == "cases":
            continue
        a = baseline.aggregate.get(key)
        b = candidate.aggregate.get(key)
        direction: Literal["higher", "lower"] = directions.get(
            key, "lower" if key in LOWER_IS_BETTER or key.startswith("latency") else "higher"
        )
        delta = (b - a) if (a is not None and b is not None) else None
        gated = key in thresholds or not key.startswith("latency")
        tolerance = thresholds.get(key, default_tolerance) if gated else None
        regressed = False
        if gated and delta is not None and tolerance is not None:
            worse = -delta if direction == "higher" else delta
            regressed = worse > tolerance + 1e-12
        elif gated and a is not None and b is None:
            regressed = True  # a metric that disappeared cannot be assumed fine
        deltas.append(MetricDelta(key, a, b, delta, direction, tolerance, regressed))
    return Comparison(
        baseline.experiment_id,
        candidate.experiment_id,
        baseline.dataset.get("hash") == candidate.dataset.get("hash"),
        deltas,
    )


def regression_exit_code(
    baseline: ExperimentResult | str | Path,
    candidate: ExperimentResult | str | Path,
    **kwargs: Any,
) -> int:
    """Convenience for CI scripts: ``sys.exit(regression_exit_code(a_path, b_path))``."""
    a = baseline if isinstance(baseline, ExperimentResult) else load_experiment(baseline)
    b = candidate if isinstance(candidate, ExperimentResult) else load_experiment(candidate)
    return compare(a, b, **kwargs).exit_code()
