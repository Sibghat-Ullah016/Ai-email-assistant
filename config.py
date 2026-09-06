import os
from dotenv import load_dotenv

# Load local environment file if present
load_dotenv()

raw_gmail_user = os.getenv("GMAIL_USER")
raw_gmail_app_password = os.getenv("GMAIL_APP_PASSWORD")
raw_gemini_api_key = os.getenv("GEMINI_API_KEY")
raw_imap_server = os.getenv("IMAP_SERVER", "imap.gmail.com").strip()
raw_imap_port = os.getenv("IMAP_PORT", "993").strip()
raw_smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com").strip()
raw_smtp_port = os.getenv("SMTP_PORT", "465").strip()

# Validate critical credentials
if not raw_gmail_user or not raw_gmail_user.strip():
    raise ValueError("Configuration Error: 'GMAIL_USER' is missing or empty in .env file.")

if not raw_gmail_app_password or not raw_gmail_app_password.strip():
    raise ValueError("Configuration Error: 'GMAIL_APP_PASSWORD' is missing or empty in .env file.")

if not raw_gemini_api_key or not raw_gemini_api_key.strip():
    raise ValueError("Configuration Error: 'GEMINI_API_KEY' is missing or empty in .env file.")

# Validate and convert ports safely
def _parse_port(port_str: str, var_name: str) -> int:
    try:
        port_num = int(port_str)
        if not (1 <= port_num <= 65535):
            raise ValueError(f"Port must be between 1 and 65535. Received: {port_num}")
        return port_num
    except ValueError as err:
        raise ValueError(f"Configuration Error: Invalid '{var_name}' value in .env: {port_str}") from err

parsed_imap_port = _parse_port(raw_imap_port, "IMAP_PORT")
parsed_smtp_port = _parse_port(raw_smtp_port, "SMTP_PORT")

# Exported typed constants
GMAIL_USER: str = raw_gmail_user.strip()
GMAIL_APP_PASSWORD: str = raw_gmail_app_password.replace(" ", "").strip()
GEMINI_API_KEY: str = raw_gemini_api_key.strip()
IMAP_SERVER: str = raw_imap_server
IMAP_PORT: int = parsed_imap_port
SMTP_SERVER: str = raw_smtp_server
SMTP_PORT: int = parsed_smtp_port

# Master switch for autonomous auto-replies (Default: True)
raw_auto_reply = os.getenv("AUTO_REPLY_ENABLED", "true").strip().lower()
AUTO_REPLY_ENABLED: bool = raw_auto_reply in ("true", "1", "yes")