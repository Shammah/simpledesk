"""Taking payment through Paystack: M-Pesa and cards, in Kenyan shillings.

The secret key goes in data/channels.ini under [paystack]. Card numbers and
M-Pesa PINs are entered on Paystack's own page and never reach this app; for
cards, Paystack gives back a code that lets us charge the same card again for
renewals.
"""
import hashlib
import hmac
import json
import urllib.error
import urllib.request

import channels

API = "https://api.paystack.co"


class PaymentError(Exception):
    pass


def settings():
    return channels._section("paystack", ("secret_key",))


def _call(method, path, payload=None):
    cfg = settings()
    if not cfg:
        raise PaymentError("Online payment is not set up yet.")
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(API + path, data=data, method=method, headers={
        "Authorization": f"Bearer {cfg['secret_key']}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode()).get("message", exc.reason)
        except (ValueError, AttributeError):
            detail = exc.reason
        raise PaymentError(f"Paystack said: {detail}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise PaymentError("Could not reach Paystack. Please try again.") from exc
    if not body.get("status"):
        raise PaymentError(f"Paystack said: {body.get('message', 'unknown error')}")
    return body.get("data") or {}


def start(email, amount, reference, return_url, note):
    """Open a Paystack checkout. Returns the page to send the customer to."""
    data = _call("POST", "/transaction/initialize", {
        "email": email, "amount": int(amount) * 100, "currency": "KES",
        "reference": reference, "callback_url": return_url,
        "channels": ["mobile_money", "card"], "metadata": {"note": note}})
    return data["authorization_url"]


def check(reference):
    """Ask Paystack how a payment went. Returns its transaction details."""
    return _call("GET", f"/transaction/verify/{reference}")


def charge_saved_card(card_code, email, amount, reference):
    """Charge a card the customer saved earlier. Returns the transaction details."""
    return _call("POST", "/transaction/charge_authorization", {
        "authorization_code": card_code, "email": email, "amount": int(amount) * 100,
        "currency": "KES", "reference": reference})


def signature_ok(raw_body, header):
    """Check a webhook really came from Paystack."""
    cfg = settings()
    if not cfg or not header:
        return False
    expected = hmac.new(cfg["secret_key"].encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, header)


def describe(tx):
    """'M-Pesa' or 'Visa ending 4081', for receipts and the Team page."""
    auth = tx.get("authorization") or {}
    if tx.get("channel") == "card" or auth.get("channel") == "card":
        brand = (auth.get("brand") or auth.get("card_type") or "Card").strip().title()
        return f"{brand} ending {auth.get('last4', '')}".strip()
    if tx.get("channel") == "mobile_money":
        return (auth.get("bank") or "M-Pesa").strip()     # Paystack names the network, e.g. M-PESA
    return (tx.get("channel") or "").replace("_", " ").title()
