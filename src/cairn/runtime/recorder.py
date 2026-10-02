"""Journal writer and the effect replay mechanism.

``Recorder.effect`` is the single choke point for nondeterminism. Every model
call, tool call, retrieval and memory operation goes through it:

* If the journal (or a source recording being replayed or forked) already has
  a result for this effect key *and* the request fingerprint matches, the
  recorded result (or recorded failure) is returned without executing.
* In strict replay mode a missing or mismatched recording raises
  :class:`ReplayDivergence` naming the node and effect, which turns any
  behavioral change (prompt edit, code change, tool change) into a precise,
  model-free regression signal.
* Otherwise the effect executes live, after a budget check, and its result is
  journaled before it is returned (write-ahead), so a crash can never lose a
  completed side effect's outcome.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cairn.core.clock import Clock
from cairn.core.errors import (
    ApprovalRequired,
    CairnError,
    ReplayDivergence,
    RunCancelled,
    error_payload,
)
from cairn.core.ids import stable_hash, to_jsonable
from cairn.journal.events import Event, EventDraft, EventType
from cairn.journal.store import ConcurrentAppend, JournalStore
from cairn.provenance.labels import Label
from cairn.runtime.budget import check_budget
from cairn.runtime.state import EffectRecord, RunState, apply


class Mode(StrEnum):
    LIVE = "live"  # execute, reuse own journal records (resume)
    STRICT = "strict"  # every effect must come from the recording


@dataclass
class EffectOutcome:
    result: Any
    label: Label
    meta: dict[str, Any] = field(default_factory=dict)


class RecordedFailure(CairnError):
    """A failure re-raised from a recording so replay follows the same path."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(payload.get("message", "recorded failure"), **payload.get("details", {}))
        self.code = payload.get("code", "recorded_failure")
        self.retryable = bool(payload.get("retryable", False))


class Recorder:
    def __init__(
        self,
        journal: JournalStore,
        state: RunState,
        clock: Clock,
        *,
        mode: Mode = Mode.LIVE,
        source: dict[str, EffectRecord] | None = None,
        on_event: Callable[[Event], None] | None = None,
    ) -> None:
        self.journal = journal
        self.state = state
        self.clock = clock
        self.mode = mode
        self.source = source if source is not None else {}
        self.on_event = on_event
        self._lock = asyncio.Lock()
        self._session_started = clock.monotonic()
        self.divergences: list[dict[str, Any]] = []

    @property
    def run_id(self) -> str:
        return self.state.run_id

    async def emit(self, type_: str, node_id: str | None = None, **data: Any) -> Event:
        draft = EventDraft(type=type_, data=to_jsonable(data), node_id=node_id, ts=self.clock.now())
        async with self._lock:
            for _ in range(20):
                try:
                    [event] = await self.journal.append(
                        self.run_id, [draft], expected_seq=self.state.last_seq + 1
                    )
                    break
                except ConcurrentAppend:
                    # Someone else (an approval, a cancel request) appended. Fold
                    # their events so our view stays authoritative, then retry.
                    for ev in await self.journal.read(self.run_id, self.state.last_seq):
                        apply(self.state, ev)
            else:  # pragma: no cover - pathological contention
                raise CairnError("could not append to journal after 20 attempts")
            apply(self.state, event)
        if self.on_event is not None:
            self.on_event(event)
        return event

    def lookup(self, key: str) -> EffectRecord | None:
        return self.state.effects.get(key) or self.source.get(key)

    async def effect(
        self,
        key: str,
        kind: str,
        request: Any,
        run: Callable[[], Awaitable[EffectOutcome]],
        *,
        node_id: str | None,
    ) -> EffectOutcome:
        fingerprint = stable_hash(request, length=24)
        record = self.lookup(key)
        if record is not None:
            if record.fingerprint == fingerprint:
                return await self._reuse(record, key, kind, fingerprint, node_id)
            detail = {
                "key": key,
                "kind": kind,
                "expected": record.fingerprint,
                "actual": fingerprint,
            }
            self.divergences.append({"node_id": node_id, **detail})
            if self.mode is Mode.STRICT:
                raise ReplayDivergence(
                    f"effect '{key}' request changed since the recording", node_id=node_id, **detail
                )
            await self.emit(EventType.EFFECT_DIVERGED, node_id, **detail)
        elif self.mode is Mode.STRICT:
            self.divergences.append({"node_id": node_id, "key": key, "kind": kind, "missing": True})
            raise ReplayDivergence(
                f"effect '{key}' is not in the recording", node_id=node_id, key=key, kind=kind
            )

        check_budget(
            self.state.budget, self.state.usage, kind, self.clock.monotonic() - self._session_started
        )
        started = time.perf_counter()
        try:
            outcome = await run()
        except (asyncio.CancelledError, ApprovalRequired, RunCancelled):
            # Not outcomes of the effect: the effect has not happened yet and
            # must run again when the run resumes.
            raise
        except Exception as exc:
            await self.emit(
                EventType.EFFECT_FAILED,
                node_id,
                key=key,
                kind=kind,
                fingerprint=fingerprint,
                error=error_payload(exc),
                latency_ms=round((time.perf_counter() - started) * 1000, 3),
            )
            raise
        await self.emit(
            EventType.EFFECT_COMPLETED,
            node_id,
            key=key,
            kind=kind,
            fingerprint=fingerprint,
            request=_summarize(request),
            result=outcome.result,
            label=outcome.label.to_dict(),
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
            **outcome.meta,
        )
        return outcome

    async def _reuse(
        self, record: EffectRecord, key: str, kind: str, fingerprint: str, node_id: str | None
    ) -> EffectOutcome:
        own = key in self.state.effects and self.state.effects[key] is record
        if not own:
            # Copy the recorded effect into this run's journal so the new run
            # is self-contained and can itself be replayed later.
            if record.error is not None:
                await self.emit(
                    EventType.EFFECT_FAILED, node_id, key=key, kind=kind,
                    fingerprint=fingerprint, error=record.error, replayed=True,
                )
            else:
                await self.emit(
                    EventType.EFFECT_COMPLETED, node_id, key=key, kind=kind,
                    fingerprint=fingerprint, result=record.result,
                    label=record.label.to_dict(), replayed=True,
                )
        if record.error is not None:
            raise RecordedFailure(record.error)
        return EffectOutcome(record.result, record.label)


def _summarize(request: Any, limit: int = 2000) -> Any:
    """Keep journaled request previews bounded; the fingerprint covers the full request."""
    data = to_jsonable(request)
    text = str(data)
    if len(text) <= limit:
        return data
    return {"truncated": True, "preview": text[:limit]}
