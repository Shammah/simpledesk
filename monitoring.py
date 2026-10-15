"""Error alerts through Sentry, and the heartbeat that /healthz checks.

Sentry is off unless SENTRY_DSN is set (on the server, in /etc/simpledesk.env).
What gets sent is kept to the technical minimum: no cookies, form data, query
strings or variable values, and email addresses, phone numbers, passwords and
help-link codes are blanked out of whatever is left.
"""
import os
import re
import time

import config

DSN = os.environ.get("SENTRY_DSN", "").strip()
ENVIRONMENT = os.environ.get("SENTRY_ENVIRONMENT", "production")
# Share of page visits timed for the Performance tab. 0 keeps within the free plan.
TRACES = float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0") or 0)

BACKUP_MONITOR = "nightly-backup"
MAIL_BEAT = config.DATA_DIR / "mail-worker.beat"

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_PHONE = re.compile(r"\+?\d[\d \-()]{7,}\d")
_HELP_LINK = re.compile(r"(/help/(?:t/)?)[\w-]{8,}")


def _scrub_text(text):
    import ai  # imported late: ai pulls in the anthropic library
    text = _HELP_LINK.sub(r"\1[link]", text)
    text = _EMAIL.sub("[email]", text)
    text = _PHONE.sub("[phone]", text)
    return ai.redact(text)


# Report fields that are Sentry's own bookkeeping and must stay as they are.
_KEEP = {"contexts", "sdk", "modules", "timestamp", "start_timestamp", "release", "platform"}


def _walk(value):
    if isinstance(value, str):
        return _scrub_text(value)
    if isinstance(value, list):
        return [_walk(v) for v in value]
    if isinstance(value, dict):
        return {k: v if (k in _KEEP or k.endswith("_id")) else _walk(v) for k, v in value.items()}
    return value


def _scrub(event, hint=None):
    request = event.get("request") or {}
    for key in ("cookies", "data", "query_string", "env"):
        request.pop(key, None)
    headers = request.get("headers") or {}
    for key in list(headers):
        if key.lower() in ("cookie", "authorization", "x-hub-signature-256", "x-csrf-token"):
            headers.pop(key)
    # Blank out personal details anywhere else in the report (messages, URLs, breadcrumbs).
    return _walk(event)


def init(component):
    """Turn on Sentry for one part of the system: web, mail or backup."""
    if not DSN:
        return False
    import sentry_sdk
    sentry_sdk.init(
        dsn=DSN,
        environment=ENVIRONMENT,
        send_default_pii=False,
        include_local_variables=False,
        max_request_body_size="never",
        traces_sample_rate=TRACES,
        before_send=_scrub,
        before_send_transaction=_scrub,
    )
    sentry_sdk.set_tag("component", component)
    return True


def backup_checkin(status, check_in_id=None):
    """Tell Sentry the nightly backup started ('in_progress') or ended ('ok'/'error').

    Sentry also raises an alert when the backup does not report in at all.
    """
    if not DSN:
        return None
    from sentry_sdk.crons import capture_checkin
    return capture_checkin(
        monitor_slug=BACKUP_MONITOR, check_in_id=check_in_id, status=status,
        monitor_config={
            "schedule": {"type": "crontab", "value": "30 2 * * *"},   # matches simpledesk-backup.timer
            "timezone": "UTC",
            "checkin_margin": 60,     # minutes late before alerting
            "max_runtime": 30,        # minutes
        })


def mail_beat():
    """Called by the mailbox checker on every round, so /healthz can tell it is alive."""
    try:
        MAIL_BEAT.write_text(str(int(time.time())))
    except OSError:
        pass


def mail_worker_stalled(limit=300):
    """True when mailbox checking is switched on but has not run for `limit` seconds."""
    try:
        last = int(MAIL_BEAT.read_text())
    except (OSError, ValueError):
        return True
    return time.time() - last > limit
