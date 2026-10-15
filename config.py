"""Settings you are most likely to change. Everything here is safe to edit.

On the server, the values marked "server" are set in /etc/simpledesk.env
instead of here (see deploy/DEPLOY.md), so this file stays the same everywhere.
"""
import os
from pathlib import Path

APP_NAME = "SimpleDesk"

BASE_DIR = Path(__file__).parent
# server: SIMPLEDESK_DATA_DIR keeps the database outside the code folder.
DATA_DIR = Path(os.environ.get("SIMPLEDESK_DATA_DIR") or BASE_DIR / "data")
DB_PATH = DATA_DIR / "helpdesk.db"

# server: SIMPLEDESK_HTTPS=1 when the app sits behind HTTPS (Caddy). Makes
# sign-in cookies HTTPS-only and trusts Caddy's note that the visit was secure.
HTTPS = os.environ.get("SIMPLEDESK_HTTPS") == "1"

# ---- Shown on the public privacy policy page ----
BUSINESS_NAME = os.environ.get("SIMPLEDESK_BUSINESS_NAME", "SimpleDesk")
CONTACT_EMAIL = os.environ.get("SIMPLEDESK_CONTACT_EMAIL", "privacy@example.com")   # change before publishing
POLICY_UPDATED = "16 October 2026"

# ---- The public "About us" page ----
# Write your own story here, in your own words. Blank lines start new paragraphs.
BUSINESS_LOCATION = os.environ.get("SIMPLEDESK_LOCATION", "Nairobi, Kenya")
ABOUT_STORY = """\
"""

# ---- Android app (Play Store) ----
# server: from PWABuilder after you build the Android package. Together they
# prove to Android that the app and this website belong together.
ANDROID_PACKAGE = os.environ.get("SIMPLEDESK_ANDROID_PACKAGE", "")
ANDROID_CERT_SHA256 = [f.strip() for f in os.environ.get("SIMPLEDESK_ANDROID_SHA256", "").split(",") if f.strip()]

# ---- Backups ----
BACKUP_DIR = Path(os.environ.get("SIMPLEDESK_BACKUP_DIR") or DATA_DIR / "backups")
BACKUP_KEEP_DAYS = 30

# ---- Pictures and files people attach to tickets ----
UPLOAD_DIR = DATA_DIR / "uploads"
MAX_FILE_MB = 5
MAX_FILES_PER_MESSAGE = 3

# ---- Pricing: charged per technician, per month, in Kenyan shillings ----
# Payments go through Paystack (M-Pesa and cards). Keys go in data/channels.ini.
CURRENCY = "KES"
PLANS = {
    "free": {
        "name": "Starter",
        "price_per_tech": 0,
        "max_techs": 1,          # None = unlimited
        "ai_replies_per_tech": 50,   # per month
    },
    "pro": {
        "name": "Team",
        "price_per_tech": 2500,
        "max_techs": None,
        "ai_replies_per_tech": 500,
    },
}
DAYS_PER_MONTH_PAID = 30
# After the paid time ends, the Team plan keeps working this many days while
# the customer pays, then drops back to Starter.
PAYMENT_GRACE_DAYS = 7
RENEWAL_REMINDER_DAYS = 3

# ---- Response targets: hours until a technician should first reply ----
# Counted from when the ticket comes to a technician, in plain clock hours.
PRIORITIES = {
    "urgent": {"label": "Urgent", "hours": 1},
    "high": {"label": "High", "hours": 4},
    "normal": {"label": "Normal", "hours": 8},
    "low": {"label": "Low", "hours": 24},
}

# ---- Sign-in links sent by email ----
RESET_LINK_HOURS = 1
INVITE_LINK_DAYS = 7

# ---- AI limits ----
AI_MODEL = os.environ.get("HELPDESK_AI_MODEL", "claude-opus-5-5")
# How many times the AI may answer on one ticket before a human takes over.
AI_MAX_REPLIES_PER_TICKET = 2
# Requests longer than this go straight to a human instead of being cut short.
AI_MAX_REQUEST_CHARS = 4000

# ---- Pitch metrics ----
# Used for the "technician hours saved" figure on the dashboard.
MINUTES_SAVED_PER_AI_TICKET = 15
