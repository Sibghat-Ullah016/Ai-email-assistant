import imaplib
import logging
import socket
import ssl
from types import TracebackType
from typing import List, Optional, Type
from config import GMAIL_USER, GMAIL_APP_PASSWORD, IMAP_SERVER, IMAP_PORT

logger = logging.getLogger(__name__)


class GmailIMAPClient:
    DEFAULT_TIMEOUT: int = 30  # Network timeout in seconds

    def __init__(self) -> None:
        self.user = GMAIL_USER
        self.password = GMAIL_APP_PASSWORD
        self.server = IMAP_SERVER
        self.port = IMAP_PORT
        self.client: Optional[imaplib.IMAP4_SSL] = None

    def __enter__(self) -> "GmailIMAPClient":
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        self.disconnect()

    def connect(self) -> None:
        """Establishes an encrypted SSL connection to the IMAP server and authenticates."""
        try:
            self.client = imaplib.IMAP4_SSL(
                host=self.server,
                port=self.port,
                timeout=self.DEFAULT_TIMEOUT,
            )
            self.client.login(self.user, self.password)
            logger.info("Successfully authenticated with Gmail IMAP server.")
        except imaplib.IMAP4.error as err:
            logger.error("IMAP Authentication Failed: %s", err)
            raise ConnectionError(f"Failed to authenticate with Gmail IMAP: {err}") from err
        except (TimeoutError, socket.timeout, ssl.SSLError) as timeout_err:
            logger.error("IMAP Connection Timed Out or SSL Handshake Failed: %s", timeout_err)
            raise TimeoutError("Connection to Gmail IMAP server timed out.") from timeout_err

    def select_mailbox(self, mailbox: str = "INBOX", readonly: bool = False) -> None:
        """Selects the target mailbox folder (default: INBOX)."""
        if not self.client:
            raise RuntimeError("IMAP client is not connected.")
        status, _ = self.client.select(mailbox, readonly=readonly)
        if status != "OK":
            raise RuntimeError(f"Failed to select mailbox: {mailbox}")

    def search_unseen_emails(self) -> List[str]:
        """
        Searches for emails that are UNSEEN and UNFLAGGED.
        Prevents reprocessing of starred emails awaiting manual response.
        Complies strictly with standard IMAP4 search command formatting.
        """
        if not self.client:
            raise RuntimeError("IMAP client is not connected.")

        try:
            # Pass None for default charset followed by distinct IMAP search criteria
            status, data = self.client.uid("search", "UNSEEN", "UNFLAGGED")
            if status != "OK" or not data or not data[0]:
                return []

            email_uids = data[0].decode("utf-8").split()
            return email_uids
        except (imaplib.IMAP4.error, socket.error) as err:
            logger.error("Error searching unseen emails: %s", err)
            return []

    def fetch_raw_email(self, email_uid: str) -> Optional[bytes]:
        """
        Fetches raw RFC822 bytes using BODY.PEEK[].
        Strictly preserves the UNSEEN state on the server during inspection.
        """
        if not self.client:
            raise RuntimeError("IMAP client is not connected.")

        try:
            # BODY.PEEK[] fetches payload without marking message as \Seen
            status, data = self.client.uid("fetch", email_uid, "(BODY.PEEK[])")
            if status != "OK" or not data:
                logger.warning("Could not fetch email UID: %s", email_uid)
                return None

            for part in data:
                if isinstance(part, tuple) and len(part) >= 2:
                    payload = part[1]
                    if isinstance(payload, bytes):
                        return payload
            return None
        except (imaplib.IMAP4.error, socket.error) as err:
            logger.error("Network error fetching email UID %s: %s", email_uid, err)
            return None

    def mark_as_read(self, email_uid: str) -> bool:
        r"""Marks an email with the \Seen flag using UID."""
        if not self.client:
            return False
        try:
            status, _ = self.client.uid("store", email_uid, "+FLAGS", "\\Seen")
            return status == "OK"
        except (imaplib.IMAP4.error, socket.error) as err:
            logger.error("Failed to mark UID %s as read: %s", email_uid, err)
            return False

    def mark_as_flagged(self, email_uid: str) -> bool:
        r"""Marks an email with the \Flagged (Star) flag using UID."""
        if not self.client:
            return False
        try:
            status, _ = self.client.uid("store", email_uid, "+FLAGS", "\\Flagged")
            return status == "OK"
        except (imaplib.IMAP4.error, socket.error) as err:
            logger.error("Failed to flag UID %s: %s", email_uid, err)
            return False

    def disconnect(self) -> None:
        """Safely closes mailbox and logs out."""
        if self.client:
            if getattr(self.client, "state", None) == "SELECTED":
                try:
                    self.client.close()
                except Exception:
                    pass
            try:
                self.client.logout()
                logger.info("IMAP session logged out cleanly.")
            except Exception:
                pass
            finally:
                self.client = None