"""Sending and receiving over email (SMTP + IMAP) and WhatsApp (Meta Cloud API).

This file only moves messages in and out. What happens to a message once it
arrives is decided in app.py. Settings are read from data/channels.ini; see
channels.example.ini.
"""
import configparser
import email
import email.policy
import hashlib
import hmac
import imaplib
import json
import re
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import parseaddr

import config

SETTINGS_PATH = config.DATA_DIR / "channels.ini"


def settings():
    """Read the settings file fresh each time, so edits apply without a restart."""
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(SETTINGS_PATH, encoding="utf-8")
    return {name: {k: v.strip() for k, v in parser[name].items()}
            for name in ("app", "email_out", "email_in", "whatsapp", "paystack")
            if parser.has_section(name)}


def _section(name, required):
    section = settings().get(name, {})
    return section if all(section.get(key) for key in required) else None


def email_out():
    return _section("email_out", ("host", "username", "password", "from_address"))


def email_in():
    return _section("email_in", ("host", "username", "password"))


def whatsapp():
    return _section("whatsapp", ("phone_number_id", "access_token", "verify_token", "app_secret"))


def public_url():
    return settings().get("app", {}).get("public_url", "").rstrip("/") or "http://127.0.0.1:5000"


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def send_email(to, subject, body):
    """Return '' on success, or a short reason on failure."""
    cfg = email_out()
    if not cfg:
        return "email sending is not set up"
    msg = EmailMessage()
    msg["From"] = cfg["from_address"]
    msg["To"] = to
    msg["Subject"] = subject
    msg["Auto-Submitted"] = "auto-replied"      # stops other systems auto-answering us
    msg.set_content(body)
    port = int(cfg.get("port") or 587)
    try:
        if port == 465:
            server = smtplib.SMTP_SSL(cfg["host"], port, timeout=20)
        else:
            server = smtplib.SMTP(cfg["host"], port, timeout=20)
            server.starttls()
        with server:
            server.login(cfg["username"], cfg["password"])
            server.send_message(msg)
        return ""
    except (smtplib.SMTPException, OSError) as exc:
        return str(exc)[:200]


def clean_email_body(text):
    """Keep what the person wrote; drop the quoted earlier messages and signature."""
    kept = []
    for line in text.replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if stripped.startswith(">") or stripped == "--":
            break
        if re.match(r"^(On .+ wrote:|-{2,} ?Original Message ?-{2,}|From: .+)$", stripped, re.IGNORECASE):
            break
        kept.append(line)
    return "\n".join(kept).strip()


def _plain_text(msg):
    part = msg.get_body(preferencelist=("plain",))
    if part is not None:
        return part.get_content()
    part = msg.get_body(preferencelist=("html",))
    if part is not None:
        html = re.sub(r"(?is)<(script|style).*?</\1>", "", part.get_content())
        html = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", html)
        return re.sub(r"<[^>]+>", "", html)
    return ""


def fetch_emails():
    """Return unread emails as dicts and mark them read. Raises on connection errors."""
    cfg = email_in()
    if not cfg:
        return []
    found = []
    box = imaplib.IMAP4_SSL(cfg["host"], int(cfg.get("port") or 993), timeout=30)
    try:
        box.login(cfg["username"], cfg["password"])
        box.select(cfg.get("folder") or "INBOX")
        _, data = box.search(None, "UNSEEN")
        for num in data[0].split()[:50]:
            _, parts = box.fetch(num, "(RFC822)")       # fetching also marks it read
            msg = email.message_from_bytes(parts[0][1], policy=email.policy.default)
            name, address = parseaddr(str(msg.get("From", "")))
            automatic = (str(msg.get("Auto-Submitted", "no")).lower() != "no"
                         or str(msg.get("Precedence", "")).lower() in ("bulk", "junk", "list")
                         or address.lower().startswith(("mailer-daemon", "postmaster", "no-reply", "noreply")))
            files = []
            for part in msg.iter_attachments():
                content = part.get_payload(decode=True)
                if content:
                    files.append((part.get_filename() or "attachment", content))
            found.append(dict(
                id="email:" + str(msg.get("Message-ID", "") or f"{address}:{msg.get('Date', '')}"),
                address=address.lower(), name=name or address.split("@")[0],
                subject=str(msg.get("Subject", "")).strip(),
                body=clean_email_body(_plain_text(msg)), automatic=automatic, files=files))
    finally:
        try:
            box.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
    return found


# ---------------------------------------------------------------------------
# WhatsApp (Meta Cloud API)
# ---------------------------------------------------------------------------

def normalize_phone(number):
    """Digits only, with country code and no leading zeros or plus: 250788123456."""
    return re.sub(r"\D", "", number or "").lstrip("0")


def send_whatsapp(to, body):
    """Return '' on success, or a short reason on failure."""
    cfg = whatsapp()
    if not cfg:
        return "WhatsApp is not set up"
    url = (f"https://graph.facebook.com/{cfg.get('api_version') or 'v24.0'}/"
           f"{cfg['phone_number_id']}/messages")
    payload = json.dumps({"messaging_product": "whatsapp", "to": normalize_phone(to),
                          "type": "text", "text": {"body": body[:4000]}}).encode()
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "Authorization": f"Bearer {cfg['access_token']}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20):
            return ""
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode())["error"]["message"]
        except (ValueError, KeyError, TypeError):
            detail = exc.reason
        return f"{exc.code} {detail}"[:200]
    except (urllib.error.URLError, OSError) as exc:
        return str(exc)[:200]


def whatsapp_signature_ok(raw_body, header):
    """Check the message really came from Meta."""
    cfg = whatsapp()
    if not cfg or not header:
        return False
    expected = "sha256=" + hmac.new(cfg["app_secret"].encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def parse_whatsapp(payload):
    """Pull the incoming messages out of a webhook delivery."""
    found = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): c.get("profile", {}).get("name", "")
                     for c in value.get("contacts", [])}
            for m in value.get("messages", []):
                phone = normalize_phone(m.get("from", ""))
                kind = m.get("type")
                text, media = None, None
                if kind == "text":
                    text = m.get("text", {}).get("body", "")
                elif kind in ("image", "document"):
                    # A picture or file, maybe with a caption. Fetched later with fetch_whatsapp_media.
                    info = m.get(kind, {})
                    text = info.get("caption", "")
                    media = dict(id=info.get("id", ""), filename=info.get("filename") or kind)
                found.append(dict(
                    id="wa:" + m.get("id", ""), phone=phone, name=names.get(m.get("from"), ""),
                    text=text, media=media))
    return found


def fetch_whatsapp_media(media_id):
    """Download a picture or file someone sent on WhatsApp. Returns bytes, or None."""
    cfg = whatsapp()
    if not cfg or not media_id:
        return None
    auth = {"Authorization": f"Bearer {cfg['access_token']}"}
    try:
        info_url = f"https://graph.facebook.com/{cfg.get('api_version') or 'v24.0'}/{media_id}"
        with urllib.request.urlopen(urllib.request.Request(info_url, headers=auth), timeout=20) as resp:
            file_url = json.loads(resp.read().decode())["url"]
        with urllib.request.urlopen(urllib.request.Request(file_url, headers=auth), timeout=60) as resp:
            return resp.read(20 * 1024 * 1024)
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return None
