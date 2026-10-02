"""Deterministic evaluation metrics.

Every metric here is a pure function of its inputs: no model calls, no
randomness, no clock. That is deliberate. Agent evaluations are noisy enough
already (sampling, provider drift); the scoring layer must not add noise, so
that a change in a reported number always means a change in system behavior.

Conventions:

* Scores are floats in ``[0, 1]`` unless documented otherwise.
* Functions return ``None`` when a metric is undefined for the input (for
  example groundedness of an empty answer). Aggregation skips ``None`` rather
  than treating it as zero, so undefined is never confused with "bad".
* Text metrics use :func:`cairn.retrieval.text.terms` (lowercased, stopwords
  removed, lightly stemmed) so that "Emails" and "email" agree, matching how
  retrieval itself tokenizes.
"""

from __future__ import annotations

import json
import math
import re
import string
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cairn.journal.events import Event, EventType
from cairn.retrieval.text import terms

# --------------------------------------------------------------------- text

_PUNCT = str.maketrans("", "", string.punctuation)
_WS = re.compile(r"\s+")
_ARTICLES = re.compile(r"\b(a|an|the)\b")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def normalize_answer(text: str) -> str:
    """SQuAD-style normalization: lowercase, strip punctuation, articles and extra spaces."""
    text = text.lower().translate(_PUNCT)
    text = _ARTICLES.sub(" ", text)
    return _WS.sub(" ", text).strip()


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def exact_match(prediction: Any, gold: Any) -> float:
    """1.0 when the normalized prediction equals the normalized gold answer.

    Non-string values are compared structurally first (so ``{"a": 1}`` matches
    ``{"a": 1}`` regardless of key order), then as normalized text.
    """
    if (not isinstance(prediction, str) or not isinstance(gold, str)) and prediction == gold:
        return 1.0
    return float(normalize_answer(_as_text(prediction)) == normalize_answer(_as_text(gold)))


def contains_match(prediction: Any, needles: str | Iterable[str]) -> float:
    """Fraction of ``needles`` that occur (normalized) inside the prediction.

    A single string is treated as one needle, so the result is 0.0 or 1.0.
    Returning a fraction for lists gives partial credit that still reaches 1.0
    only when every required fact is present.
    """
    items = [needles] if isinstance(needles, str) else list(needles)
    if not items:
        return 1.0
    haystack = normalize_answer(_as_text(prediction))
    hits = sum(1 for n in items if normalize_answer(n) in haystack)
    return hits / len(items)


def token_f1(prediction: Any, gold: Any) -> float:
    """Bag-of-tokens F1 between prediction and gold (SQuAD definition).

    Uses multiset overlap so repeating a correct word does not inflate recall.
    Two empty strings score 1.0; exactly one empty string scores 0.0.
    """
    pred = normalize_answer(_as_text(prediction)).split()
    ref = normalize_answer(_as_text(gold)).split()
    if not pred and not ref:
        return 1.0
    if not pred or not ref:
        return 0.0
    common = Counter(pred) & Counter(ref)
    overlap = sum(common.values())
    if overlap == 0:
        return 0.0
    precision = overlap / len(pred)
    recall = overlap / len(ref)
    return 2 * precision * recall / (precision + recall)


# ---------------------------------------------------------------- retrieval


def recall_at_k(retrieved: Sequence[str], relevant: Collection[str], k: int) -> float | None:
    """Fraction of relevant ids that appear in the top ``k`` results.

    Undefined (``None``) when there are no relevant ids, because a query
    without relevant documents cannot distinguish good and bad retrievers.
    """
    if not relevant:
        return None
    top = set(retrieved[:k])
    return sum(1 for r in relevant if r in top) / len(relevant)


def precision_at_k(retrieved: Sequence[str], relevant: Collection[str], k: int) -> float:
    """Fraction of the top ``k`` slots holding a relevant id.

    The denominator is ``k`` even when fewer than ``k`` results were returned:
    returning nothing should not look precise.
    """
    if k <= 0:
        raise ValueError("k must be positive")
    rel = set(relevant)
    return sum(1 for r in retrieved[:k] if r in rel) / k


def reciprocal_rank(retrieved: Sequence[str], relevant: Collection[str]) -> float:
    """1 / rank of the first relevant result, or 0.0 if none is retrieved."""
    rel = set(relevant)
    for rank, item in enumerate(retrieved, start=1):
        if item in rel:
            return 1.0 / rank
    return 0.0


def mean_reciprocal_rank(
    runs: Iterable[tuple[Sequence[str], Collection[str]]],
) -> float | None:
    """MRR over ``(retrieved, relevant)`` pairs; ``None`` for an empty input."""
    values = [reciprocal_rank(r, g) for r, g in runs]
    return sum(values) / len(values) if values else None


def ndcg_at_k(
    retrieved: Sequence[str],
    relevance: Mapping[str, float] | Collection[str],
    k: int,
) -> float | None:
    """Normalized discounted cumulative gain with linear gain.

    ``DCG = sum(rel_i / log2(i + 1))`` for ranks ``i = 1..k``; the ideal DCG
    sorts all judged documents by grade. ``relevance`` may be a mapping of
    graded judgments or a plain collection (every member has grade 1).
    Linear rather than exponential gain keeps grade 2 "twice as useful" as
    grade 1, which is how the bundled datasets are labeled.
    """
    grades: dict[str, float] = (
        {str(key): float(val) for key, val in relevance.items()}
        if isinstance(relevance, Mapping)
        else {str(item): 1.0 for item in relevance}
    )
    grades = {key: val for key, val in grades.items() if val > 0}
    if not grades:
        return None
    dcg = sum(
        grades.get(item, 0.0) / math.log2(rank + 1)
        for rank, item in enumerate(retrieved[:k], start=1)
    )
    ideal = sorted(grades.values(), reverse=True)[:k]
    idcg = sum(g / math.log2(rank + 1) for rank, g in enumerate(ideal, start=1))
    return dcg / idcg if idcg else None


# ----------------------------------------------------------- tool selection


@dataclass(frozen=True)
class PRF:
    """Precision, recall and F1 with the raw counts that produced them."""

    precision: float
    recall: float
    f1: float
    true_positives: int
    false_positives: int
    false_negatives: int

    def to_dict(self) -> dict[str, float | int]:
        return dict(self.__dict__)


def tool_selection(expected: Iterable[str], actual: Iterable[str]) -> PRF:
    """Set-based precision/recall/F1 of the tools an agent actually invoked.

    Set semantics on purpose: calling ``search`` three times is a retrieval
    strategy, not three selection decisions. Use trajectory matchers when the
    order or the count of calls matters. Empty expected and empty actual is a
    perfect score (the agent correctly used no tools).
    """
    exp, act = set(expected), set(actual)
    tp = len(exp & act)
    fp = len(act - exp)
    fn = len(exp - act)
    precision = tp / (tp + fp) if (tp + fp) else (1.0 if not exp else 0.0)
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return PRF(precision, recall, f1, tp, fp, fn)


# ------------------------------------------------------------ groundedness


def _support(tokens: Sequence[str], evidence_terms: set[str]) -> float | None:
    if not tokens:
        return None
    return sum(1 for t in tokens if t in evidence_terms) / len(tokens)


def groundedness(answer: Any, evidence: Iterable[Any]) -> float | None:
    """Fraction of the answer's content tokens that occur somewhere in the evidence.

    A cheap lexical proxy, not entailment: it catches answers that introduce
    names, numbers or terms absent from every source (a common hallucination
    shape) but cannot catch a wrong claim assembled from correct words.
    Returns ``None`` for answers with no content tokens.
    """
    evidence_terms: set[str] = set()
    for item in evidence:
        evidence_terms.update(terms(_as_text(item)))
    return _support(terms(_as_text(answer)), evidence_terms)


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text) if s.strip()]


def unsupported_claim_rate(
    answer: Any, evidence: Iterable[Any], threshold: float = 0.5
) -> float | None:
    """Share of answer sentences whose lexical support falls below ``threshold``.

    Each sentence is treated as one claim; its support is the fraction of its
    content tokens found in the evidence. This is a hallucination *proxy*:
    higher is worse. Sentences without content tokens ("Sure.") are ignored.
    """
    evidence_terms: set[str] = set()
    for item in evidence:
        evidence_terms.update(terms(_as_text(item)))
    sentences = split_sentences(_as_text(answer))
    supports = [_support(terms(sent), evidence_terms) for sent in sentences]
    scores = [s for s in supports if s is not None]
    if not scores:
        return None
    return sum(1 for s in scores if s < threshold) / len(scores)


# -------------------------------------------------------------- trajectories


def trajectory_metrics(events: Sequence[Event]) -> dict[str, int]:
    """Behavioral counters derived from a run's journal events.

    Computed from events rather than from ``RunResult`` because the journal
    records *how* a result was reached: retries, fallbacks and approvals leave
    no trace in the final output but matter for cost, latency and safety.

    * ``recovered_failures``: nodes with at least one non-final failed attempt
      that later completed normally (not via ``on_error=default``).
    * ``fallbacks_used``: node attempts that ran a fallback strategy.
    """
    nodes: set[str] = set()
    attempts = 0
    retries = 0
    fallbacks = 0
    failed_attempt_nodes: set[str] = set()
    completed: set[str] = set()
    approvals = 0
    denials = 0
    approval_required = 0
    model_calls = 0
    tool_calls = 0
    failed_effects = 0
    replayed = 0
    verify_failures = 0
    for ev in events:
        t = ev.type
        nid = ev.node_id or ""
        if t == EventType.NODE_STARTED:
            nodes.add(nid)
            attempts += 1
            if ev.data.get("strategy", "primary") != "primary":
                fallbacks += 1
        elif t == EventType.NODE_RETRYING:
            retries += 1
        elif t == EventType.NODE_FAILED and not ev.data.get("final", True):
            failed_attempt_nodes.add(nid)
        elif t == EventType.NODE_COMPLETED and not ev.data.get("defaulted"):
            completed.add(nid)
        elif t == EventType.APPROVAL_REQUESTED and not ev.data.get("copied_from"):
            approvals += 1
        elif t == EventType.POLICY_DECISION:
            verdict = ev.data.get("verdict")
            if verdict == "deny":
                denials += 1
            elif verdict == "require_approval":
                approval_required += 1
        elif t == EventType.EFFECT_COMPLETED:
            if ev.data.get("replayed"):
                replayed += 1
            elif ev.data.get("kind") == "model":
                model_calls += 1
            elif ev.data.get("kind") == "tool":
                tool_calls += 1
        elif t == EventType.EFFECT_FAILED:
            failed_effects += 1
        elif t == EventType.VERIFY_RESULT and not ev.data.get("passed", True):
            verify_failures += 1
    return {
        "node_count": len(nodes),
        "node_attempts": attempts,
        "retries": retries,
        "fallbacks_used": fallbacks,
        "recovered_failures": len(failed_attempt_nodes & completed),
        "approvals_requested": approvals,
        "policy_require_approval": approval_required,
        "policy_denials": denials,
        "model_calls": model_calls,
        "tool_calls": tool_calls,
        "failed_effects": failed_effects,
        "replayed_effects": replayed,
        "verify_failures": verify_failures,
        "events": len(events),
    }


# -------------------------------------------------------- latency and usage


def percentile(values: Sequence[float], q: float) -> float | None:
    """Percentile with linear interpolation between closest ranks (numpy default).

    ``q`` is in ``[0, 100]``. ``None`` for an empty input.
    """
    if not values:
        return None
    if not 0 <= q <= 100:
        raise ValueError("q must be within [0, 100]")
    data = sorted(values)
    if len(data) == 1:
        return float(data[0])
    pos = (len(data) - 1) * q / 100
    lo = math.floor(pos)
    hi = math.ceil(pos)
    return float(data[lo] + (data[hi] - data[lo]) * (pos - lo))


def latency_summary(values: Sequence[float]) -> dict[str, float | int | None]:
    """count, mean, min, p50, p95, max of a latency sample (seconds or ms, unit-agnostic)."""
    if not values:
        return {"count": 0, "mean": None, "min": None, "p50": None, "p95": None, "max": None}
    return {
        "count": len(values),
        "mean": sum(values) / len(values),
        "min": float(min(values)),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "max": float(max(values)),
    }


def aggregate_usage(usages: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Sum ``Usage.to_dict()`` payloads with honest cost reporting.

    ``Usage`` records unpriced model calls as cost 0 plus an
    ``unpriced_calls`` counter. Summing those zeros would claim the work was
    free, so the aggregate reports ``cost_usd = None`` when *no* call was
    priced, and ``cost_complete = False`` when only some were.
    """
    totals: dict[str, Any] = {
        "runs": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "tokens": 0,
        "model_calls": 0,
        "tool_calls": 0,
        "retrieval_calls": 0,
        "memory_ops": 0,
        "replayed_effects": 0,
        "unpriced_calls": 0,
    }
    priced_cost = 0.0
    for usage in usages:
        totals["runs"] += 1
        for key in list(totals):
            if key == "runs":
                continue
            totals[key] += int(usage.get(key, 0) or 0)
        cost = usage.get("cost_usd")
        if cost is not None:
            priced_cost += float(cost)
    priced_calls = totals["model_calls"] - totals["unpriced_calls"]
    if totals["model_calls"] == 0:
        totals["cost_usd"] = 0.0
        totals["cost_complete"] = True
    elif priced_calls <= 0:
        totals["cost_usd"] = None
        totals["cost_complete"] = False
    else:
        totals["cost_usd"] = round(priced_cost, 6)
        totals["cost_complete"] = totals["unpriced_calls"] == 0
    return totals


def mean(values: Iterable[float | int | None]) -> float | None:
    """Mean that skips ``None`` (undefined) entries; ``None`` if nothing is defined."""
    data = [float(v) for v in values if v is not None]
    return sum(data) / len(data) if data else None
