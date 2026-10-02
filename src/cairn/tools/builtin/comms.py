"""Outbound messaging tools.

``comms.send_email`` delivers through a pluggable transport found in
``ToolContext.services["mail_transport"]``. Without one it writes to an
in-memory/outbox list (``services["outbox"]``) so examples and tests can
observe exactly what an agent tried to send. Recipients and bodies are
sensitive sinks.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from cairn.core.errors import ToolError
from cairn.tools.decorator import tool
from cairn.tools.registry import ToolContext

MailTransport = Callable[[dict[str, Any]], Awaitable[str]]


@tool(
    name="comms.send_email",
    effects={"send"},
    sensitive={"to"},
    output_trust="trusted",
    tags={"email", "mail", "notify", "message"},
)
async def send_email(to: str, subject: str, body: str, ctx: ToolContext) -> dict[str, Any]:
    """Send an email message.

    Args:
        to: recipient email address
        subject: subject line
        body: plain-text body
    """
    if "@" not in to or any(c in to for c in "\r\n,;"):
        raise ToolError(f"invalid recipient '{to}'")
    message = {"to": to, "subject": subject, "body": body}
    transport: MailTransport | None = ctx.services.get("mail_transport")
    if transport is not None:
        message_id = await transport(message)
    else:
        outbox = ctx.services.setdefault("outbox", [])
        outbox.append(message)
        message_id = f"outbox-{len(outbox)}"
    return {"message_id": message_id, "to": to}
