"""MemoryManager: the multi-layer memory service used by the runtime and agents.

What is stored
--------------
Every memory is a :class:`~cairn.memory.types.MemoryRecord` holding: the text,
its kind (layer), its provenance label (integrity, sources, secrecy tags), an
importance score in [0, 1], creation and last-access timestamps, an access
count, a ``pinned`` flag, the id of the run that wrote it (``source_run``),
free-form metadata, the text embedding (so the index can be rebuilt without
re-embedding) and, once consolidated, ``superseded_by``. Text is stored exactly
as given; the executor redacts secrets before calling ``remember``.

The layers and why they exist:

* ``working``: per-session scratchpad held in process (:class:`WorkingMemory`,
  never persisted) plus optional short-lived persisted notes. It lets an agent
  carry context between steps under a token budget.
* ``session``: facts for one conversation or run family (hours).
* ``episodic``: one record per finished run, written by ``record_episode``
  (goal, status, outcome). Agents use it to avoid repeating failed approaches.
* ``semantic``: durable facts, written directly or produced by
  ``consolidate`` from clusters of similar episodes.
* ``procedural``: plans that succeeded, keyed by goal (``save_procedure``),
  returned by ``find_procedures`` as planner few-shot examples.

Provenance rules
----------------
Memory crosses run boundaries, so it is the easiest place to launder injected
content. Therefore: procedural and pinned writes require a trusted label
(``PolicyViolation`` otherwise, unless ``allow_untrusted_procedural``); other
untrusted writes are stored with an ``untrusted_source`` flag and a capped
importance; every recall result carries its label so the executor re-taints
whatever consumes it; deduplication and consolidation *join* labels, never
drop them.

Retention rules
---------------
* Strength decays exponentially from the last access, with per-kind
  half-lives (working 15 min, session 1 day, episodic 14 days, semantic 90
  days, procedural 365 days by default), scaled by importance and reinforced
  by access count. ``expire`` deletes non-pinned records whose strength is
  below the threshold (0.05 by default); recall already ignores them.
* Pinned records (trusted only) never decay or expire.
* Consolidated episodes are kept, marked ``superseded_by``, excluded from
  recall, and expire on the normal schedule for audit purposes.
* ``delete``, ``forget(source_run=...)`` and ``forget(kind=...)`` remove
  records immediately and unconditionally (pinned included), for user requests
  and for purging everything a compromised run wrote.
* Near-duplicates (cosine >= ``dedup_threshold`` within a kind) are merged
  into the existing record instead of being stored twice.
"""

from __future__ import annotations

import asyncio
import builtins
import json
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from cairn.core.clock import Clock, SystemClock
from cairn.core.errors import NotFound
from cairn.core.ids import new_id
from cairn.memory.policy import (
    FLAG_UNTRUSTED,
    DecayPolicy,
    RetrievalScorer,
    ScoredMemory,
    WritePolicy,
    score_importance,
)
from cairn.memory.store import InMemoryMemoryStore, MemoryStore, SQLiteMemoryStore
from cairn.memory.types import MemoryKind, MemoryRecord
from cairn.memory.working import WorkingMemory
from cairn.provenance.labels import Label, join_all
from cairn.retrieval.embeddings import Embedder, HashingEmbedder, cosine
from cairn.retrieval.index import HybridIndex
from cairn.retrieval.text import terms
from cairn.storage.sqlite import SQLiteDatabase

#: Async callable turning a cluster of episodes into one summary text (for LLM summaries).
Summarizer = Callable[[Sequence[MemoryRecord]], Awaitable[str]]

ANY_KIND = "any"
#: RRF over two rankings scores at most 2 / (60 + 1); dividing by it maps relevance to [0, 1].
_MAX_RRF = 2.0 / 61.0
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


def extractive_summary(texts: Sequence[str], max_sentences: int = 3) -> str:
    """Deterministic summary: the sentences whose terms recur most across ``texts``.

    Terms are counted once per source text, so a sentence scores highly when
    it states what the cluster has in common rather than what one episode
    repeated. Picked sentences keep their original order.
    """
    doc_freq: Counter[str] = Counter()
    for text in texts:
        doc_freq.update(set(terms(text)))
    candidates: list[tuple[float, int, str]] = []
    seen: set[str] = set()
    position = 0
    for text in texts:
        for raw in _SENTENCE.split(text):
            sentence = raw.strip()
            key = " ".join(terms(sentence))
            if not sentence or not key or key in seen:
                continue
            seen.add(key)
            sentence_terms = set(terms(sentence))
            score = sum(doc_freq[t] for t in sentence_terms) / (1 + len(sentence_terms)) ** 0.5
            candidates.append((score, position, sentence))
            position += 1
    best = sorted(candidates, key=lambda c: (-c[0], c[1]))[:max_sentences]
    return " ".join(s for _, _, s in sorted(best, key=lambda c: c[1]))


class MemoryManager:
    """Implements the runtime's ``MemoryService`` on a store plus a hybrid index.

    The index holds only active (non-superseded) records and is rebuilt from
    the store on first use, so the store stays the single source of truth.
    """

    def __init__(
        self,
        store: MemoryStore | None = None,
        *,
        embedder: Embedder | None = None,
        clock: Clock | None = None,
        write_policy: WritePolicy | None = None,
        decay: DecayPolicy | None = None,
        scorer: RetrievalScorer | None = None,
        summarizer: Summarizer | None = None,
        dedup_threshold: float = 0.92,
        consolidation_threshold: float = 0.5,
        min_relevance: float = 0.1,
        working_max_items: int = 64,
    ) -> None:
        self.store: MemoryStore = store if store is not None else InMemoryMemoryStore()
        self.embedder: Embedder = embedder or HashingEmbedder()
        self.clock: Clock = clock or SystemClock()
        self.write_policy = write_policy or WritePolicy()
        self.decay_policy = decay or (scorer.decay if scorer else DecayPolicy())
        self.scorer = scorer or RetrievalScorer(decay=self.decay_policy)
        self.summarizer = summarizer
        self.dedup_threshold = dedup_threshold
        self.consolidation_threshold = consolidation_threshold
        self.min_relevance = min_relevance
        self.working_max_items = working_max_items
        self.index = HybridIndex(self.embedder)
        self._ready = False
        self._lock = asyncio.Lock()
        self._working: dict[str, WorkingMemory] = {}

    @classmethod
    async def open(cls, db: SQLiteDatabase | str, **options: Any) -> MemoryManager:
        """Create a manager on a SQLite store and rebuild its index."""
        manager = cls(SQLiteMemoryStore(db), **options)
        await manager.start()
        return manager

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Rebuild the search index from the store (idempotent)."""
        if self._ready:
            return
        async with self._lock:
            await self._rebuild_index()

    async def _rebuild_index(self) -> None:
        """Load every active record into the index once; caller holds the lock."""
        if not self._ready:
            for record in await self.store.list(include_superseded=False):
                await self._index_add(record)
            self._ready = True

    async def _index_add(self, record: MemoryRecord) -> None:
        """Index a record, reusing its stored embedding when it came from this embedder."""
        if record.embedding is None or record.metadata.get("embedder") != self.embedder.name:
            await self.index.add(record.id, record.text, {"kind": record.kind.value})
            return
        self.index.texts[record.id] = record.text
        self.index.meta[record.id] = {"kind": record.kind.value}
        self.index.lexical.add(record.id, record.text)
        await self.index.vectors.add(record.id, record.embedding)

    # ------------------------------------------------------- MemoryService

    async def recall(self, query: str, kind: str = ANY_KIND, k: int = 5) -> list[dict[str, Any]]:
        """Top ``k`` memories for ``query`` as dicts carrying ``label``, ``score`` and parts.

        ``kind`` is a memory kind or ``"any"``. Recalling counts as an access,
        which refreshes recency and reinforces the record against decay.
        """
        scored = await self.search(query, kind, k)
        return [self._result(s) for s in scored]

    async def remember(
        self,
        text: str,
        kind: str | MemoryKind,
        label: Label,
        importance: float | None = None,
        source_run: str | None = None,
        *,
        pinned: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Store ``text`` (or merge it into a near-duplicate) and return the record dict.

        Raises :class:`~cairn.core.errors.PolicyViolation` when the label is
        not allowed for this kind or for pinning.
        """
        await self.start()
        async with self._lock:
            record, deduplicated = await self._write(
                text, MemoryKind.parse(kind), label, importance, source_run, pinned, metadata
            )
        result: dict[str, Any] = record.to_dict()
        result["deduplicated"] = deduplicated
        return result

    # ------------------------------------------------------------ retrieval

    async def search(
        self, query: str, kind: str | MemoryKind = ANY_KIND, k: int = 5, *, touch: bool = True
    ) -> builtins.list[ScoredMemory]:
        """Ranked records with score breakdowns; ``touch`` records the access."""
        await self.start()
        wanted = None if kind == ANY_KIND else MemoryKind.parse(kind)
        if k <= 0 or not query.strip() or not len(self.index):
            return []
        now = self.clock.now()
        hits = await self.index.search(query, k=max(50, k * 10), candidates=max(50, k * 10))
        scored: builtins.list[ScoredMemory] = []
        for hit in hits:
            if wanted is not None and self.index.meta.get(hit.id, {}).get("kind") != wanted.value:
                continue
            lexical_hit = hit.parts.get("bm25", 0.0) > 0.0
            if not lexical_hit and hit.parts.get("dense", 0.0) < self.min_relevance:
                continue
            record = await self.store.get(hit.id)
            if record is None or not record.active or self.decay_policy.is_expired(record, now):
                continue
            relevance = min(1.0, hit.score / _MAX_RRF)
            scored.append(self.scorer.score(record, relevance, now))
        scored.sort(key=lambda s: (-s.score, s.record.id))
        top = scored[:k]
        if touch:
            async with self._lock:
                for item in top:
                    item.record.access_count += 1
                    item.record.last_accessed = now
                    await self.store.put(item.record)
        return top

    @staticmethod
    def _result(scored: ScoredMemory) -> dict[str, Any]:
        data: dict[str, Any] = scored.record.to_dict()
        data["score"] = scored.score
        data["score_parts"] = {k: round(v, 6) for k, v in scored.parts.items()}
        return data

    # --------------------------------------------------------------- writes

    async def _write(
        self,
        text: str,
        kind: MemoryKind,
        label: Label,
        importance: float | None,
        source_run: str | None,
        pinned: bool,
        metadata: dict[str, Any] | None,
    ) -> tuple[MemoryRecord, bool]:
        """Policy check, dedup and persist. Caller holds ``self._lock``."""
        text = text.strip()
        if not text:
            raise ValueError("cannot remember empty text")
        decision = self.write_policy.check_write(
            kind, label, score_importance(text, importance), pinned=pinned
        )
        now = self.clock.now()
        [embedding] = await self.embedder.embed([text])
        meta = dict(metadata or {})
        duplicate = await self._find_duplicate(embedding, kind, label)
        if duplicate is not None:
            return await self._merge(duplicate, decision.importance, label, source_run, meta), True
        meta["embedder"] = self.embedder.name
        if decision.flags:
            meta["flags"] = sorted(set(meta.get("flags", [])) | set(decision.flags))
        record = MemoryRecord(
            id=new_id("mem"),
            kind=kind,
            text=text,
            label=label,
            importance=decision.importance,
            created_at=now,
            last_accessed=now,
            pinned=pinned,
            source_run=source_run,
            metadata=meta,
            embedding=embedding,
        )
        await self.store.put(record)
        await self._index_add(record)
        return record, False

    async def _find_duplicate(
        self, embedding: list[float], kind: MemoryKind, label: Label
    ) -> MemoryRecord | None:
        hits = await self.index.vectors.search(embedding, k=10)
        for hit in hits:
            if hit.score < self.dedup_threshold:
                break
            if self.index.meta.get(hit.id, {}).get("kind") != kind.value:
                continue
            record = await self.store.get(hit.id)
            if record is None or not record.active:
                continue
            # Never let untrusted text merge into (and thereby taint or ride on) a
            # pinned, user-approved record; store it separately and flagged instead.
            if record.pinned and not label.trusted:
                continue
            if record.embedding is not None and cosine(record.embedding, embedding) < (
                self.dedup_threshold
            ):
                continue
            return record
        return None

    async def _merge(
        self,
        record: MemoryRecord,
        importance: float,
        label: Label,
        source_run: str | None,
        metadata: dict[str, Any],
    ) -> MemoryRecord:
        """Fold a near-duplicate write into ``record``: stronger, fresher, labels joined."""
        now = self.clock.now()
        record.importance = min(1.0, max(record.importance, importance) + 0.05)
        record.access_count += 1
        record.last_accessed = now
        record.label = record.label.join(label)
        flags = set(record.metadata.get("flags", [])) | set(metadata.pop("flags", []))
        record.metadata.update(metadata)
        if not record.label.trusted:
            flags.add(FLAG_UNTRUSTED)
        if flags:
            record.metadata["flags"] = sorted(flags)
        runs = builtins.list(record.metadata.get("merged_runs", []))
        if source_run and source_run != record.source_run and source_run not in runs:
            runs.append(source_run)
            record.metadata["merged_runs"] = runs
        record.metadata["merge_count"] = int(record.metadata.get("merge_count", 0)) + 1
        await self.store.put(record)
        return record

    # -------------------------------------------------------- consolidation

    async def consolidate(
        self,
        *,
        threshold: float | None = None,
        min_cluster_size: int = 2,
        max_sentences: int = 3,
    ) -> builtins.list[dict[str, Any]]:
        """Summarize clusters of similar episodic memories into semantic memories.

        Episodes are clustered greedily (oldest first, cosine to the seed >=
        ``threshold``). Each cluster of at least ``min_cluster_size`` becomes one
        semantic record whose label is the join of the sources' labels, and
        each source is marked ``superseded_by`` it, so recall returns the
        distilled fact instead of many overlapping episodes.
        """
        await self.start()
        limit = self.consolidation_threshold if threshold is None else threshold
        created: builtins.list[dict[str, Any]] = []
        async with self._lock:
            episodes = [
                r
                for r in await self.store.list(kind=MemoryKind.EPISODIC, include_superseded=False)
                if r.embedding is not None
            ]
            assigned: set[str] = set()
            for seed in episodes:
                if seed.id in assigned or seed.embedding is None:
                    continue
                cluster = [seed] + [
                    other
                    for other in episodes
                    if other.id != seed.id
                    and other.id not in assigned
                    and other.embedding is not None
                    and cosine(seed.embedding, other.embedding) >= limit
                ]
                if len(cluster) < min_cluster_size:
                    continue
                assigned.update(r.id for r in cluster)
                created.append(await self._consolidate_cluster(cluster, max_sentences))
        return created

    async def _consolidate_cluster(
        self, cluster: builtins.list[MemoryRecord], max_sentences: int
    ) -> dict[str, Any]:
        if self.summarizer is not None:
            summary, method = (await self.summarizer(cluster)).strip(), "summarizer"
        else:
            summary = extractive_summary([r.text for r in cluster], max_sentences)
            method = "extractive"
        label = join_all(r.label for r in cluster)
        record, deduplicated = await self._write(
            summary or cluster[0].text,
            MemoryKind.SEMANTIC,
            label,
            max(r.importance for r in cluster),
            None,
            False,
            {"consolidated_from": [r.id for r in cluster], "summary_method": method},
        )
        for source in cluster:
            source.superseded_by = record.id
            await self.store.put(source)
            await self.index.remove(source.id)
        result: dict[str, Any] = record.to_dict()
        result["deduplicated"] = deduplicated
        return result

    # ------------------------------------------------------------ retention

    async def decay(self, now: float | None = None) -> dict[str, float]:
        """Current effective strength of every record (read-only, for inspection)."""
        at = self.clock.now() if now is None else now
        return {
            r.id: round(self.decay_policy.effective_strength(r, at), 6)
            for r in await self.store.list()
        }

    async def expire(self, now: float | None = None) -> builtins.list[str]:
        """Delete non-pinned records whose strength fell below the threshold."""
        await self.start()
        at = self.clock.now() if now is None else now
        async with self._lock:
            doomed = [
                r.id for r in await self.store.list() if self.decay_policy.is_expired(r, at)
            ]
            await self._remove(doomed)
        return doomed

    async def _remove(self, ids: builtins.list[str]) -> int:
        for record_id in ids:
            await self.index.remove(record_id)
        removed: int = await self.store.delete_many(ids)
        return removed

    # ----------------------------------------------------------- inspection

    async def list(
        self,
        *,
        kind: str | MemoryKind | None = None,
        pinned: bool | None = None,
        source_run: str | None = None,
        include_superseded: bool = True,
    ) -> builtins.list[dict[str, Any]]:
        wanted = None if kind is None or kind == ANY_KIND else MemoryKind.parse(kind)
        records = await self.store.list(
            kind=wanted,
            pinned=pinned,
            source_run=source_run,
            include_superseded=include_superseded,
        )
        return [r.to_dict() for r in records]

    async def get(self, record_id: str) -> dict[str, Any]:
        data: dict[str, Any] = (await self._get(record_id)).to_dict()
        return data

    async def _get(self, record_id: str) -> MemoryRecord:
        record = await self.store.get(record_id)
        if record is None:
            raise NotFound(f"memory '{record_id}' not found", memory_id=record_id)
        return record

    async def delete(self, record_id: str) -> bool:
        await self.start()
        async with self._lock:
            return await self._remove([record_id]) > 0

    async def pin(self, record_id: str) -> dict[str, Any]:
        """Exempt a record from decay. Only trusted records may be pinned."""
        async with self._lock:
            record = await self._get(record_id)
            self.write_policy.check_pin(record)
            record.pinned = True
            await self.store.put(record)
            data: dict[str, Any] = record.to_dict()
        return data

    async def unpin(self, record_id: str) -> dict[str, Any]:
        async with self._lock:
            record = await self._get(record_id)
            record.pinned = False
            record.last_accessed = self.clock.now()
            await self.store.put(record)
            data: dict[str, Any] = record.to_dict()
        return data

    async def forget(
        self, *, source_run: str | None = None, kind: str | MemoryKind | None = None
    ) -> int:
        """Delete every record written by ``source_run`` and/or of ``kind``.

        At least one filter is required so a typo cannot wipe all memory.
        """
        if source_run is None and kind is None:
            raise ValueError("forget() needs source_run and/or kind")
        await self.start()
        async with self._lock:
            records = await self.store.list(
                source_run=source_run,
                kind=MemoryKind.parse(kind) if kind is not None else None,
            )
            return await self._remove([r.id for r in records])

    async def stats(self) -> dict[str, Any]:
        records = await self.store.list()
        now = self.clock.now()
        return {
            "total": len(records),
            "active": sum(1 for r in records if r.active),
            "superseded": sum(1 for r in records if not r.active),
            "pinned": sum(1 for r in records if r.pinned),
            "untrusted": sum(1 for r in records if not r.label.trusted),
            "expired_pending": sum(1 for r in records if self.decay_policy.is_expired(r, now)),
            "by_kind": {k.value: sum(1 for r in records if r.kind is k) for k in MemoryKind},
            "indexed": len(self.index),
            "working_sessions": len(self._working),
        }

    async def export(self) -> builtins.list[dict[str, Any]]:
        rows: builtins.list[dict[str, Any]] = await self.store.export()
        return rows

    # ------------------------------------------------------- working memory

    def working(self, session_id: str) -> WorkingMemory:
        """The in-process scratchpad for ``session_id`` (created on first use)."""
        scratch = self._working.get(session_id)
        if scratch is None:
            scratch = WorkingMemory(self.working_max_items, clock=self.clock)
            self._working[session_id] = scratch
        return scratch

    def end_session(self, session_id: str) -> None:
        self._working.pop(session_id, None)

    # ------------------------------------------------- episodic, procedural

    async def record_episode(
        self,
        goal: str,
        outcome: str,
        status: str,
        run_id: str,
        label: Label,
        *,
        importance: float | None = None,
    ) -> dict[str, Any]:
        """Remember how a run went. Failures get a little more weight: they teach more."""
        text = f"Goal: {goal.strip()}\nStatus: {status}\nOutcome: {outcome.strip()}"
        weight = importance if importance is not None else (0.5 if status == "succeeded" else 0.6)
        return await self.remember(
            text,
            MemoryKind.EPISODIC,
            label,
            weight,
            run_id,
            metadata={"goal": goal, "status": status},
        )

    async def save_procedure(
        self,
        goal: str,
        plan_json: dict[str, Any] | str,
        label: Label,
        *,
        run_id: str | None = None,
        status: str = "succeeded",
    ) -> dict[str, Any]:
        """Store a plan that worked, indexed by its goal; a newer plan for the same goal wins."""
        plan = json.loads(plan_json) if isinstance(plan_json, str) else plan_json
        return await self.remember(
            goal,
            MemoryKind.PROCEDURAL,
            label,
            0.7,
            run_id,
            metadata={"goal": goal, "plan": plan, "status": status},
        )

    async def find_procedures(self, goal: str, k: int = 3) -> builtins.list[dict[str, Any]]:
        """Successful plans for goals similar to ``goal`` (planner few-shot examples)."""
        scored = await self.search(goal, MemoryKind.PROCEDURAL, k * 3)
        found: builtins.list[dict[str, Any]] = []
        for item in scored:
            meta = item.record.metadata
            if meta.get("status", "succeeded") != "succeeded":
                continue
            found.append(
                {
                    "id": item.record.id,
                    "goal": meta.get("goal", item.record.text),
                    "plan": meta.get("plan"),
                    "score": item.score,
                    "label": item.record.label.to_dict(),
                }
            )
            if len(found) >= k:
                break
        return found
