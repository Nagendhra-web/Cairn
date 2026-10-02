# Models and routing

Plans and agents ask for a capability tier, not a model name. The `ModelRouter` picks a registered endpoint for each call, falls back on failure, skips endpoints whose circuit is open, honors per-endpoint rate limits, prices the call, and returns a `RouteDecision` that is journaled. Code: `src/cairn/models/`.

## Request and response types

`models/types.py`:

* `Tier`: `fast`, `balanced`, `frontier`, ordered by `rank`; `escalate()` moves one step up and stays at `frontier`.
* `ContentPart(type: "text"|"image"|"audio"|"document", text, url, data (base64), media_type)`.
* `Message(role: "system"|"user"|"assistant", content: str | list[ContentPart])`.
* `ModelRequest(messages, system=None, max_tokens=4096, temperature=None, response_schema=None, stop=None, effort=None, metadata={})`. `required_capabilities()` derives `vision` (image parts), `audio`, `documents` and `json` (a `response_schema`).
* `Usage(input_tokens, output_tokens, estimated)`; `ModelResponse(text, model, provider, usage, finish_reason, latency_ms, ttft_ms, cost_usd, parsed)`.
* `ModelInfo(name, provider, tier="balanced", capabilities={"text", "json"}, context_window=128000, input_price_per_mtok=None, output_price_per_mtok=None, local=False)`.

`llm` nodes build requests with `max_tokens` from the node (default 2048); `effort` and `temperature` are not settable from plan nodes. `context_window` is descriptive: the router does not check request size against it.

When a provider does not report usage, tokens are estimated at about 4 characters per token (`estimate_tokens`, `estimate_request_tokens` adds 4 per message) and `Usage.estimated` is `true`.

## Providers

`Provider` protocol: `name`; `async complete(model, request) -> ModelResponse`. `StreamingProvider` adds `stream(model, request)`, yielding text deltas and then exactly one final `ModelResponse`. `EmbeddingProvider`: `async embed(model, texts)`.

### Anthropic (`models/anthropic.py`)

`AnthropicProvider(api_key=None, *, base_url=None, server_fallback=True, timeout_s=600.0, client=None)` uses the official `anthropic` SDK (`pip install 'cairn-runtime[anthropic]'`; without it a `ConfigError` explains the install), through `client.beta.messages.create` and `client.beta.messages.stream`.

Request mapping:

* `system` is the request's `system` plus the text of any `system`-role messages, joined with blank lines; other messages are sent as `messages`.
* `stop` becomes `stop_sequences`.
* `effort` becomes `output_config.effort`; `response_schema` becomes `output_config.format = {"type": "json_schema", "schema": ...}` (structured outputs).
* With `server_fallback=True` (the default, and what `build_router` uses), every request carries `betas=["server-side-fallback-2026-07-01"]` and `fallbacks="default"`, so the API can re-run a request declined by a safety classifier on a fallback model chosen by refusal category.
* Image and document parts become `url` sources or `base64` sources (default media types `image/png` and `application/pdf`); audio parts raise `ModelError`.

Response mapping:

* `stop_reason == "refusal"` raises a non-retryable `ModelError("model declined the request", category=<stop_details.category>)`. With server-side fallbacks enabled this means the whole fallback chain declined.
* Text blocks are concatenated; usage comes from the response; the response's `model` is recorded (it may name the fallback model that served the request).
* Errors: HTTP 429 becomes `RateLimited` (retryable); other errors are `ModelError`, retryable when the status is unknown, 5xx, 408 or 409.

`DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"` is defined in the module but not used by the router or configuration.

### OpenAI-compatible (`models/openai_compat.py`)

`OpenAICompatibleProvider(base_url, api_key=None, *, name="openai-compatible", timeout_s=120.0, native_json_schema=True, client=None)` speaks `POST <base_url>/chat/completions` over `httpx`. One implementation covers hosted APIs, vLLM, llama.cpp server, LM Studio, TGI, gateways and Ollama (`http://localhost:11434/v1`).

* `system` becomes a leading system message; `temperature` and `stop` are passed through.
* `response_schema` becomes `response_format = {"type": "json_schema", "json_schema": {"name": "output", "schema": ...}}`, or `{"type": "json_object"}` with `native_json_schema=False` for servers that lack schema support. Either way Cairn validates the result itself.
* Streaming uses server-sent events with `stream_options.include_usage`; TTFT is measured at the first content delta.
* Image parts become `image_url` (data URLs for base64); audio parts become `input_audio`; document parts raise `ModelError`.
* Missing `usage` falls back to estimates (`estimated=True`).
* HTTP 429 is `RateLimited`; other 4xx/5xx are `ModelError` with the first 500 characters of the body, retryable for 5xx, 408, 409; transport errors are `ModelError` (retryable).
* `embed(model, texts)` calls `/embeddings`, so the provider also works with `ProviderEmbedder`.

### Scripted (`models/scripted.py`)

`ScriptedProvider(rules=None, default=None, latency_s=0.0)` is deterministic, for tests, offline demos, evaluations and adversarial simulations. `on(match, response, *, fail_times=0, model=None)` adds a rule:

* `match`: a substring, a compiled regex, or a predicate over the `ModelRequest`, tested against the system prompt plus all message text;
* `response`: a string, a dict or list (JSON-encoded), or a callable of the request;
* `fail_times`: the first N matching calls raise a retryable `ModelError`;
* `model`: only match calls to that model name.

Rules are tried in order. With no match and no `default`, it raises `ModelError("no scripted rule matched request: ...")`. Usage is always estimated. `requests` records every `(model, request)`. `stream` yields 16-character chunks. The `scripted` provider cannot be configured in `cairn.toml` (`build_router` raises `ConfigError`); register it in code, for example `Cairn.create(providers=[(ScriptedProvider(...), ModelInfo(...))])`.

## Routing

`ModelRouter.register(provider, info, *, requests_per_second=None, breaker=None)` registers an endpoint under the key `<info.provider>/<info.name>`.

`candidates(tier, needs, model)`:

1. Keep endpoints whose `capabilities` include every needed capability. If none remain, `ConfigError("no registered model satisfies capabilities [...]")`.
2. If `model` is given (an `llm` node's `model`, or a fallback's), endpoints whose name or `provider/name` matches come first (if none match, `ConfigError`); the remaining pool follows in ranked order.
3. Otherwise sort by `(tier_key, price, local)`:
   * `tier_key`: the requested tier first (0), then higher tiers by distance (1, 2), then lower tiers (11, 12);
   * `price`: input plus output price per million tokens, unknown prices counting as 0;
   * endpoints with `local=True` before remote ones at equal tier and price.

Lower tiers are therefore last-resort candidates, used only when every equal or higher-tier candidate failed or was skipped; the attempt is visible in the journaled route decision.

Example: with endpoints `f` (fast, price 1), `b1` (balanced, 5), `b2` (balanced, 1), `bl` (balanced, 1) and `fr` (frontier, 9), a `balanced` request tries `b2, bl, b1, fr, f` and a `frontier` request tries `fr, b2, bl, b1, f`.

`complete(request, *, tier="balanced", model=None, on_token=None) -> (ModelResponse, RouteDecision)` walks the candidates:

* an endpoint whose breaker is open is skipped (`{"model": ..., "skipped": "circuit_open"}`);
* the endpoint's rate limiter, if any, is awaited;
* a `ModelError` records a breaker failure and an attempt entry (`error`, first 200 characters of `message`); a non-retryable error stops the walk, a retryable one moves to the next candidate;
* on success the breaker resets, the cost is computed, and the decision records `chosen`.

If no candidate succeeds, it raises `ModelError("all candidate models failed")` with the route attached, retryable if the last error was retryable. Exceptions that are not `ModelError` propagate without fallback.

`RouteDecision` (journaled as `route.decision`): `requested_tier`, `needs`, `chosen`, `attempts`.

In the executor, model calls are effects of kind `model`. Retries of an `llm` node (with optional tier escalation) happen above the router; fallbacks in the plan (`Fallback(tier=..., model=...)`) replace the tier or model for an attempt.

### Circuit breaker

`CircuitBreaker(threshold=3, cooldown_s=30.0)` per endpoint: after 3 consecutive failures it opens for 30 seconds; after the cooldown it is half-open and lets one trial through (one more failure reopens it); a success closes it. Breakers are in-process state, not persisted.

### Rate limits

`requests_per_second` creates a `TokenBucket(rate, capacity=max(1, rate))`; calls wait for a token. Configure it per model in `cairn.toml` (`requests_per_second`).

## Cost accounting

`ModelInfo.cost(usage)` returns `(input_tokens * input_price + output_tokens * output_price) / 1e6`, or `None` if either price is unknown. Prices are configuration, never guessed.

* The journal records `cost_usd` per model effect (possibly `null`).
* Run usage (`Usage`) adds priced costs and counts `unpriced_calls` for calls without a price; reports print `cost=$0.000000 (+1 unpriced)`; `by_model` breaks usage down per model name.
* `cairn.eval.metrics.aggregate_usage` reports `cost_usd = None` when no call was priced and `cost_complete = False` when only some were.
* Planner usage is added to the run under `by_model["planner"]`. The planner sums only known costs, and unpriced planner calls are not counted in `unpriced_calls`.
* Cost budgets (`max_cost_usd`) only see priced calls.

`auto_models` (below) configures published per-token prices for the three Claude models it adds. Ollama models added by `auto_models` are priced at 0. Models declared in `cairn.toml` without prices are unpriced.

## Streaming and TTFT

The executor passes an `on_token` callback to the router only when someone is subscribed to the run's live channel (`LiveBus`), for example an API SSE or WebSocket client. Then a `StreamingProvider` is used in streaming mode and each delta is published as `{"type": "token", "node_id": ..., "text": ...}`; those messages are ephemeral and not journaled. Time to first token is measured by the provider and journaled as `ttft_ms` on the model effect (null when not streaming). `model_latency_ms` is the provider-measured latency; `latency_ms` is the effect's wall time.

## Structured output

For `llm` nodes with `output_schema` (and for the planner, critic, judge and supervisor), responses are parsed with `extract_json` (tries the whole text, fenced code blocks, then the outermost `{...}` or `[...]`) and validated with `validate_against`, which accepts a pydantic model class or a JSON schema subset: `type` (including lists of types), `enum`, `required`, `properties`, `additionalProperties: false`, `items`, `minItems`, `maxItems`, `minLength`, `maxLength`, `pattern`, `minimum`, `maximum`. Other keywords are ignored. Validation errors are fed back for up to 2 repairs in `llm` nodes.

## Configuring models

In `cairn.toml` (see [configuration.md](configuration.md)):

```toml
[[models]]
name = "llama3.1:8b"
provider = "ollama"                    # anthropic | openai-compatible | ollama | scripted
base_url = "http://localhost:11434/v1"
tier = "fast"
local = true
capabilities = ["text", "json"]
input_price_per_mtok = 0.0
output_price_per_mtok = 0.0
requests_per_second = 2.0

[[models]]
name = "gpt-compatible-model"
provider = "openai-compatible"
base_url = "https://api.example.com/v1"
api_key_env = "EXAMPLE_API_KEY"         # the key itself never goes in the file
tier = "balanced"
```

`build_router(models, env)` creates one provider per `(provider, base_url, api_key_env)` and registers each model. `openai-compatible` and `ollama` require `base_url`. `load_config` fails if a model names an unset `api_key_env`.

### Auto detection from the environment

When the configuration declares no models, `auto_models(env)` adds:

| Condition | Models |
|---|---|
| `ANTHROPIC_API_KEY` set | `claude-haiku-4-5` (fast, $1/$5 per million input/output tokens, 200k context), `claude-sonnet-5-5` (balanced, $2/$10, 1M), `claude-opus-5-5` (frontier, $4/$20, 1M); provider `anthropic`, capabilities `text, json, vision, documents` |
| `CAIRN_OPENAI_MODEL` and `OPENAI_API_KEY` set | that model, provider `openai-compatible`, `base_url` from `OPENAI_BASE_URL` (default `https://api.openai.com/v1`), tier from `CAIRN_OPENAI_TIER` (default `balanced`), unpriced |
| `CAIRN_OLLAMA_MODEL` set | that model, provider `ollama`, `base_url` = `OLLAMA_HOST` (default `http://localhost:11434`, `http://` added if missing) + `/v1`, tier from `CAIRN_OLLAMA_TIER` (default `fast`), `local=true`, price 0 |

Several can apply at once. `cairn doctor` lists what was detected. With no models at all, `Cairn.ask`, `cairn run`, `cairn plan` and goal-based API runs fail with a `ConfigError` explaining these options; plans that use no `llm` nodes still run.

## Limitations

* `server_fallback` cannot be turned off from `cairn.toml`; construct `AnthropicProvider(server_fallback=False)` and register it in code if your account or model does not accept the fallback beta.
* No request-size check against `context_window`.
* Breaker and rate-limiter state is per process.
