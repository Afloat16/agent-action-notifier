"""Bounded RFC822 import into an unverified, non-executable review inbox.

No email header, routing reference, or imported file authenticates its author.
This module intentionally provides no approval/verification/agent-answer API.
"""

from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
import hashlib
import json
import re

from .privacy import ValidationError, redact_text

MAX_REPLY_BYTES = 65536
MAX_REPLY_TEXT = 2000
_NOTICE_ID = re.compile(r"<aan\.[0-9a-f]{64}@agent-action-notifier\.invalid>\Z")
_MESSAGE_ID = re.compile(r"<[^<>\s@]+@[^<>\s@]+>\Z", re.ASCII)
_MARKER = re.compile(r"AAN-REPLY ([0-9a-f]{24}) ([1-9][0-9]{0,9})\Z")
_SINGLETONS = ("Message-ID", "In-Reply-To", "References", "From", "To", "Subject",
               "Auto-Submitted", "Content-Type", "Content-Transfer-Encoding",
               "Content-Disposition", "MIME-Version")
_FORWARD = re.compile(r"(?im)^(?:[- ]*forwarded message[- ]*|begin forwarded message:|fwd:)")
_QUOTE = re.compile(r"(?i)^(?:>|on .+wrote:|[- ]*original message[- ]*|from:|sent:|subject:|to:)")


@dataclass(frozen=True)
class Reply:
    message_hash: str
    digest: str
    content_digest: str
    in_reply_to: str
    reference: str
    revision: int
    body: str

    @classmethod
    def parse(cls, raw: bytes) -> "Reply":
        if not isinstance(raw, bytes) or not raw or len(raw) > MAX_REPLY_BYTES:
            raise ValidationError("email_reply_too_large_or_empty")
        try:
            return _parse(raw)
        except ValidationError:
            raise
        except Exception:
            pass
        # Do not retain a parser exception or expose any raw header/body text.
        raise ValidationError("invalid_email_reply") from None


def _parse(raw: bytes) -> Reply:
    message = BytesParser(policy=policy.default.clone(raise_on_defect=True)).parsebytes(raw)
    for part in message.walk():
        if part.defects or any(len(part.get_all(name, [])) > 1 for name in _SINGLETONS):
            raise ValidationError("ambiguous_email_reply")
        for name in part.keys():
            if getattr(part[name], "defects", ()):
                raise ValidationError("invalid_email_reply_headers")
        if (part.get_content_type() == "message/rfc822" or part.get_filename()
                or part.get_content_disposition() == "attachment"):
            raise ValidationError("email_reply_attachments_not_supported")
    if any(name.lower().startswith("resent-") for name in message.keys()):
        raise ValidationError("forwarded_email_reply_not_supported")
    if (str(message.get("Auto-Submitted", "no")).strip().lower() != "no"
            or message.get("X-Autoreply") or message.get("X-Autorespond")):
        raise ValidationError("automatic_email_reply_not_supported")
    if re.match(r"(?i)\s*(?:fw|fwd)\s*:", str(message.get("Subject", ""))):
        raise ValidationError("forwarded_email_reply_not_supported")
    for name in ("Message-ID", "In-Reply-To", "From"):
        if len(message.get_all(name, [])) != 1:
            raise ValidationError("email_reply_required_header_missing")
    message_id = str(message["Message-ID"]).strip()
    in_reply_to = str(message["In-Reply-To"]).strip()
    if (not message_id.isascii() or len(message_id) > 998
            or not _MESSAGE_ID.fullmatch(message_id) or not _NOTICE_ID.fullmatch(in_reply_to)):
        raise ValidationError("invalid_email_reply_correlation")
    references = str(message.get("References", ""))
    known_references = set(re.findall(r"<aan\.[0-9a-f]{64}@agent-action-notifier\.invalid>", references))
    if known_references and known_references != {in_reply_to}:
        raise ValidationError("ambiguous_email_reply_correlation")
    if message.get_content_type() == "text/plain" and not message.is_multipart():
        plain = message
    elif message.get_content_type() == "multipart/alternative" and message.is_multipart():
        parts = list(message.iter_parts())
        if (not 1 <= len(parts) <= 2 or any(part.is_multipart() for part in parts)
                or any(part.get_content_type() not in {"text/plain", "text/html"} for part in parts)):
            raise ValidationError("email_reply_mime_not_supported")
        plains = [part for part in parts if part.get_content_type() == "text/plain"]
        if len(plains) != 1:
            raise ValidationError("email_reply_plain_text_required")
        plain = plains[0]
    else:
        raise ValidationError("email_reply_plain_text_required")
    encoding = str(plain.get("Content-Transfer-Encoding", "7bit")).strip().lower()
    if encoding not in {"7bit", "8bit", "binary", "base64", "quoted-printable"}:
        raise ValidationError("email_reply_encoding_not_supported")
    text = plain.get_content(errors="strict")
    if not isinstance(text, str):
        raise ValidationError("invalid_email_reply_text")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if any((ord(char) < 32 or ord(char) == 127) and char not in "\n\t" for char in text):
        raise ValidationError("invalid_email_reply_text")
    text.encode("utf-8")
    lines = text.strip().splitlines()
    marker = _MARKER.fullmatch(lines[0].strip()) if lines else None
    if marker is None:
        raise ValidationError("email_reply_marker_required")
    fresh_lines = []
    for line in lines[1:]:
        if _QUOTE.match(line.strip()):
            break
        fresh_lines.append(line)
    fresh = "\n".join(fresh_lines).strip()
    if _FORWARD.search(fresh):
        raise ValidationError("forwarded_email_reply_not_supported")
    if not fresh or len(fresh) > MAX_REPLY_TEXT:
        raise ValidationError("email_reply_text_too_large_or_empty")
    body = redact_text(fresh)
    reference, revision_text = marker.groups()
    revision = int(revision_text)
    canonical = json.dumps([in_reply_to, reference, revision, body], separators=(",", ":"))
    return Reply(hashlib.sha256(message_id.encode()).hexdigest(),
                 hashlib.sha256(canonical.encode()).hexdigest(),
                 hashlib.sha256(body.encode()).hexdigest(), in_reply_to, reference, revision, body)
