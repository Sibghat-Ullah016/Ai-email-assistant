import sys
import time
import logging
from dataclasses import dataclass
from typing import List, Optional

from imap_client import GmailIMAPClient
from email_parser import EmailParser, ParsedEmail
from ai_engine import AIEmailEngine, EmailAnalysisResult, EmailCategory, PriorityLevel
from smtp_sender import GmailSMTPSender

# Suppress internal SDK-level function calling noise explicitly
logging.getLogger("google_genai.models").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.WARNING)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("EmailAssistantOrchestrator")


@dataclass
class ProcessingMetrics:
    total_scanned: int = 0
    urgent_actionable: int = 0
    replies_sent: int = 0
    informational: int = 0
    spam_promotions: int = 0
    failures: int = 0


class EmailAssistantService:
    BATCH_SIZE: int = 10              # Emails processed per IMAP fetch cycle
    POLL_INTERVAL_SECONDS: int = 60    # Idle sleep duration when queue is zero
    AI_THROTTLE_SECONDS: float = 1.5   # Rate-limit cushion between AI calls

    def __init__(self) -> None:
        self.ai_engine = AIEmailEngine()
        self.smtp_sender = GmailSMTPSender()
        self.metrics = ProcessingMetrics()
        self._is_running = True

    def _apply_inbox_actions(
        self,
        client: GmailIMAPClient,
        uid: str,
        parsed_email: ParsedEmail,
        analysis: EmailAnalysisResult,
    ) -> None:
        """
        Applies IMAP flags and dispatches auto-replies based on AI classification.
        Enforces strict unread retention for emails requiring manual intervention.
        """
        reply_dispatched = False

        # Step A: Dispatch auto-reply if policy certifies it safe and draft exists
        if analysis.is_auto_reply_safe and analysis.suggested_reply:
            sent = self.smtp_sender.send_auto_reply(parsed_email, analysis.suggested_reply)
            if sent:
                self.metrics.replies_sent += 1
                reply_dispatched = True
            else:
                logger.warning("UID %s: Auto-reply skipped by safety or cooldown filters.", uid)
        else:
            logger.info(
                "UID %s: Auto-reply omitted (Policy safe: %s, Has draft: %s).",
                uid,
                analysis.is_auto_reply_safe,
                bool(analysis.suggested_reply),
            )

        # Step B: Inbox presentation and read state synchronization
        if analysis.category in [EmailCategory.URGENT, EmailCategory.ACTION_REQUIRED]:
            # Always star actionable items for user visibility
            client.mark_as_flagged(uid)
            self.metrics.urgent_actionable += 1

            if reply_dispatched:
                # Safe acknowledgment was sent; mark read to clear from pending queue
                read_status = client.mark_as_read(uid)
                if read_status:
                    logger.info("UID %s: FLAGGED (Starred) and marked READ (Auto-reply sent).", uid)
            else:
                # NO auto-reply was sent. CRITICAL: Keep UNREAD so user notices it immediately!
                logger.info(
                    "UID %s: FLAGGED (Starred) but LEFT UNREAD for manual intervention.",
                    uid,
                )

        elif analysis.category == EmailCategory.SPAM_OR_PROMOTIONAL:
            read_status = client.mark_as_read(uid)
            if read_status:
                logger.info("UID %s: Marked READ (Promotional/Spam cleared).", uid)
            self.metrics.spam_promotions += 1

        elif analysis.category == EmailCategory.INFORMATIONAL:
            read_status = client.mark_as_read(uid)
            if read_status:
                logger.info("UID %s: Marked READ (Informational archived).", uid)
            self.metrics.informational += 1

    def _process_batch(self, client: GmailIMAPClient, target_batch: List[str]) -> None:
        """Processes a single slice of email UIDs chronologically from newest to oldest."""
        for uid in target_batch:
            if not self._is_running:
                break

            self.metrics.total_scanned += 1
            logger.info("=" * 60)
            logger.info("Processing Email UID: %s", uid)

            # Step 1: Raw Bytes Fetching (Preserving UNSEEN via BODY.PEEK[])
            raw_bytes: Optional[bytes] = client.fetch_raw_email(uid)
            if not raw_bytes:
                logger.error("Skipping UID %s: Failed to fetch raw payload.", uid)
                self.metrics.failures += 1
                continue

            # Step 2: MIME Parsing & Content Sanitization
            try:
                parsed_email: ParsedEmail = EmailParser.parse(uid, raw_bytes)
            except Exception as parse_err:
                logger.error("Skipping UID %s: MIME Parser error: %s", uid, parse_err)
                self.metrics.failures += 1
                continue

            logger.info("Date:    %s", parsed_email.date)
            logger.info("Sender:  %s", parsed_email.sender)
            logger.info("Subject: %s", parsed_email.subject)

            # Step 3: Heuristic Pre-Filtering vs AI Analysis
            if parsed_email.is_automated:
                logger.info(
                    "UID %s: Pre-filtered as automated/promotional. Reason: %s. Bypassing AI inference.",
                    uid,
                    parsed_email.pre_filter_reason,
                )
                analysis = EmailAnalysisResult(
                    category=EmailCategory.SPAM_OR_PROMOTIONAL,
                    priority=PriorityLevel.LOW,
                    summary=f"Automated message pre-filtered: {parsed_email.pre_filter_reason}",
                    is_auto_reply_safe=False,
                    suggested_reply=None,
                )
            else:
                # Rate-limit cushion before LLM call
                time.sleep(self.AI_THROTTLE_SECONDS)
                analysis = self.ai_engine.analyze_email(parsed_email)
                if not analysis:
                    logger.error("Skipping UID %s: AI Inference failed to return schema.", uid)
                    self.metrics.failures += 1
                    continue

            # Step 4: Display Terminal Output
            print("\n" + "-" * 50)
            print(f"  CLASSIFICATION: {analysis.category.value} (Priority: {analysis.priority.value})")
            print(f"  SUMMARY:        {analysis.summary}")
            print(f"  SAFE TO REPLY:  {analysis.is_auto_reply_safe}")
            if analysis.suggested_reply:
                print(f"  PROPOSED DRAFT:\n{analysis.suggested_reply}")
            else:
                print("  PROPOSED DRAFT: [None Generated]")
            print("-" * 50 + "\n")

            # Step 5: Inbox State Synchronization & Auto-Reply Dispatch
            self._apply_inbox_actions(client, uid, parsed_email, analysis)

    def run_daemon(self) -> None:
        """Runs the email assistant in continuous daemon mode, prioritizing newest emails."""
        logger.info("Starting AI Email Assistant Daemon Worker...")

        while self._is_running:
            try:
                with GmailIMAPClient() as client:
                    client.select_mailbox("INBOX", readonly=False)
                    unseen_uids: List[str] = client.search_unseen_emails()

                    if unseen_uids:
                        # CRITICAL: Reverse list so newest (highest UID) is processed first
                        unseen_uids.reverse()

                        target_batch = unseen_uids[: self.BATCH_SIZE]
                        logger.info(
                            "Eligible Unread Queue: %d | Processing newest batch of %d emails...",
                            len(unseen_uids),
                            len(target_batch),
                        )
                        self._process_batch(client, target_batch)

                        # Print progressive summary after each batch
                        self._print_execution_summary()

                        # Short pause before next batch drain
                        time.sleep(2)
                    else:
                        logger.info(
                            "Inbox clean (0 unread actionable emails). Sleeping for %d seconds...",
                            self.POLL_INTERVAL_SECONDS,
                        )
                        time.sleep(self.POLL_INTERVAL_SECONDS)

            except (ConnectionError, TimeoutError, OSError) as net_err:
                logger.warning("Network or IMAP handshake dropped (%s). Retrying in 15 seconds...", net_err)
                time.sleep(15)
            except Exception as loop_err:
                logger.error("Unexpected worker exception: %s", loop_err, exc_info=True)
                time.sleep(10)

    def stop(self) -> None:
        """Signals daemon to stop gracefully."""
        self._is_running = False

    def _print_execution_summary(self) -> None:
        """Prints active audit report of the pipeline run."""
        print("\n" + "=" * 50)
        print("           EXECUTION AUDIT SUMMARY               ")
        print("=" * 50)
        print(f"Total Emails Scanned:      {self.metrics.total_scanned}")
        print(f"Urgent / Action Required:  {self.metrics.urgent_actionable}")
        print(f"Auto-Replies Sent:         {self.metrics.replies_sent}")
        print(f"Informational Logged:      {self.metrics.informational}")
        print(f"Spam / Promotional Cleared:{self.metrics.spam_promotions}")
        print(f"Failed / Dropped:          {self.metrics.failures}")
        print("=" * 50 + "\n")


if __name__ == "__main__":
    service = EmailAssistantService()
    try:
        service.run_daemon()
    except KeyboardInterrupt:
        logger.info("Termination signal received. Shutting down cleanly...")
        service.stop()
        service._print_execution_summary()
        sys.exit(0)
    except Exception as fatal_err:
        logger.critical("Fatal pipeline crash: %s", fatal_err, exc_info=True)
        sys.exit(1)