import sys
import logging
from dataclasses import dataclass
from typing import List, Optional

from imap_client import GmailIMAPClient
from email_parser import EmailParser, ParsedEmail
from ai_engine import AIEmailEngine, EmailAnalysisResult, EmailCategory
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
    BATCH_SIZE: int = 5  # Maximum emails to process in a single execution run

    def __init__(self) -> None:
        self.ai_engine = AIEmailEngine()
        self.smtp_sender = GmailSMTPSender()
        self.metrics = ProcessingMetrics()

    def _apply_inbox_actions(
        self,
        client: GmailIMAPClient,
        uid: str,
        parsed_email: ParsedEmail,
        analysis: EmailAnalysisResult,
    ) -> None:
        """
        Applies IMAP flags and dispatches auto-replies based on AI classification.
        Enforces is_auto_reply_safe policy guard before sending.
        Marks all processed emails as READ to prevent duplicate reprocessing loops.
        """
        # Step A: Dispatch auto-reply if policy certifies it safe and draft exists
        if analysis.is_auto_reply_safe and analysis.suggested_reply:
            sent = self.smtp_sender.send_auto_reply(parsed_email, analysis.suggested_reply)
            if sent:
                self.metrics.replies_sent += 1
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
            flagged = client.mark_as_flagged(uid)
            read_status = client.mark_as_read(uid)
            if flagged and read_status:
                logger.info("UID %s: Successfully FLAGGED (Starred) and marked READ.", uid)
            self.metrics.urgent_actionable += 1

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

    def run(self) -> None:
        """Executes the end-to-end processing pipeline."""
        logger.info("Starting AI Email Assistant pipeline run...")

        with GmailIMAPClient() as client:
            client.select_mailbox("INBOX", readonly=False)
            unseen_uids: List[str] = client.search_unseen_emails()

            if not unseen_uids:
                logger.info("Zero unread emails found. Pipeline exiting cleanly.")
                return

            # Pick the latest batch of unread emails
            target_batch: List[str] = unseen_uids[-self.BATCH_SIZE :]
            logger.info(
                "Total Unread: %d | Processing target batch of %d emails...",
                len(unseen_uids),
                len(target_batch),
            )

            for uid in target_batch:
                self.metrics.total_scanned += 1
                logger.info("=" * 60)
                logger.info("Processing Email UID: %s", uid)

                # Step 1: Raw Bytes Fetching
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

                logger.info("Sender:  %s", parsed_email.sender)
                logger.info("Subject: %s", parsed_email.subject)

                # Step 3: Structured AI Analysis
                analysis: Optional[EmailAnalysisResult] = self.ai_engine.analyze_email(parsed_email)
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

        # Audit Summary
        self._print_execution_summary()

    def _print_execution_summary(self) -> None:
        """Prints final audit report of the pipeline run."""
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
    try:
        service = EmailAssistantService()
        service.run()
    except KeyboardInterrupt:
        logger.info("Pipeline terminated manually by user.")
        sys.exit(0)
    except Exception as fatal_err:
        logger.critical("Fatal pipeline crash: %s", fatal_err, exc_info=True)
        sys.exit(1)