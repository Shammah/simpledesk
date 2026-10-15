"""Checks the helpdesk mailbox once a minute.

On your own computer, `python app.py` already does this. On the server the
website runs in several copies at once, so the mailbox is checked here, by one
separate service, to avoid answering the same email twice.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import email_poller  # noqa: E402
import monitoring  # noqa: E402

if __name__ == "__main__":
    if monitoring.DSN:              # importing app already switched Sentry on
        import sentry_sdk
        sentry_sdk.set_tag("component", "mail")
    email_poller()
