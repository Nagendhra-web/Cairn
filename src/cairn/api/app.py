"""HTTP API (FastAPI): runs, live streams, approvals, replay/fork, tools, memory.

Security model:
* API keys come from the environment variable named in ``api.api_keys_env``
  (comma-separated). If none are configured the API refuses non-loopback
  clients, so a default install is never an open agent endpoint.
* Every key (or client address) has a token-bucket rate limit.
* Responses never contain secret values: the journal is redacted at write time.

Runs started over HTTP execute as background tasks in this process; for
horizontal scale, enqueue them for workers instead (``cairn worker``).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator
from importlib import resources
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from cairn.core.errors import (
    ApprovalRequired,
    BudgetExceeded,
    CairnError,
    ConfigError,
    NotFound,
    PlanValidationError,
    PolicyViolation,
)
from cairn.core.ids import new_id
from cairn.journal import TERMINAL_RUN_EVENTS
from cairn.runtime import Plan
from cairn.security import KeyedRateLimiter

log = logging.getLogger("cairn.api")

_STATUS = {
    NotFound: 404, PolicyViolation: 403, PlanValidationError: 422, ConfigError: 400,
    BudgetExceeded: 429, ApprovalRequired: 409,
}


class StartRun(BaseModel):
    goal: str | None = None
    plan: dict[str, Any] | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    agent: dict[str, Any] | None = None


class Decision(BaseModel):
    approved: bool
    note: str | None = None


class ForkRequest(BaseModel):
    patches: dict[str, dict[str, Any]] = Field(default_factory=dict)
    invalidate: list[str] = Field(default_factory=list)


class MemorySearch(BaseModel):
    query: str
    kind: str = "any"
    k: int = Field(default=10, ge=1, le=100)


def create_app(cairn: Any, *, api_keys: list[str] | None = None) -> FastAPI:
    """Build the API around a :class:`cairn.sdk.Cairn` instance."""
    config = cairn.config.api
    keys = api_keys if api_keys is not None else [
        k.strip() for k in os.environ.get(config.api_keys_env, "").split(",") if k.strip()
    ]
    limiter = KeyedRateLimiter(config.requests_per_second, config.burst)
    tasks: set[asyncio.Task[Any]] = set()

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        # Graceful shutdown: stop in-flight executions; their journals keep them resumable.
        for task in list(tasks):
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    app = FastAPI(title="Cairn", version="0.1.0", lifespan=lifespan,
                  description="Replayable, provenance-tracking agent runtime")

    async def authenticate(request: Request) -> str:
        client = request.client.host if request.client else "unknown"
        if keys:
            header = request.headers.get("authorization", "")
            token = header.removeprefix("Bearer ").strip() or request.headers.get("x-api-key", "")
            if token not in keys:
                raise HTTPException(401, "missing or invalid API key")
            identity = f"key:{keys.index(token)}"
        else:
            if client not in ("127.0.0.1", "::1", "localhost", "testclient"):
                raise HTTPException(403, "no API keys configured; only loopback clients are allowed")
            identity = f"ip:{client}"
        if not limiter.try_acquire(identity):
            raise HTTPException(429, "rate limit exceeded")
        return identity

    @app.exception_handler(CairnError)
    async def cairn_error(_: Request, exc: CairnError) -> JSONResponse:
        status = next((code for cls, code in _STATUS.items() if isinstance(exc, cls)), 400)
        return JSONResponse({"error": exc.to_dict()}, status_code=status)

    def spawn(coro: Any) -> None:
        task = asyncio.create_task(coro)
        tasks.add(task)

        def done(t: asyncio.Task[Any]) -> None:
            tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.error("background run failed", exc_info=t.exception())

        task.add_done_callback(done)

    rt = cairn.runtime

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "models": [m.name for m in cairn.router.models],
                "tools": len(cairn.tools.list()), "active_runs": len(tasks)}

    @app.get("/v1/runs")
    async def list_runs(status: str | None = None, limit: int = 50,
                        _: str = Depends(authenticate)) -> list[dict[str, Any]]:
        return [r.to_dict() for r in await rt.journal.list_runs(limit=min(limit, 500), status=status)]

    @app.post("/v1/runs", status_code=202)
    async def start_run(body: StartRun, _: str = Depends(authenticate)) -> dict[str, Any]:
        if (body.goal is None) == (body.plan is None):
            raise HTTPException(422, "provide exactly one of 'goal' or 'plan'")
        if body.plan is not None:
            plan = Plan.model_validate(body.plan)
            run_id = await rt.create_run(plan, inputs=body.inputs, budget=cairn.budget())
            spawn(rt.execute(run_id))
            return {"run_id": run_id, "status": "accepted"}
        cairn._require_models()
        agent = cairn.agent(**(body.agent or {}))
        run_id = new_id("run")
        spawn(agent.run(body.goal or "", inputs=body.inputs, run_id=run_id))
        return {"run_id": run_id, "status": "accepted"}

    @app.get("/v1/runs/{run_id}")
    async def get_run(run_id: str, _: str = Depends(authenticate)) -> dict[str, Any]:
        report: dict[str, Any] = await cairn.report(run_id)
        return report

    @app.get("/v1/runs/{run_id}/html", response_class=HTMLResponse)
    async def run_html(run_id: str, _: str = Depends(authenticate)) -> str:
        return str(cairn.render_html(await cairn.report(run_id)))

    @app.get("/v1/runs/{run_id}/events")
    async def events(run_id: str, after: int = 0, _: str = Depends(authenticate)) -> list[dict[str, Any]]:
        return [e.to_dict() for e in await rt.journal.read(run_id, after)]

    async def event_stream(run_id: str, after: int) -> AsyncIterator[dict[str, Any]]:
        """Journal events (durable) merged with live tokens (ephemeral)."""
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def tokens() -> None:
            async for message in rt.services.live.subscribe(run_id):
                await queue.put({"kind": "live", **message})

        token_task = asyncio.create_task(tokens())
        last = after
        try:
            while True:
                for ev in await rt.journal.read(run_id, last):
                    last = ev.seq
                    yield {"kind": "event", **ev.to_dict()}
                    if ev.type in TERMINAL_RUN_EVENTS or ev.type == "run.suspended":
                        state = await rt.load(run_id)
                        if state.status != "running":
                            return
                with contextlib.suppress(TimeoutError):
                    while True:
                        yield await asyncio.wait_for(queue.get(), timeout=0.25)
        finally:
            token_task.cancel()

    @app.get("/v1/runs/{run_id}/stream")
    async def stream(run_id: str, after: int = 0, _: str = Depends(authenticate)) -> StreamingResponse:
        await rt.journal.get_run(run_id)

        async def sse() -> AsyncIterator[str]:
            async for item in event_stream(run_id, after):
                yield f"event: {item['kind']}\ndata: {json.dumps(item, default=str)}\n\n"
            yield "event: end\ndata: {}\n\n"

        return StreamingResponse(sse(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    @app.websocket("/v1/runs/{run_id}/ws")
    async def ws(websocket: WebSocket, run_id: str) -> None:
        if keys and websocket.query_params.get("key") not in keys:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        try:
            async for item in event_stream(run_id, int(websocket.query_params.get("after", 0))):
                await websocket.send_text(json.dumps(item, default=str))
            await websocket.send_text(json.dumps({"kind": "end"}))
            await websocket.close()
        except WebSocketDisconnect:
            return

    @app.get("/v1/runs/{run_id}/approvals")
    async def approvals(run_id: str, _: str = Depends(authenticate)) -> list[dict[str, Any]]:
        state = await rt.load(run_id)
        return [a.__dict__ for a in state.approvals.values()]

    @app.post("/v1/runs/{run_id}/approvals/{request_id}")
    async def decide(run_id: str, request_id: str, body: Decision,
                     who: str = Depends(authenticate)) -> dict[str, Any]:
        await rt.decide(run_id, request_id, approved=body.approved, by=who, note=body.note)
        spawn(rt.resume(run_id))
        return {"run_id": run_id, "request_id": request_id, "approved": body.approved, "resuming": True}

    @app.post("/v1/runs/{run_id}/resume", status_code=202)
    async def resume(run_id: str, _: str = Depends(authenticate)) -> dict[str, Any]:
        await rt.journal.get_run(run_id)
        spawn(rt.resume(run_id))
        return {"run_id": run_id, "status": "resuming"}

    @app.post("/v1/runs/{run_id}/cancel")
    async def cancel(run_id: str, _: str = Depends(authenticate)) -> dict[str, Any]:
        return {"run_id": run_id, "cancelled": await rt.cancel(run_id)}

    @app.post("/v1/runs/{run_id}/replay")
    async def replay(run_id: str, _: str = Depends(authenticate)) -> dict[str, Any]:
        report: dict[str, Any] = (await rt.replay(run_id)).to_dict()
        return report

    @app.post("/v1/runs/{run_id}/fork")
    async def fork(run_id: str, body: ForkRequest, _: str = Depends(authenticate)) -> dict[str, Any]:
        result = await rt.fork(run_id, patches=body.patches, invalidate=body.invalidate)
        out: dict[str, Any] = result.to_dict()
        return out

    @app.get("/v1/tools")
    async def tools(q: str | None = None, _: str = Depends(authenticate)) -> list[dict[str, Any]]:
        specs = [m.spec for m in await cairn.tools.discover(q, k=20)] if q else cairn.tools.list()
        return [{**s.catalog_entry(), "output_trust": s.output_trust.value,
                 "sensitive": sorted(s.sensitive_params), "source": s.source} for s in specs]

    @app.get("/v1/memory")
    async def memory_list(kind: str | None = None, _: str = Depends(authenticate)) -> list[dict[str, Any]]:
        if cairn.memory is None:
            raise HTTPException(404, "memory disabled")
        records: list[dict[str, Any]] = await cairn.memory.list(kind=kind)
        return records

    @app.post("/v1/memory/search")
    async def memory_search(body: MemorySearch, _: str = Depends(authenticate)) -> list[dict[str, Any]]:
        if cairn.memory is None:
            raise HTTPException(404, "memory disabled")
        found: list[dict[str, Any]] = await cairn.memory.recall(body.query, body.kind, body.k)
        return found

    @app.delete("/v1/memory/{memory_id}")
    async def memory_delete(memory_id: str, _: str = Depends(authenticate)) -> dict[str, Any]:
        if cairn.memory is None:
            raise HTTPException(404, "memory disabled")
        return {"deleted": await cairn.memory.delete(memory_id)}

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> str:
        return resources.files("cairn.api").joinpath("static/dashboard.html").read_text("utf-8")

    app.state.background = tasks
    return app
