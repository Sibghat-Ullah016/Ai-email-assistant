import os
import ssl
import json
import time
import smtplib
import logging
import re
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Dict
from config import GMAIL_USER, GMAIL_APP_PASSWORD, AUTO_REPLY_ENABLED
from email_parser import ParsedEmail

logger = logging.getLogger(__name__)


class GmailSMTPSender:
    SMTP_SERVER: str = "smtp.gmail.com"
    SMTP_PORT: int = 465  # SSL Port
    TIMEOUT: int = 30     # Network timeout in seconds
    COOLDOWN_SECONDS: int = 86400  # 24-hour rate limit per recipient
    CACHE_FILE: str = ".reply_cooldown_cache.json"

    AUTOMATED_PATTERNS = (
        "no-reply",
        "noreply",
        "do-not-reply",
        "mailer-daemon",
        "postmaster",
        "notifications",
        "newsletter",
        "bounce",
    )

    def __init__(self) -> None:
        self.user = GMAIL_USER.strip().lower()
        self.password = GMAIL_APP_PASSWORD
        self.auto_reply_enabled = AUTO_REPLY_ENABLED
        self._cooldown_cache: Dict[str, float] = self._load_cache()

    def _load_cache(self) -> Dict[str, float]:
        """Loads and purges expired recipient cooldown timestamps from disk."""
        if not os.path.exists(self.CACHE_FILE):
            return {}
        try:
            with open(self.CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                now = time.time()
                # Purge entries older than cooldown window
                return {email: ts for email, ts in data.items() if now - ts < self.COOLDOWN_SECONDS}
        except Exception as err:
            logger.warning("Could not read reply cache file (%s). Initializing fresh cache.", err)
            return {}

    def _save_cache(self) -> None:
        """Persists active cooldown cache to disk."""
        try:
            with open(self.CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._cooldown_cache, f)
        except Exception as err:
            logger.warning("Failed to persist reply cooldown cache: %s", err)

    def _is_safe_to_reply(self, recipient_email: str) -> bool:
        """Enforces killswitch, loop suppression, bot filtering, and 24h rate-limits."""
        target = recipient_email.lower().strip()

        # Guard 1: Master Killswitch
        if not self.auto_reply_enabled:
            logger.info("Auto-reply skipped: AUTO_REPLY_ENABLED is set to False.")
            return False

        # Guard 2: Never reply to authenticated sender (self-loop prevention)
        if target == self.user:
            logger.warning("Aborted auto-reply: Recipient is the authenticated sender address.")
            return False

        # Guard 3: Filter automated bot mailboxes
        if any(pattern in target for pattern in self.AUTOMATED_PATTERNS):
            logger.warning("Aborted auto-reply: Address '%s' matches automated mailbox pattern.", target)
            return False

        # Guard 4: 24-Hour Cooldown Window Check
        last_sent = self._cooldown_cache.get(target)
        now = time.time()
        if last_sent and (now - last_sent < self.COOLDOWN_SECONDS):
            remaining_hours = (self.COOLDOWN_SECONDS - (now - last_sent)) / 3600
            logger.info(
                "Aborted auto-reply: Recipient '%s' is in cooldown (%.1f hours remaining).",
                target,
                remaining_hours,
            )
            return False

        return True

    def send_auto_reply(self, original_email: ParsedEmail, reply_body: str) -> bool:
        """
        Constructs and dispatches a threaded auto-reply email via secure SSL SMTP.
        Complies strictly with RFC 2822, RFC 5322, and RFC 3834.
        """
        # Parse clean email address from sender header
        _, raw_recipient = parseaddr(original_email.sender)
        if not raw_recipient or "@" not in raw_recipient:
            logger.error(
                "Aborted auto-reply: Could not extract valid recipient address from '%s'.",
                original_email.sender,
            )
            return False

        if not self._is_safe_to_reply(raw_recipient):
            return False

        # Sanitize Subject against Header Injection (strip newlines)
        clean_subject = re.sub(r"[\r\n]+", " ", original_email.subject).strip()
        if not clean_subject.lower().startswith("re:"):
            reply_subject = f"Re: {clean_subject}"
        else:
            reply_subject = clean_subject

        # Construct MIME Message
        msg = EmailMessage()
        msg["From"] = self.user
        msg["To"] = raw_recipient
        msg["Subject"] = reply_subject

        # Threading Headers (RFC 2822 / RFC 5322)
        if original_email.message_id:
            msg["In-Reply-To"] = original_email.message_id
            msg["References"] = original_email.message_id

        # Loop Prevention Header (RFC 3834)
        msg["Auto-Submitted"] = "auto-replied"

        # Content
        msg.set_content(reply_body)

        # Transmission via SSL Context
        context = ssl.create_default_context()
        try:
            with smtplib.SMTP_SSL(
                host=self.SMTP_SERVER,
                port=self.SMTP_PORT,
                context=context,
                timeout=self.TIMEOUT,
            ) as server:
                server.login(self.user, self.password)
                server.send_message(msg)
                logger.info("Auto-reply successfully dispatched to %s.", raw_recipient)

                # Update cooldown timestamp and persist
                self._cooldown_cache[raw_recipient.lower()] = time.time()
                self._save_cache()
                return True

        except smtplib.SMTPAuthenticationError as auth_err:
            logger.error("SMTP Authentication Failed: %s", auth_err)
            return False
        except smtplib.SMTPRecipientsRefused as recipient_err:
            logger.error("Recipient address rejected by Gmail SMTP server: %s", recipient_err)
            return False
        except smtplib.SMTPSenderRefused as sender_err:
            logger.error("Sender address rejected by Gmail SMTP server: %s", sender_err)
            return False
        except (smtplib.SMTPException, TimeoutError, OSError) as net_err:
            logger.error("Network or protocol error sending auto-reply to %s: %s", raw_recipient, net_err)
            return False