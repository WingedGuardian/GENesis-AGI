"""MIME email parser — extracts structured fields from raw RFC 2822 bytes."""

from __future__ import annotations

import email
import email.header
import email.utils
import hashlib
import re
from datetime import UTC, datetime
from email.message import Message

from genesis.mail.types import ParsedEmail

_MAX_BODY_CHARS = 50_000
_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def parse_email(raw_bytes: bytes, *, uid: int) -> ParsedEmail:
    """Parse raw RFC 2822 email bytes into a structured ParsedEmail."""
    msg = email.message_from_bytes(raw_bytes)

    message_id = msg.get("Message-ID", "").strip()
    if not message_id:
        # Generate a deterministic fallback from content hash
        content_hash = hashlib.sha256(raw_bytes[:4096]).hexdigest()[:16]
        message_id = f"<generated-{content_hash}@genesis>"

    sender = _decode_header(msg.get("From", ""))
    subject = _decode_header(msg.get("Subject", ""))
    date_str = _parse_date(msg.get("Date", ""))
    body, has_attachments = _extract_body(msg)

    if len(body) > _MAX_BODY_CHARS:
        body = body[:_MAX_BODY_CHARS] + "\n... (truncated)"

    urls = _URL_RE.findall(body)

    return ParsedEmail(
        message_id=message_id,
        imap_uid=uid,
        sender=sender,
        subject=subject,
        date=date_str,
        body=body,
        urls=urls,
        has_attachments=has_attachments,
    )


def _decode_header(value: str) -> str:
    """Decode RFC 2047 encoded header value."""
    if not value:
        return ""
    parts = email.header.decode_header(value)
    decoded = []
    for data, charset in parts:
        if isinstance(data, bytes):
            decoded.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(data)
    return " ".join(decoded)


def _parse_date(value: str) -> str:
    """Parse email Date header into ISO format."""
    if not value:
        return datetime.now(UTC).isoformat()
    parsed = email.utils.parsedate_to_datetime(value)
    return parsed.isoformat()


def _extract_body(msg: Message) -> tuple[str, bool]:
    """Extract text body from email, preferring plain text. Returns (body, has_attachments)."""
    if not msg.is_multipart():
        content_type = msg.get_content_type()
        payload = _decode_payload(msg)
        if content_type == "text/html":
            payload = _strip_html(payload)
        return payload, False

    plain_parts: list[str] = []
    html_parts: list[str] = []
    has_attachments = False

    for part in msg.walk():
        content_type = part.get_content_type()
        disposition = str(part.get("Content-Disposition", ""))

        if "attachment" in disposition:
            has_attachments = True
            continue

        if content_type == "text/plain":
            plain_parts.append(_decode_payload(part))
        elif content_type == "text/html":
            html_parts.append(_decode_payload(part))

    if plain_parts:
        return "\n".join(plain_parts), has_attachments
    if html_parts:
        return _strip_html("\n".join(html_parts)), has_attachments
    return "", has_attachments


def _decode_payload(part: Message) -> str:
    """Decode a MIME part's payload to string."""
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    return payload.decode(charset, errors="replace")


def _strip_html(html: str) -> str:
    """Crude HTML tag stripping — intentionally simple."""
    text = _HTML_TAG_RE.sub(" ", html)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()
