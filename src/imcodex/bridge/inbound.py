from __future__ import annotations

from ..models import InboundMessage, InboundQuoteAttachment


_QUOTED_MESSAGE_BEGIN = "[Quoted message begins]"
_QUOTED_MESSAGE_END = "[Quoted message ends]"
_CURRENT_MESSAGE = "[Current message]"


def render_inbound_input(message: InboundMessage) -> str:
    quote = message.quote
    if quote is None:
        return message.text

    quoted_parts: list[str] = []
    if quote.text.strip():
        quoted_parts.append(quote.text.strip())
    quoted_parts.extend(_render_quoted_attachment(attachment) for attachment in quote.attachments)
    if not quoted_parts:
        quoted_parts.append("[Original content unavailable]")
    quoted_block = _as_markdown_quote("\n".join(quoted_parts))

    current_text = message.text.strip()
    if not current_text:
        if message.attachments:
            current_text = (
                "[Image]"
                if all(item.kind == "image" for item in message.attachments)
                else "[Attachment]"
            )
        else:
            current_text = "[No additional text]"
    return "\n".join(
        (
            _QUOTED_MESSAGE_BEGIN,
            quoted_block,
            _QUOTED_MESSAGE_END,
            _CURRENT_MESSAGE,
            current_text,
        )
    )


def _as_markdown_quote(value: str) -> str:
    """Keep untrusted quoted content visibly inside one data boundary."""

    return "\n".join(f"> {line}" for line in value.splitlines())


def _render_quoted_attachment(attachment: InboundQuoteAttachment) -> str:
    label = attachment.kind
    if attachment.kind == "voice" and attachment.transcript:
        return f"[voice: {attachment.transcript}]"
    if attachment.filename:
        return f"[{label}: {attachment.filename}]"
    return f"[{label}]"
