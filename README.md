# Autonomous AI Email Assistant

An enterprise-grade, autonomous email triage and auto-reply agent powered by Google Gemini and Python IMAP/SMTP stack.

## Architecture Highlights
- **Deterministic AI Triage:** Structured classification (`URGENT`, `ACTION_REQUIRED`, `INFORMATIONAL`, `SPAM_OR_PROMOTIONAL`) using Pydantic enforcement.
- **RFC-Compliant Auto-Reply Engine:** Thread-safe dispatch via SSL/465 with `In-Reply-To`, `References`, and `Auto-Submitted: auto-replied` loop prevention (RFC 3834).
- **Enterprise Safety Policies:** Zero-authority constraints on financial, scheduling, and legal commitments; indirect prompt injection neutralization.
- **24-Hour Per-Recipient Cooldown:** Persistent cache blocking email bombing and bot loops.

## Installation & Setup

1. **Clone repository:**
   ```bash
   git clone [https://github.com/your-username/ai-email-assistant.git](https://github.com/your-username/ai-email-assistant.git)
   cd ai-email-assistant
   ```

2. **Setup virtual environment:**
   ```bash
   python -m venv venv
   # Windows:
   .\venv\Scripts\activate
   # Linux/Mac:
   source venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment:**
   Copy `.env.example` to `.env` and fill in your Gmail App Password and Gemini API key:
   ```bash
   cp .env.example .env
   ```

5. **Run the assistant:**
   ```bash
   python main.py
   ```