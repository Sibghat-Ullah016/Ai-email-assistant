import re
import html
import email
from email import policy
from email.header import decode_header
from email.message import EmailMessage
from email.utils import parseaddr
from dataclasses import dataclass
from typing import Optional, cast, Tuple


@dataclass
class ParsedEmail:
    email_id: str
    message_id: str
    sender: str
    subject: str
    date: str
    body: str
    is_truncated: bool = False
    is_automated: bool = False
    pre_filter_reason: Optional[str] = None


class EmailParser:
    MAX_BODY_CHARS: int = 4000  # Token optimization boundary for Gemini

    AUTOMATED_SENDER_PATTERNS = (
        "no-reply@",
        "noreply@",
        "notifications@",
        "updates@",
        "messages-noreply@",
        "mailer-daemon@",
        "postmaster@",
        "newsletter@",
        "marketing@",
        "bounce@",
    )

    @staticmethod
    def _clean_header_value(header_val: Optional[object]) -> str:
        """
        Extracts string representation from modern email headers or falls
        back to decode_header for legacy MIME-encoded words.
        """
        if header_val is None:
            return ""

        raw_str = str(header_val).strip()
        if "=?" not in raw_str:
            return raw_str

        # Fallback for unparsed raw MIME words
        decoded_fragments = []
        for text_bytes, encoding in decode_header(raw_str):
            if isinstance(text_bytes, bytes):
                try:
                    encoding_to_use = encoding if encoding else "utf-8"
                    decoded_fragments.append(text_bytes.decode(encoding_to_use, errors="replace"))
                except (LookupError, UnicodeDecodeError):
                    decoded_fragments.append(text_bytes.decode("latin1", errors="replace"))
            else:
                decoded_fragments.append(str(text_bytes))

        return "".join(decoded_fragments).strip()

    @staticmethod
    def _clean_html_to_text(html_content: str) -> str:
        """Extracts clean human-readable text from HTML payloads with table and block structure preservation."""
        clean = re.sub(r"<(script|style|head)[^>]*>.*?</\1>", "", html_content, flags=re.DOTALL | re.IGNORECASE)
        clean = re.sub(r"<!--.*?-->", "", clean, flags=re.DOTALL)
        clean = re.sub(r"</t[dh]>", "  ", clean, flags=re.IGNORECASE)
        clean = re.sub(r"<br\s*/?>|</p>|</div>|</tr>|</li>|</h[1-6]>|</blockquote>", "\n", clean, flags=re.IGNORECASE)
        clean = re.sub(r"<[^>]+>", " ", clean)
        clean = html.unescape(clean)
        return clean

    @classmethod
    def _strip_quoted_reply_chains(cls, text: str) -> str:
        """
        Removes repetitive quoted reply threads
        to preserve token space for current context.
        """
        patterns = [
            r"(?m)^On\s+[^\n\r]+?wrote:\s*$",
            r"(?m)^-{3,}\s*Original Message\s*-{3,}",
            r"(?m)^From:\s+[^\n\r]+?Subject:\s+[^\n\r]+?$",
            r"(?m)^_{10,}\s*$",
        ]
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                text = text[:match.start()].strip()
        return text

    @classmethod
    def _extract_body(cls, msg: EmailMessage) -> str:
        """
        Traverses MIME structures uniformly, isolates text payloads from binaries/attachments,
        and prioritizes plain text over HTML.
        """
        plain_text = ""
        html_text = ""

        for part in msg.walk():
            if part.get_content_maintype() != "text":
                continue

            content_disposition = str(part.get("Content-Disposition", "")).lower()
            if "attachment" in content_disposition:
                continue

            try:
                payload = part.get_payload(decode=True)
                if not payload or not isinstance(payload, bytes):
                    continue

                raw_charset = part.get_content_charset() or "utf-8"
                try:
                    decoded = payload.decode(raw_charset, errors="replace")
                except (LookupError, UnicodeDecodeError):
                    decoded = payload.decode("utf-8", errors="replace")

                content_subtype = part.get_content_subtype()
                if content_subtype == "plain" and not plain_text:
                    plain_text = decoded
                elif content_subtype == "html" and not html_text:
                    html_text = cls._clean_html_to_text(decoded)
            except Exception:
                continue

        raw_body = plain_text if plain_text else html_text

        cleaned = re.sub(r"[ \t]+", " ", raw_body)
        cleaned = re.sub(r"\r\n|\r", "\n", cleaned)
        cleaned = re.sub(r"\n\s*\n", "\n\n", cleaned).strip()
        cleaned = cls._strip_quoted_reply_chains(cleaned)

        if not cleaned:
            return "[No readable text content found in email body]"

        return cleaned

    @classmethod
    def _detect_automated_or_marketing(cls, msg: EmailMessage, sender_header: str) -> Tuple[bool, Optional[str]]:
        """
        Inspects RFC headers and sender patterns to detect bulk,
        marketing, or automated system mail without calling LLM.
        """
        # RFC 2369: Unsubscribe header present indicates mailing list / marketing
        if msg.get("List-Unsubscribe") or msg.get("List-Id"):
            return True, "RFC Marketing/Mailing List (List-Unsubscribe/List-Id detected)"

        # Precedence bulk/list/junk
        precedence = str(msg.get("Precedence", "")).lower().strip()
        if precedence in ("bulk", "list", "junk"):
            return True, f"Bulk Precedence Header ({precedence})"

        # RFC 3834 Auto-submitted machine messages
        auto_submitted = str(msg.get("Auto-Submitted", "")).lower().strip()
        if auto_submitted and auto_submitted != "no":
            return True, f"Machine Generated Header (Auto-Submitted: {auto_submitted})"

        # Inspect Sender Email Address
        _, email_addr = parseaddr(sender_header)
        email_addr_clean = email_addr.lower().strip()

        for pattern in cls.AUTOMATED_SENDER_PATTERNS:
            if pattern in email_addr_clean:
                return True, f"Automated Mailbox Pattern ({pattern})"

        return False, None

    @classmethod
    def parse(cls, email_id: str, raw_bytes: bytes) -> ParsedEmail:
        """Parses raw RFC822 bytes into an optimized, structured ParsedEmail object."""
        msg = cast(EmailMessage, email.message_from_bytes(raw_bytes, policy=policy.default))

        sender = cls._clean_header_value(msg.get("From", "Unknown Sender"))
        raw_subject = cls._clean_header_value(msg.get("Subject", ""))
        subject = raw_subject if raw_subject else "(No Subject)"
        date = cls._clean_header_value(msg.get("Date", "Unknown Date"))
        message_id = cls._clean_header_value(msg.get("Message-ID", ""))

        # Heuristic detection for automated or marketing mail
        is_automated, reason = cls._detect_automated_or_marketing(msg, sender)

        body = cls._extract_body(msg)
        is_truncated = False

        if len(body) > cls.MAX_BODY_CHARS:
            body = body[: cls.MAX_BODY_CHARS] + "\n\n[...Content Truncated for AI Token Optimization...]"
            is_truncated = True

        return ParsedEmail(
            email_id=email_id,
            message_id=message_id,
            sender=sender,
            subject=subject,
            date=date,
            body=body,
            is_truncated=is_truncated,
            is_automated=is_automated,
            pre_filter_reason=reason,
        )