"""Network tools gated by :class:`NetworkPolicy` (domain allowlist + SSRF checks)."""

from __future__ import annotations

import html
import re
from typing import Any

import httpx

from cairn.core.errors import SandboxViolation, ToolError
from cairn.tools.decorator import tool
from cairn.tools.registry import ToolContext

_SCRIPT = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)


def html_to_text(raw: str) -> str:
    text = _SCRIPT.sub(" ", raw)
    text = re.sub(r"<(br|p|div|li|h[1-6])[^>]*>", "\n", text, flags=re.I)
    text = html.unescape(_TAG.sub(" ", text))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def _policy(ctx: ToolContext) -> Any:
    if ctx.network is None:
        raise SandboxViolation("network tools require a configured NetworkPolicy allowlist")
    return ctx.network


@tool(
    name="http.fetch",
    effects={"network"},
    sensitive={"url"},
    output_trust="untrusted",
    timeout_s=30.0,
    tags={"web", "download", "page", "url"},
)
async def fetch(url: str, ctx: ToolContext, max_chars: int = 20_000) -> dict[str, Any]:
    """Fetch a web page and return its readable text.

    The URL is a sensitive parameter because a URL is an exfiltration channel
    (data can be smuggled out in the query string). Output is untrusted.

    Args:
        url: http(s) URL on the network allowlist
        max_chars: maximum characters of text to return
    """
    _policy(ctx).check(url)
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
        resp = await client.get(url, headers={"User-Agent": "cairn-runtime/0.1"})
    if resp.is_redirect:
        location = resp.headers.get("location", "")
        _policy(ctx).check(str(resp.url.join(location)))
        raise ToolError(f"redirect to {location} must be fetched explicitly", status=resp.status_code)
    if resp.status_code >= 400:
        raise ToolError(f"HTTP {resp.status_code} fetching {url}", status=resp.status_code)
    body = resp.text
    title_match = _TITLE.search(body)
    is_html = "html" in resp.headers.get("content-type", "")
    text = html_to_text(body) if is_html else body
    return {
        "url": str(resp.url),
        "status": resp.status_code,
        "title": html.unescape(title_match.group(1).strip()) if title_match else None,
        "text": text[:max_chars],
        "truncated": len(text) > max_chars,
    }


@tool(
    name="http.post_json",
    effects={"network", "send"},
    sensitive={"url", "payload"},
    output_trust="untrusted",
    timeout_s=30.0,
    tags={"webhook", "api", "post"},
)
async def post_json(url: str, payload: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """POST a JSON payload to an allowlisted URL (webhooks, internal APIs).

    Args:
        url: http(s) URL on the network allowlist
        payload: JSON object to send
    """
    _policy(ctx).check(url)
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=False) as client:
        resp = await client.post(url, json=payload)
    if resp.status_code >= 400:
        raise ToolError(f"HTTP {resp.status_code} posting to {url}", status=resp.status_code)
    return {"status": resp.status_code, "body": resp.text[:5000]}
