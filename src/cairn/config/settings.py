"""Configuration: TOML file + environment, validated up front with actionable errors.

Resolution order (later wins): built-in defaults, ``cairn.toml`` (or the file
named by ``CAIRN_CONFIG``), then ``CAIRN_*`` environment variables for the most
common settings. API keys are never stored in the config file: models name the
environment variable that holds their key (``api_key_env``).
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from cairn.core.errors import ConfigError
from cairn.models.types import Tier


class ModelConfig(BaseModel):
    name: str
    provider: Literal["anthropic", "openai-compatible", "ollama", "scripted"]
    tier: Tier = Tier.BALANCED
    base_url: str | None = None
    api_key_env: str | None = None
    capabilities: list[str] = Field(default_factory=lambda: ["text", "json"])
    context_window: int = 128_000
    input_price_per_mtok: float | None = None
    output_price_per_mtok: float | None = None
    requests_per_second: float | None = None
    local: bool = False

    @field_validator("base_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        return v


class CollectionConfig(BaseModel):
    name: str
    path: str | None = None
    trusted: bool = False


class MCPServerEntry(BaseModel):
    """Passed through to ``cairn.mcp.MCPServerConfig``, which validates the extra fields
    (pinned, effects_override, sensitive_params, requires_approval, timeouts...)."""

    model_config = ConfigDict(extra="allow")

    name: str
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    trust: Literal["trusted", "untrusted"] = "untrusted"
    allowed_tools: list[str] = Field(default_factory=lambda: ["*"])


class PolicyConfig(BaseModel):
    enabled: bool = True
    strict: bool = False


class SecurityConfig(BaseModel):
    sandbox_roots: list[str] = Field(default_factory=lambda: ["./workspace"])
    sandbox_read_only: bool = False
    network_allow: list[str] = Field(default_factory=list)
    allow_private_network: bool = False


class BudgetConfig(BaseModel):
    max_cost_usd: float | None = 1.0
    max_tokens: int | None = 200_000
    max_model_calls: int | None = 100
    max_tool_calls: int | None = 100
    max_wall_s: float | None = 900.0
    max_nodes: int = 64
    max_depth: int = 3
    max_concurrency: int = 8


class APIConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8787
    api_keys_env: str = "CAIRN_API_KEYS"
    requests_per_second: float = 5.0
    burst: int = 20


class CairnConfig(BaseModel):
    data_dir: str = ".cairn"
    tools: list[str] = Field(default_factory=lambda: ["math.*", "fs.*", "http.fetch", "comms.*"])
    models: list[ModelConfig] = Field(default_factory=list)
    collections: list[CollectionConfig] = Field(default_factory=list)
    mcp_servers: list[MCPServerEntry] = Field(default_factory=list)
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    memory_enabled: bool = True
    log_level: str = "WARNING"
    json_logs: bool = False

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "cairn.db"


# Claude prices (USD per million tokens) as published by Anthropic, used only
# when ANTHROPIC_API_KEY is present and no models are configured explicitly.
_CLAUDE_DEFAULTS = [
    ("claude-haiku-4-5", Tier.FAST, 1.0, 5.0, 200_000),
    ("claude-sonnet-5-5", Tier.BALANCED, 2.0, 10.0, 1_000_000),
    ("claude-opus-5-5", Tier.FRONTIER, 4.0, 20.0, 1_000_000),
]


def auto_models(env: dict[str, str] | None = None) -> list[ModelConfig]:
    """Infer a model lineup from the environment when none is configured."""
    env = dict(os.environ if env is None else env)
    models: list[ModelConfig] = []
    if env.get("ANTHROPIC_API_KEY"):
        for name, tier, pin, pout, ctx in _CLAUDE_DEFAULTS:
            models.append(ModelConfig(
                name=name, provider="anthropic", tier=tier, api_key_env="ANTHROPIC_API_KEY",
                capabilities=["text", "json", "vision", "documents"], context_window=ctx,
                input_price_per_mtok=pin, output_price_per_mtok=pout,
            ))
    if env.get("CAIRN_OPENAI_MODEL") and env.get("OPENAI_API_KEY"):
        models.append(ModelConfig(
            name=env["CAIRN_OPENAI_MODEL"], provider="openai-compatible",
            base_url=env.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            api_key_env="OPENAI_API_KEY", tier=Tier(env.get("CAIRN_OPENAI_TIER", "balanced")),
        ))
    if env.get("CAIRN_OLLAMA_MODEL"):
        host = env.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
        if not host.startswith("http"):
            host = "http://" + host
        models.append(ModelConfig(
            name=env["CAIRN_OLLAMA_MODEL"], provider="ollama", base_url=host + "/v1",
            tier=Tier(env.get("CAIRN_OLLAMA_TIER", "fast")), local=True,
            input_price_per_mtok=0.0, output_price_per_mtok=0.0,
        ))
    return models


def load_config(path: str | Path | None = None, env: dict[str, str] | None = None) -> CairnConfig:
    env = dict(os.environ if env is None else env)
    path = path or env.get("CAIRN_CONFIG")
    data: dict[str, Any] = {}
    if path is None and Path("cairn.toml").exists():
        path = "cairn.toml"
    if path is not None:
        file = Path(path)
        if not file.exists():
            raise ConfigError(f"config file '{file}' does not exist", path=str(file))
        try:
            data = tomllib.loads(file.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"config file '{file}' is not valid TOML: {exc}") from exc
        data = data.get("cairn", data)
    if env.get("CAIRN_DATA_DIR"):
        data["data_dir"] = env["CAIRN_DATA_DIR"]
    if env.get("CAIRN_LOG_LEVEL"):
        data["log_level"] = env["CAIRN_LOG_LEVEL"]
    if env.get("CAIRN_NETWORK_ALLOW"):
        data.setdefault("security", {})["network_allow"] = [
            d.strip() for d in env["CAIRN_NETWORK_ALLOW"].split(",") if d.strip()
        ]
    if env.get("CAIRN_POLICY_STRICT"):
        data.setdefault("policy", {})["strict"] = env["CAIRN_POLICY_STRICT"].lower() in ("1", "true", "yes")
    try:
        config = CairnConfig.model_validate(data)
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()
        ]
        raise ConfigError("invalid configuration:\n  " + "\n  ".join(problems), problems=problems) from exc
    from cairn.mcp.bridge import MCPServerConfig

    for server in config.mcp_servers:
        try:
            MCPServerConfig.model_validate(server.model_dump())
        except ValidationError as exc:
            problems = [f"mcp_servers[{server.name}].{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
                        for e in exc.errors()]
            raise ConfigError("invalid MCP server configuration:\n  " + "\n  ".join(problems),
                              problems=problems) from exc
    if not config.models:
        config.models = auto_models(env)
    missing = [m.api_key_env for m in config.models
               if m.api_key_env and not env.get(m.api_key_env)]
    if missing:
        raise ConfigError(
            f"models reference unset environment variables: {sorted(set(missing))}. "
            "Export them or remove those models from the config.",
            missing=sorted(set(missing)),
        )
    return config
