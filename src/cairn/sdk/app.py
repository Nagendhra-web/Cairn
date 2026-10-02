"""High-level SDK: one object that wires configuration into a working runtime.

Example::

    from cairn.sdk import Cairn

    async with await Cairn.create() as cairn:
        result = await cairn.ask("Summarize ./workspace/notes.md and email it to me@example.com")
        print(result.status, result.output)
        print(cairn.render(await cairn.report(result.run_id)))
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path
from types import TracebackType
from typing import Any

from cairn.agents import Agent, AgentResult, AgentSpec, AgentSubagentRunner, Supervisor
from cairn.config import CairnConfig, ModelConfig, load_config
from cairn.core.errors import ConfigError
from cairn.journal import SQLiteJournal
from cairn.memory import MemoryManager, SQLiteMemoryStore
from cairn.models import ModelInfo, ModelRouter, OpenAICompatibleProvider, Provider
from cairn.observability import render_html, render_text, run_report
from cairn.provenance import PolicyEngine
from cairn.retrieval import DocumentCorpus
from cairn.runtime import Budget, Plan, RunResult, Runtime, Services
from cairn.runtime.services import ApprovalHandler
from cairn.security import NetworkPolicy, PathSandbox, SecretVault
from cairn.storage import SQLiteDatabase
from cairn.tools import ToolRegistry, ToolSpec
from cairn.tools.builtin import builtin_tools


def build_router(models: Iterable[ModelConfig], env: dict[str, str] | None = None) -> ModelRouter:
    env = dict(os.environ if env is None else env)
    router = ModelRouter()
    providers: dict[tuple[str, str | None, str | None], Provider] = {}
    for m in models:
        key = (m.provider, m.base_url, m.api_key_env)
        if key not in providers:
            api_key = env.get(m.api_key_env) if m.api_key_env else None
            if m.provider == "anthropic":
                from cairn.models.anthropic import AnthropicProvider

                providers[key] = AnthropicProvider(api_key=api_key, base_url=m.base_url)
            elif m.provider in ("openai-compatible", "ollama"):
                if not m.base_url:
                    raise ConfigError(f"model '{m.name}' needs base_url")
                providers[key] = OpenAICompatibleProvider(m.base_url, api_key, name=m.provider)
            else:
                raise ConfigError(
                    f"model '{m.name}': provider '{m.provider}' can only be registered in code"
                )
        router.register(
            providers[key],
            ModelInfo(
                name=m.name, provider=m.provider, tier=m.tier, capabilities=set(m.capabilities),
                context_window=m.context_window, input_price_per_mtok=m.input_price_per_mtok,
                output_price_per_mtok=m.output_price_per_mtok, local=m.local,
            ),
            requests_per_second=m.requests_per_second,
        )
    return router


class Cairn:
    def __init__(
        self,
        runtime: Runtime,
        config: CairnConfig,
        memory: MemoryManager | None,
        db: SQLiteDatabase,
    ) -> None:
        self.runtime = runtime
        self.config = config
        self.memory = memory
        self.db = db
        self._mcp: list[Any] = []

    @classmethod
    async def create(
        cls,
        config: CairnConfig | None = None,
        *,
        in_memory: bool = False,
        providers: Iterable[tuple[Provider, ModelInfo]] = (),
        tools: Iterable[ToolSpec] = (),
        approval_handler: ApprovalHandler | None = None,
        vault: SecretVault | None = None,
        mount_mcp: bool = True,
    ) -> Cairn:
        config = config or load_config()
        db = SQLiteDatabase(":memory:" if in_memory else config.db_path)
        router = build_router(config.models)
        for provider, info in providers:
            router.register(provider, info)
        registry = ToolRegistry()
        registry.register_all(builtin_tools(tuple(config.tools)))
        for spec in tools:
            registry.register(spec, replace=True)
        roots = [Path(r) for r in config.security.sandbox_roots]
        for root in roots:
            root.mkdir(parents=True, exist_ok=True)
        memory = MemoryManager(SQLiteMemoryStore(db)) if config.memory_enabled else None
        if memory is not None:
            await memory.start()
        services = Services(
            journal=SQLiteJournal(db),
            router=router,
            tools=registry,
            policy=PolicyEngine(strict=config.policy.strict, enabled=config.policy.enabled),
            vault=vault or SecretVault.from_env(),
            sandbox=PathSandbox(roots, read_only=config.security.sandbox_read_only) if roots else None,
            network=NetworkPolicy(config.security.network_allow,
                                  allow_private=config.security.allow_private_network),
            memory=memory,
            approval_handler=approval_handler,
        )
        runtime = Runtime(services)
        services.subagents = AgentSubagentRunner(runtime, memory)
        app = cls(runtime, config, memory, db)
        for collection in config.collections:
            corpus = app.corpus(collection.name, trusted=collection.trusted)
            if collection.path:
                await corpus.add_directory(collection.path)
        if mount_mcp:
            for server in config.mcp_servers:
                await app.mount_mcp(server.model_dump())
        return app

    # ------------------------------------------------------------ components

    @property
    def tools(self) -> ToolRegistry:
        return self.runtime.services.tools

    @property
    def router(self) -> ModelRouter:
        return self.runtime.services.router

    def register_tool(self, spec: ToolSpec) -> ToolSpec:
        return self.tools.register(spec, replace=True)

    def corpus(self, name: str = "default", *, trusted: bool = False) -> DocumentCorpus:
        corpora = self.runtime.services.corpora
        if name not in corpora:
            corpora[name] = DocumentCorpus(name, trusted=trusted)
        corpus = corpora[name]
        assert isinstance(corpus, DocumentCorpus)
        return corpus

    async def mount_mcp(self, server: dict[str, Any]) -> Any:
        from cairn.mcp import MCPServerConfig, mount_mcp_server

        report = await mount_mcp_server(self.tools, MCPServerConfig.model_validate(server))
        self._mcp.append(report)
        return report

    def grants(self) -> list[str]:
        """Tool patterns runs may use: configured tools plus mounted MCP servers."""
        return [*self.config.tools, *(f"{s.name}.*" for s in self.config.mcp_servers)]

    def budget(self) -> Budget:
        return Budget(**self.config.budget.model_dump())

    # -------------------------------------------------------------- running

    def agent(self, spec: AgentSpec | None = None, **overrides: Any) -> Agent:
        spec = spec or AgentSpec(name="cairn", tools=self.config.tools,
                                 budget=self.config.budget.model_dump())
        if overrides:
            spec = spec.model_copy(update=overrides)
        return Agent(spec, self.runtime, self.memory)

    async def ask(self, goal: str, **kwargs: Any) -> AgentResult:
        self._require_models()
        return await self.agent().run(goal, **kwargs)

    async def run(self, plan: Plan, **kwargs: Any) -> RunResult:
        kwargs.setdefault("budget", self.budget())
        return await self.runtime.run(plan, **kwargs)

    async def submit(self, plan: Plan, **kwargs: Any) -> tuple[str, str]:
        """Create a run and enqueue it for background workers; returns (run_id, job_id)."""
        from cairn.workers import SQLiteWorkQueue, submit_run

        kwargs.setdefault("budget", self.budget())
        return await submit_run(self.runtime, SQLiteWorkQueue(self.db), plan, **kwargs)

    def supervisor(self, specialists: list[AgentSpec]) -> Supervisor:
        self._require_models()
        return Supervisor(self.runtime, specialists)

    def _require_models(self) -> None:
        if not self.router.models:
            raise ConfigError(
                "no models configured. Set ANTHROPIC_API_KEY, or CAIRN_OLLAMA_MODEL for a local "
                "model, or add [[models]] to cairn.toml. Run 'cairn doctor' for details."
            )

    # ------------------------------------------------------------ inspection

    async def report(self, run_id: str) -> dict[str, Any]:
        await self.runtime.journal.get_run(run_id)  # raises NotFound for unknown runs
        return run_report(run_id, await self.runtime.journal.read(run_id))

    @staticmethod
    def render(report: dict[str, Any], *, color: bool = False) -> str:
        return render_text(report, color=color)

    @staticmethod
    def render_html(report: dict[str, Any]) -> str:
        return render_html(report)

    # -------------------------------------------------------------- lifecycle

    async def close(self) -> None:
        for report in self._mcp:
            client = getattr(report, "client", None)
            if client is not None:
                await client.close()
        self._mcp.clear()
        self.db.close()

    async def __aenter__(self) -> Cairn:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()
