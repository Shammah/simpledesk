"""SimpleDesk: a helpdesk for IT providers who look after several client companies.

Run:  python app.py   then open http://127.0.0.1:5000
"""
import calendar
import csv
import hashlib
import io
import json
import re
import secrets
import sqlite3
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from markupsafe import Markup
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import ai
import channels
import config
import db
import files
import insights
import monitoring
import payments

monitoring.init("web")   # error alerts, only when SENTRY_DSN is set
app = Flask(__name__)
# Room for a few attached pictures per message.
app.config["MAX_CONTENT_LENGTH"] = (config.MAX_FILE_MB * config.MAX_FILES_PER_MESSAGE + 1) * 1024 * 1024


def _secret_key():
    config.DATA_DIR.mkdir(exist_ok=True)
    path = config.DATA_DIR / "secret.key"
    if not path.exists():
        path.write_text(secrets.token_hex(32))
    return path.read_text().strip()


app.secret_key = _secret_key()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=config.HTTPS,
                  PREFERRED_URL_SCHEME="https" if config.HTTPS else "http")
if config.HTTPS:
    # Caddy (in front) handles HTTPS and tells us the visitor's real address and scheme.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
db.init()


# ---------------------------------------------------------------------------
# Request plumbing
# ---------------------------------------------------------------------------

@app.before_request
def before():
    g.db = db.connect()
    g.tech = None
    g.provider = None
    # Set by static/app.js when the page is open inside the Play Store app.
    # Google requires its own billing for subscriptions bought inside an
    # Android app, so plans are managed only on the website.
    g.android = (request.cookies.get("shell") == "android"
                 or request.args.get("source") == "android-app")
    if session.get("tech_id"):
        g.tech = g.db.execute(
            "SELECT * FROM technicians WHERE id = ? AND active = 1",
            (session["tech_id"],)).fetchone()
        # Changing the password signs out every other device.
        if g.tech and session.get("pw") == password_stamp(g.tech):
            g.provider = g.db.execute(
                "SELECT * FROM providers WHERE id = ?", (g.tech["provider_id"],)).fetchone()
        else:
            g.tech = None
            session.clear()
    if request.method == "POST" and request.endpoint not in ("whatsapp_webhook", "paystack_webhook"):
        token = session.get("csrf")
        if not token or not secrets.compare_digest(token, request.form.get("csrf", "")):
            abort(400, "This form has expired. Go back, refresh the page, and try again.")


@app.after_request
def remember_android(response):
    # The Android app opens /?source=android-app; remember that for later pages.
    if request.args.get("source") == "android-app" and request.cookies.get("shell") != "android":
        response.set_cookie("shell", "android", max_age=365 * 86400, samesite="Lax",
                            secure=config.HTTPS, httponly=True)
    return response


@app.teardown_request
def teardown(exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


def csrf_field():
    return Markup(f'<input type="hidden" name="csrf" value="{csrf_token()}">')


def password_stamp(tech):
    """Changes when the password or session key changes, which signs out other devices."""
    return hashlib.sha256((tech["password_hash"] + tech["session_key"]).encode()).hexdigest()[:16]


def sign_in(tech):
    session.clear()
    session["tech_id"] = tech["id"]
    session["pw"] = password_stamp(tech)


def utcnow():
    return datetime.now(timezone.utc)


def parse_time(value):
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def fmt_time(moment):
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def money(amount):
    return f"{config.CURRENCY} {amount:,.0f}"


def hours(minutes):
    """90 -> '1h 30m'."""
    minutes = int(minutes or 0)
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def due_label(t):
    """'Late by 2 hours' / 'Reply due in 3 hours' for an open ticket, else ''."""
    if t["status"] != "open" or not t["due_at"] or t["target_met"] is not None:
        return ""
    seconds = (parse_time(t["due_at"]) - utcnow()).total_seconds()
    span = abs(seconds)
    text = (f"{int(span // 3600)} hour{'s' if span >= 7200 else ''}" if span >= 3600
            else f"{max(int(span // 60), 1)} min")
    return f"Late by {text}" if seconds < 0 else f"Reply due in {text}"


def ago(value):
    if not value:
        return ""
    then = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    seconds = int((datetime.now(timezone.utc) - then).total_seconds())
    if seconds < 60:
        return "just now"
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            n = seconds // size
            return f"{n} {unit}{'s' if n != 1 else ''} ago"


def hue(name):
    """A steady colour for someone's initials circle, from their name."""
    return int(hashlib.md5((name or "?").encode()).hexdigest()[:4], 16) % 360


def initials(name):
    parts = [p for p in (name or "?").replace("Dr.", "").split() if p[:1].isalnum()]
    return ("".join(p[0] for p in parts[:2]) or "?").upper()


def iso(value):
    """Database time -> ISO with Z, for the page's own clock (countdown rings)."""
    return value.replace(" ", "T") + "Z" if value else ""


def provider_contact(provider_id):
    """The IT provider's support line, for the footer of their clients' help pages."""
    return g.db.execute("SELECT support_phone, support_hours FROM providers WHERE id = ?",
                        (provider_id,)).fetchone()


app.jinja_env.globals["provider_contact"] = provider_contact


@app.context_processor
def live_numbers():
    """For the menu badge and live updates on every signed-in page."""
    if not g.get("tech") or not g.get("provider"):
        return {}
    row = g.db.execute(
        "SELECT (SELECT COUNT(*) FROM tickets t JOIN companies c ON c.id = t.company_id"
        "  WHERE c.provider_id = ? AND t.status = 'open'),"
        " (SELECT COALESCE(MAX(id), 0) FROM activity WHERE provider_id = ?)",
        (g.provider["id"], g.provider["id"])).fetchone()
    return dict(nav_open=row[0], last_activity=row[1])


app.jinja_env.filters.update(ago=ago, money=money, hours=hours, hue=hue, initials=initials, iso=iso)
app.jinja_env.globals.update(csrf=csrf_field, csrf_token=csrf_token, app_name=config.APP_NAME,
                             currency=config.CURRENCY, priorities=config.PRIORITIES,
                             due_label=due_label, file_accept=files.ACCEPT,
                             max_files=config.MAX_FILES_PER_MESSAGE, max_file_mb=config.MAX_FILE_MB)

STATUS_LABELS = {"ai_answered": "AI answered", "open": "Needs technician", "resolved": "Resolved"}
app.jinja_env.globals["status_labels"] = STATUS_LABELS


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.tech:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def owner_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if g.tech["role"] != "owner":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


# ---- Tenant-safe lookups: every one checks the row belongs to this provider ----

def own_company(company_id):
    row = g.db.execute("SELECT * FROM companies WHERE id = ? AND provider_id = ?",
                       (company_id, g.provider["id"])).fetchone()
    if not row:
        abort(404)
    return row


def own_ticket(ticket_id):
    row = g.db.execute(
        "SELECT t.*, c.name AS company_name FROM tickets t"
        " JOIN companies c ON c.id = t.company_id"
        " WHERE t.id = ? AND c.provider_id = ?", (ticket_id, g.provider["id"])).fetchone()
    if not row:
        abort(404)
    return row


def own_article(article_id):
    row = g.db.execute("SELECT * FROM articles WHERE id = ? AND provider_id = ?",
                       (article_id, g.provider["id"])).fetchone()
    if not row:
        abort(404)
    return row


# ---------------------------------------------------------------------------
# Plans, seats and AI allowance
# ---------------------------------------------------------------------------

def month_start():
    return datetime.now(timezone.utc).strftime("%Y-%m-01 00:00:00")


def billing(provider):
    plan = config.PLANS[provider["plan"]]
    seats = g.db.execute(
        "SELECT COUNT(*) FROM technicians WHERE provider_id = ? AND active = 1",
        (provider["id"],)).fetchone()[0]
    used = g.db.execute(
        "SELECT COUNT(*) FROM ai_usage WHERE provider_id = ? AND created_at >= ?",
        (provider["id"], month_start())).fetchone()[0]
    allowance = plan["ai_replies_per_tech"] * max(seats, 1)
    # Payment state of the Team plan: paid, due (in the grace days) or none.
    state, days_left = "none", None
    if provider["plan"] == "pro" and provider["paid_until"]:
        left = parse_time(provider["paid_until"]) - utcnow()
        days_left = left.days
        state = "paid" if left.total_seconds() > 0 else "due"
    return dict(plan=plan, plan_key=provider["plan"], seats=seats,
                monthly_cost=config.PLANS["pro"]["price_per_tech"] * max(seats, 1)
                if provider["plan"] == "pro" else 0,
                team_cost=config.PLANS["pro"]["price_per_tech"] * max(seats, 1),
                ai_used=used, ai_allowance=allowance,
                ai_left=max(allowance - used, 0),
                pay_state=state, days_left=days_left, paid_until=provider["paid_until"])


def apply_payment(reference, tx):
    """Record a successful Paystack payment once, and extend the Team plan.

    Called from both the return page and the webhook; whichever comes first
    does the work. Returns the payment row, or None if nothing applied.
    """
    pay = g.db.execute("SELECT * FROM payments WHERE reference = ?", (reference,)).fetchone()
    if (not pay or tx.get("status") != "success" or tx.get("currency") != "KES"
            or int(tx.get("amount", 0)) < pay["amount"] * 100):
        return None
    claimed = g.db.execute("UPDATE payments SET status = 'paid', paid_at = ?, method = ?"
                           " WHERE id = ? AND status = 'pending'",
                           (db.now(), payments.describe(tx), pay["id"])).rowcount
    if not claimed:
        return None             # already applied
    provider = g.db.execute("SELECT * FROM providers WHERE id = ?", (pay["provider_id"],)).fetchone()
    start = utcnow()
    if provider["plan"] == "pro" and provider["paid_until"]:
        start = max(start, parse_time(provider["paid_until"]))
    until = start + timedelta(days=config.DAYS_PER_MONTH_PAID * pay["months"])
    auth = tx.get("authorization") or {}
    card = auth.get("reusable") and auth.get("channel") == "card" and auth.get("authorization_code")
    email = (tx.get("customer") or {}).get("email") or provider["billing_email"]
    g.db.execute(
        "UPDATE providers SET plan = 'pro', paid_until = ?, billing_email = ?, billing_notice = '',"
        " card_code = CASE WHEN ? != '' THEN ? ELSE card_code END,"
        " card_label = CASE WHEN ? != '' THEN ? ELSE card_label END WHERE id = ?",
        (fmt_time(until), email, card or "", card or "", card and payments.describe(tx) or "",
         card and payments.describe(tx) or "", provider["id"]))
    log_activity("payment", f"Payment received: {money(pay['amount'])}, Team plan until {until:%d %b %Y}",
                 provider_id=provider["id"])
    g.db.commit()
    return pay


# ---------------------------------------------------------------------------
# Sign up / sign in
# ---------------------------------------------------------------------------

@app.route("/favicon.ico")
def favicon():
    return redirect(url_for("static", filename="favicon.svg"), code=301)


@app.route("/healthz")
def healthz():
    """For the uptime monitor (UptimeRobot): 'ok' if the app, the database and,
    when email is switched on, the mailbox checker are all working."""
    headers = {"Content-Type": "text/plain", "Cache-Control": "no-store"}
    g.db.execute("SELECT 1").fetchone()
    if channels.email_in() and monitoring.mail_worker_stalled():
        return "mail worker stopped", 503, headers
    return "ok", 200, headers


@app.route("/privacy-policy")
def privacy_policy():
    """Public privacy policy, needed for the Play Store listing."""
    return render_template("privacy_policy.html", public=True, brand_name=config.APP_NAME,
                           business=config.BUSINESS_NAME,
                           contact=config.CONTACT_EMAIL, updated=config.POLICY_UPDATED,
                           keep_days=config.BACKUP_KEEP_DAYS)


@app.route("/.well-known/assetlinks.json")
def assetlinks():
    """Tells Android that the Play Store app and this website belong together,
    so the app opens full screen without a browser bar."""
    links = []
    if config.ANDROID_PACKAGE and config.ANDROID_CERT_SHA256:
        links.append({"relation": ["delegate_permission/common.handle_all_urls"],
                      "target": {"namespace": "android_app", "package_name": config.ANDROID_PACKAGE,
                                 "sha256_cert_fingerprints": config.ANDROID_CERT_SHA256}})
    return app.response_class(json.dumps(links), mimetype="application/json")


# ---- Installing on a home screen ----

@app.route("/manifest.webmanifest")
def manifest():
    return send_file(config.BASE_DIR / "static" / "manifest.webmanifest",
                     mimetype="application/manifest+json", max_age=3600)


@app.route("/help/<token>/manifest.webmanifest")
def portal_manifest(token):
    """A client's staff can put their company's help page on their home screen."""
    c = g.db.execute("SELECT name FROM companies WHERE portal_token = ?", (token,)).fetchone()
    if not c:
        abort(404)
    data = json.loads((config.BASE_DIR / "static" / "manifest.webmanifest").read_text(encoding="utf-8"))
    start = url_for("portal", token=token)
    data.update(name=f"IT help - {c['name']}", short_name="IT help", id=start, start_url=start,
                scope=start.rsplit("/", 1)[0] + "/", description=f"Get IT help at {c['name']}.")
    data.pop("shortcuts", None)
    return app.response_class(json.dumps(data), mimetype="application/manifest+json")


@app.route("/sw.js")
def service_worker():
    # Served from the top of the site so it can look after every page.
    response = send_file(config.BASE_DIR / "static" / "sw.js", mimetype="text/javascript", max_age=0)
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/")
def home():
    return redirect(url_for("dashboard" if g.tech else "login"))


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if g.tech:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        business = request.form.get("business", "").strip()
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not (business and name and email) or len(password) < 8:
            flash("Fill in every field. The password needs at least 8 characters.")
        elif g.db.execute("SELECT 1 FROM technicians WHERE email = ?", (email,)).fetchone():
            flash("That email already has an account. Sign in instead.")
        else:
            cur = g.db.execute("INSERT INTO providers (name, created_at) VALUES (?, ?)",
                               (business, db.now()))
            provider_id = cur.lastrowid
            cur = g.db.execute(
                "INSERT INTO technicians (provider_id, name, email, password_hash, role, created_at)"
                " VALUES (?, ?, ?, ?, 'owner', ?)",
                (provider_id, name, email, generate_password_hash(password), db.now()))
            g.db.commit()
            sign_in(g.db.execute("SELECT * FROM technicians WHERE id = ?", (cur.lastrowid,)).fetchone())
            return redirect(url_for("dashboard"))     # where the welcome tour starts
    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if g.tech:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        row = g.db.execute("SELECT * FROM technicians WHERE email = ? AND active = 1",
                           (email,)).fetchone()
        # An invited technician has no password until they open their invite link.
        if (row and row["password_hash"]
                and check_password_hash(row["password_hash"], request.form.get("password", ""))):
            sign_in(row)
            return redirect(url_for("dashboard"))
        flash("Email or password is not right.")
    return render_template("login.html")


# ---- Password reset and invites: one-time links sent by email ----

def make_link(tech_id, kind):
    """Create a one-time sign-in link. Only a fingerprint of it is stored."""
    token = secrets.token_urlsafe(24)
    life = (timedelta(hours=config.RESET_LINK_HOURS) if kind == "reset"
            else timedelta(days=config.INVITE_LINK_DAYS))
    g.db.execute("UPDATE sign_in_links SET used_at = ? WHERE tech_id = ? AND used_at IS NULL",
                 (db.now(), tech_id))       # an older link stops working
    g.db.execute("INSERT INTO sign_in_links (tech_id, kind, token_hash, expires_at, created_at)"
                 " VALUES (?, ?, ?, ?, ?)",
                 (tech_id, kind, hashlib.sha256(token.encode()).hexdigest(),
                  fmt_time(utcnow() + life), db.now()))
    return f"{channels.public_url()}{url_for('set_password', token=token)}"


def find_link(token):
    return g.db.execute(
        "SELECT l.*, t.name, t.email, t.provider_id FROM sign_in_links l"
        " JOIN technicians t ON t.id = l.tech_id"
        " WHERE l.token_hash = ? AND l.used_at IS NULL AND l.expires_at > ? AND t.active = 1",
        (hashlib.sha256(token.encode()).hexdigest(), db.now())).fetchone()


def send_in_background(to, subject, body):
    """Send an email without making the person on the page wait for it."""
    def run():
        error = channels.send_email(to, subject, body)
        if error:
            app.logger.error("Could not send email '%s': %s", subject, error)
    threading.Thread(target=run, daemon=True).start()


def link_email(tech, kind, url):
    """The subject and text of a reset or invite email."""
    business = g.db.execute("SELECT name FROM providers WHERE id = ?", (tech["provider_id"],)).fetchone()[0]
    if kind == "invite":
        subject = f"You're invited to {business} on {config.APP_NAME}"
        body = (f"Hello {tech['name']},\n\n{business} has added you as a technician on {config.APP_NAME}.\n"
                f"Choose your password here to get started:\n\n{url}\n\n"
                f"This link works once and expires in {config.INVITE_LINK_DAYS} days.")
    else:
        subject = f"Reset your {config.APP_NAME} password"
        body = (f"Hello {tech['name']},\n\nSomeone asked to reset your {config.APP_NAME} password. "
                f"To choose a new one, open:\n\n{url}\n\nThis link works once and expires in "
                f"{config.RESET_LINK_HOURS} hour. If you didn't ask for this, ignore this email: "
                "your password stays the same.")
    return subject, body


@app.route("/forgot", methods=["GET", "POST"])
def forgot():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        tech = g.db.execute("SELECT * FROM technicians WHERE email = ? AND active = 1 AND password_hash != ''",
                            (email,)).fetchone()
        if not channels.email_out():
            flash("Password emails aren't set up on this server yet. Ask your account owner to "
                  "create a reset link for you on the Team page.")
            return redirect(url_for("forgot"))
        if tech:
            subject, body = link_email(tech, "reset", make_link(tech["id"], "reset"))
            g.db.commit()
            send_in_background(tech["email"], subject, body)
        # Same answer either way, so nobody can test which emails have accounts.
        flash("If that email has an account, a reset link is on its way. Check your inbox and spam folder.")
        return redirect(url_for("login"))
    return render_template("forgot.html")


@app.route("/set-password/<token>", methods=["GET", "POST"])
def set_password(token):
    link = find_link(token)
    if not link:
        return render_template("error.html", code=410,
                               message="This link has expired or was already used. Ask for a new one."), 410
    if request.method == "POST":
        password = request.form.get("password", "")
        if len(password) < 8:
            flash("The password needs at least 8 characters.")
        elif password != request.form.get("confirm", ""):
            flash("The two passwords don't match.")
        else:
            g.db.execute("UPDATE technicians SET password_hash = ? WHERE id = ?",
                         (generate_password_hash(password), link["tech_id"]))
            g.db.execute("UPDATE sign_in_links SET used_at = ? WHERE tech_id = ? AND used_at IS NULL",
                         (db.now(), link["tech_id"]))
            g.db.commit()
            sign_in(g.db.execute("SELECT * FROM technicians WHERE id = ?", (link["tech_id"],)).fetchone())
            flash("Password saved. You're signed in.")
            return redirect(url_for("dashboard"))
    return render_template("set_password.html", link=link)


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Dashboard and pitch metrics
# ---------------------------------------------------------------------------

def metrics(provider_id, company_id=None, since=None, until=None):
    where = "c.provider_id = ?"
    args = [provider_id]
    if company_id:
        where += " AND t.company_id = ?"
        args.append(company_id)
    if since:
        where += " AND t.created_at >= ?"
        args.append(since)
    if until:
        where += " AND t.created_at < ?"
        args.append(until)
    row = g.db.execute(
        "SELECT COUNT(*) AS total,"
        " SUM(t.status = 'resolved') AS resolved,"
        " SUM(t.resolved_by = 'ai') AS by_ai,"
        " SUM(t.resolved_by = 'human') AS by_human,"
        " SUM(t.status = 'open') AS open,"
        " SUM(t.status = 'ai_answered') AS waiting,"
        " AVG(CASE WHEN t.first_reply_at IS NOT NULL THEN"
        "   (julianday(t.first_reply_at) - julianday(t.created_at)) * 1440 END) AS reply_minutes,"
        " COUNT(t.target_met) AS targeted, SUM(t.target_met) AS on_time,"
        " SUM(t.status = 'open' AND t.target_met IS NULL AND t.due_at < ?) AS late,"
        " COUNT(t.rating) AS rated, SUM(t.rating = 3) AS happy,"
        " (SELECT COALESCE(SUM(e.minutes), 0) FROM time_entries e"
        "   WHERE e.ticket_id IN (SELECT t2.id FROM tickets t2 JOIN companies c ON c.id = t2.company_id"
        f"   WHERE {where.replace('t.', 't2.')})) AS minutes"
        f" FROM tickets t JOIN companies c ON c.id = t.company_id WHERE {where}",
        [db.now()] + args + args).fetchone()
    m = {k: (row[k] or 0) for k in row.keys()}
    m["ai_rate"] = round(100 * m["by_ai"] / m["resolved"]) if m["resolved"] else None
    m["hours_saved"] = round(m["by_ai"] * config.MINUTES_SAVED_PER_AI_TICKET / 60, 1)
    m["reply_minutes"] = round(m["reply_minutes"], 1) if row["reply_minutes"] is not None else None
    m["on_time_rate"] = round(100 * m["on_time"] / m["targeted"]) if m["targeted"] else None
    m["happy_rate"] = round(100 * m["happy"] / m["rated"]) if m["rated"] else None
    return m


@app.route("/dashboard")
@login_required
def dashboard():
    pid = g.provider["id"]
    by_company = []
    for c in g.db.execute("SELECT * FROM companies WHERE provider_id = ? ORDER BY name", (pid,)):
        by_company.append((c, metrics(pid, c["id"])))
    categories = g.db.execute(
        "SELECT t.category, COUNT(*) AS n, SUM(t.resolved_by = 'ai') AS by_ai"
        " FROM tickets t JOIN companies c ON c.id = t.company_id"
        " WHERE c.provider_id = ? GROUP BY t.category ORDER BY n DESC LIMIT 6", (pid,)).fetchall()
    soon = (datetime.now(timezone.utc) + timedelta(days=60)).strftime("%Y-%m-%d")
    warranties = g.db.execute(
        "SELECT d.*, c.name AS company_name FROM devices d JOIN companies c ON c.id = d.company_id"
        " WHERE c.provider_id = ? AND d.warranty_end != '' AND d.warranty_end <= ?"
        " ORDER BY d.warranty_end LIMIT 8", (pid, soon)).fetchall()
    queue = g.db.execute(
        "SELECT t.*, c.name AS company_name FROM tickets t JOIN companies c ON c.id = t.company_id"
        " WHERE c.provider_id = ? AND t.status = 'open'"
        " ORDER BY t.target_met IS NOT NULL, t.due_at IS NULL, t.due_at, t.created_at LIMIT 8",
        (pid,)).fetchall()
    feed = g.db.execute(
        "SELECT a.*, c.name AS company_name FROM activity a LEFT JOIN companies c ON c.id = a.company_id"
        " WHERE a.provider_id = ? ORDER BY a.id DESC LIMIT 12", (pid,)).fetchall()
    all_time = metrics(pid)
    days = insights.daily(g.db, pid, 30)
    return render_template("dashboard.html", all_time=all_time,
                           month=metrics(pid, since=month_start()),
                           by_company=by_company, categories=categories,
                           warranties=warranties, queue=queue, bill=billing(g.provider),
                           today=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                           feed=feed, days=days, days_max=max([d["total"] for d in days] + [1]),
                           reply_weeks=insights.weekly_reply_hours(g.db, pid),
                           week=insights.highlights(g.db, pid), streak=insights.streak(g.db, pid),
                           my_badges=insights.badges(g.db, pid).get(g.tech["id"], []),
                           first_name=g.tech["name"].split(" ")[0])


# ---------------------------------------------------------------------------
# Tickets (technician side)
# ---------------------------------------------------------------------------

@app.route("/tickets")
@login_required
def tickets():
    status = request.args.get("status", "open")
    company_id = request.args.get("company", type=int)
    where, args = ["c.provider_id = ?"], [g.provider["id"]]
    if status in STATUS_LABELS:
        where.append("t.status = ?")
        args.append(status)
    if company_id:
        where.append("t.company_id = ?")
        args.append(company_id)
    # Waiting tickets: the ones whose reply is due soonest come first.
    order = ("t.target_met IS NOT NULL, t.due_at IS NULL, t.due_at, t.created_at" if status == "open"
             else "t.created_at DESC")
    rows = g.db.execute(
        "SELECT t.*, c.name AS company_name, tech.name AS tech_name FROM tickets t"
        " JOIN companies c ON c.id = t.company_id"
        " LEFT JOIN technicians tech ON tech.id = t.assigned_to"
        f" WHERE {' AND '.join(where)} ORDER BY {order} LIMIT 200", args).fetchall()
    all_companies = g.db.execute(
        "SELECT id, name FROM companies WHERE provider_id = ? ORDER BY name",
        (g.provider["id"],)).fetchall()
    return render_template("tickets.html", tickets=rows, status=status,
                           company_id=company_id, companies=all_companies)


@app.route("/tickets/<int:ticket_id>")
@login_required
def ticket(ticket_id):
    t = own_ticket(ticket_id)
    msgs = g.db.execute("SELECT * FROM messages WHERE ticket_id = ? ORDER BY id",
                        (ticket_id,)).fetchall()
    person = device = None
    history = []
    if t["person_id"]:
        person = g.db.execute("SELECT * FROM people WHERE id = ?", (t["person_id"],)).fetchone()
        history = g.db.execute(
            "SELECT id, subject, status, created_at FROM tickets"
            " WHERE person_id = ? AND id != ? ORDER BY created_at DESC LIMIT 5",
            (t["person_id"], ticket_id)).fetchall()
    devices = g.db.execute("SELECT * FROM devices WHERE company_id = ? AND person_id = ?",
                           (t["company_id"], t["person_id"])).fetchall() if t["person_id"] else []
    techs = g.db.execute(
        "SELECT id, name FROM technicians WHERE provider_id = ? AND active = 1 ORDER BY name",
        (g.provider["id"],)).fetchall()
    time_log = g.db.execute("SELECT * FROM time_entries WHERE ticket_id = ? ORDER BY id",
                            (ticket_id,)).fetchall()
    replies = g.db.execute("SELECT * FROM saved_replies WHERE provider_id = ? ORDER BY title",
                           (g.provider["id"],)).fetchall()
    # The staff member sees "Seen by a technician" from now on.
    g.db.execute("UPDATE tickets SET tech_seen_at = ? WHERE id = ?", (db.now(), ticket_id))
    g.db.commit()
    team = {x["name"]: x for x in g.db.execute(
        "SELECT id, name, photo FROM technicians WHERE provider_id = ?", (g.provider["id"],))}
    return render_template("ticket.html", t=t, messages=msgs, person=person, team=team,
                           last_message=msgs[-1]["id"] if msgs else 0,
                           devices=devices, history=history, techs=techs,
                           attached=files.by_message(g.db, ticket_id), time_log=time_log,
                           time_total=sum(e["minutes"] for e in time_log), saved_replies=replies)


def log_time(ticket_id, minutes, note=""):
    if minutes and 0 < minutes <= 24 * 60:
        g.db.execute("INSERT INTO time_entries (ticket_id, tech_id, tech_name, minutes, note, created_at)"
                     " VALUES (?, ?, ?, ?, ?, ?)",
                     (ticket_id, g.tech["id"], g.tech["name"], minutes, note.strip()[:200], db.now()))


def target_reached(ticket_id):
    """A technician has answered: record whether it was within the response target."""
    g.db.execute("UPDATE tickets SET target_met = (? <= due_at)"
                 " WHERE id = ? AND due_at IS NOT NULL AND target_met IS NULL", (db.now(), ticket_id))


@app.route("/tickets/<int:ticket_id>/reply", methods=["POST"])
@login_required
def ticket_reply(ticket_id):
    t = own_ticket(ticket_id)
    body = request.form.get("body", "").strip()
    uploads = [u for u in request.files.getlist("files") if u and u.filename]
    if body or uploads:
        cur = g.db.execute(
            "INSERT INTO messages (ticket_id, sender, author_name, body, created_at)"
            " VALUES (?, 'tech', ?, ?, ?)", (ticket_id, g.tech["name"], body or "(attached files)", db.now()))
        for problem in files.save_uploads(g.db, ticket_id, cur.lastrowid, uploads):
            flash(f"Not attached: {problem}")
        g.db.execute(
            "UPDATE tickets SET first_reply_at = COALESCE(first_reply_at, ?),"
            " assigned_to = COALESCE(assigned_to, ?),"
            " status = CASE WHEN status = 'ai_answered' THEN 'open' ELSE status END WHERE id = ?",
            (db.now(), g.tech["id"], ticket_id))
        target_reached(ticket_id)
        if not request.form.get("resolve"):
            log_activity("tech_reply", f"{g.tech['name']} replied to {t['requester_name']}: {t['subject']}",
                         t["company_id"], ticket_id, g.tech["name"])
        g.db.execute("UPDATE tickets SET tech_typing_until = NULL WHERE id = ?", (ticket_id,))
    log_time(ticket_id, request.form.get("minutes", type=int))
    if request.form.get("resolve"):
        target_reached(ticket_id)
        g.db.execute(
            "UPDATE tickets SET status = 'resolved', resolved_by = 'human', resolved_at = ?"
            " WHERE id = ?", (db.now(), ticket_id))
        log_activity("resolved", f"{g.tech['name']} resolved: {t['subject']}", t["company_id"], ticket_id,
                     g.tech["name"])
        # Step 4 of the loop: the human fix becomes a guide the AI can use next time.
        title = request.form.get("guide_title", "").strip()
        if request.form.get("save_guide") and title and body:
            g.db.execute(
                "INSERT INTO articles (provider_id, company_id, title, keywords, body, source, created_at)"
                " VALUES (?, ?, ?, ?, ?, 'ticket', ?)",
                (g.provider["id"], t["company_id"], title, t["subject"], body, db.now()))
            flash("Ticket resolved and the fix was saved as a guide for this company. 🎉", "celebrate")
        else:
            flash("Ticket resolved. Nice work! 🎉", "celebrate")
    g.db.commit()
    flush_outbox(ticket_id)
    return redirect(url_for("ticket", ticket_id=ticket_id))


@app.route("/tickets/<int:ticket_id>/update", methods=["POST"])
@login_required
def ticket_update(ticket_id):
    t = own_ticket(ticket_id)
    action = request.form.get("action")
    if action == "assign":
        tech_id = request.form.get("tech_id", type=int)
        valid = tech_id and g.db.execute(
            "SELECT 1 FROM technicians WHERE id = ? AND provider_id = ? AND active = 1",
            (tech_id, g.provider["id"])).fetchone()
        g.db.execute("UPDATE tickets SET assigned_to = ? WHERE id = ?",
                     (tech_id if valid else None, ticket_id))
        if valid and tech_id != g.tech["id"] and t["status"] != "resolved":
            notify_techs(ticket_id, "assigned", only=tech_id)
    elif action == "priority":
        priority = request.form.get("priority")
        if priority in config.PRIORITIES:
            g.db.execute("UPDATE tickets SET priority = ? WHERE id = ?", (priority, ticket_id))
            set_due(ticket_id)
    elif action == "log_time":
        log_time(ticket_id, request.form.get("minutes", type=int), request.form.get("note", ""))
    elif action == "remove_time":
        g.db.execute("DELETE FROM time_entries WHERE id = ? AND ticket_id = ?",
                     (request.form.get("entry_id", type=int), ticket_id))
    elif action == "reopen":
        g.db.execute("UPDATE tickets SET status = 'open', resolved_by = NULL, resolved_at = NULL"
                     " WHERE id = ?", (ticket_id,))
    elif action == "take":
        g.db.execute("UPDATE tickets SET status = 'open', assigned_to = ?,"
                     " escalation_reason = 'Taken over by a technician' WHERE id = ?",
                     (g.tech["id"], ticket_id))
    g.db.commit()
    return redirect(url_for("ticket", ticket_id=ticket_id))


# ---- Live updates: the pages ask every few seconds what has changed ----

def typing_until():
    return fmt_time(utcnow() + timedelta(seconds=8))


@app.route("/live")
@login_required
def live():
    """New activity since the page last asked, and the counts for the menu badge."""
    after = request.args.get("after", type=int) or 0
    pid = g.provider["id"]
    rows = g.db.execute(
        "SELECT a.*, c.name AS company_name FROM activity a LEFT JOIN companies c ON c.id = a.company_id"
        " WHERE a.provider_id = ? AND a.id > ? ORDER BY a.id DESC LIMIT 20", (pid, after)).fetchall() if after else []
    last = g.db.execute("SELECT COALESCE(MAX(id), 0) FROM activity WHERE provider_id = ?", (pid,)).fetchone()[0]
    counts = g.db.execute(
        "SELECT SUM(t.status = 'open'), SUM(t.status = 'open' AND t.target_met IS NULL AND t.due_at < ?)"
        " FROM tickets t JOIN companies c ON c.id = t.company_id WHERE c.provider_id = ?",
        (db.now(), pid)).fetchone()
    events = [dict(id=r["id"], kind=r["kind"], text=r["text"], actor=r["actor"], when=ago(r["created_at"]),
                   company=r["company_name"] or "", hue=hue(r["actor"] or r["company_name"] or ""),
                   initials=initials(r["actor"] or r["company_name"] or config.APP_NAME),
                   url=url_for("ticket", ticket_id=r["ticket_id"]) if r["ticket_id"] else "")
              for r in reversed(rows)]
    return jsonify(last=last, events=events, open=counts[0] or 0, late=counts[1] or 0)


@app.route("/tickets/<int:ticket_id>/state")
@login_required
def ticket_state(ticket_id):
    t = own_ticket(ticket_id)
    last = g.db.execute("SELECT COALESCE(MAX(id), 0) FROM messages WHERE ticket_id = ?", (ticket_id,)).fetchone()[0]
    typing = bool(t["user_typing_until"] and t["user_typing_until"] > db.now())
    return jsonify(key=f"{last}-{t['status']}", typing=t["requester_name"].split(" ")[0] if typing else "")


@app.route("/tickets/<int:ticket_id>/typing", methods=["POST"])
@login_required
def ticket_typing(ticket_id):
    own_ticket(ticket_id)
    g.db.execute("UPDATE tickets SET tech_typing_until = ?, tech_typing_name = ? WHERE id = ?",
                 (typing_until(), g.tech["name"].split(" ")[0], ticket_id))
    g.db.commit()
    return "", 204


# ---- Profile photos and company logos ----

def send_picture(stored):
    if not stored or not (config.UPLOAD_DIR / stored).exists():
        abort(404)
    response = send_file(config.UPLOAD_DIR / stored, mimetype=files.picture_type(stored))
    response.headers["Content-Security-Policy"] = "sandbox; default-src 'none'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "private, max-age=3600"
    return response


@app.route("/photos/<int:tech_id>")
@login_required
def tech_photo(tech_id):
    row = g.db.execute("SELECT photo FROM technicians WHERE id = ? AND provider_id = ?",
                       (tech_id, g.provider["id"])).fetchone()
    return send_picture(row["photo"] if row else "")


@app.route("/companies/<int:company_id>/logo")
@login_required
def company_logo(company_id):
    return send_picture(own_company(company_id)["logo"])


@app.route("/help/<token>/logo")
def portal_logo(token):
    row = g.db.execute("SELECT logo FROM companies WHERE portal_token = ?", (token,)).fetchone()
    return send_picture(row["logo"] if row else "")


@app.route("/files/<int:file_id>")
@login_required
def ticket_file(file_id):
    row = g.db.execute(
        "SELECT a.* FROM attachments a JOIN tickets t ON t.id = a.ticket_id"
        " JOIN companies c ON c.id = t.company_id WHERE a.id = ? AND c.provider_id = ?",
        (file_id, g.provider["id"])).fetchone()
    return send_attachment(row)


def send_attachment(row):
    if not row or not files.path(row).exists():
        abort(404)
    response = send_file(files.path(row), mimetype=row["content_type"], download_name=row["filename"],
                         as_attachment=not row["content_type"].startswith("image/"))
    # A file can never run scripts or be treated as a web page.
    response.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "private, max-age=86400"
    return response


# ---------------------------------------------------------------------------
# Companies, people and devices
# ---------------------------------------------------------------------------

@app.route("/companies", methods=["GET", "POST"])
@login_required
def companies():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if name:
            cur = g.db.execute(
                "INSERT INTO companies (provider_id, name, portal_token, created_at)"
                " VALUES (?, ?, ?, ?)",
                (g.provider["id"], name, secrets.token_urlsafe(9), db.now()))
            g.db.commit()
            return redirect(url_for("company", company_id=cur.lastrowid))
    rows = g.db.execute(
        "SELECT c.*,"
        " (SELECT COUNT(*) FROM people p WHERE p.company_id = c.id) AS people,"
        " (SELECT COUNT(*) FROM devices d WHERE d.company_id = c.id) AS devices,"
        " (SELECT COUNT(*) FROM tickets t WHERE t.company_id = c.id AND t.status = 'open') AS open"
        " FROM companies c WHERE c.provider_id = ? ORDER BY c.name", (g.provider["id"],)).fetchall()
    return render_template("companies.html", companies=rows,
                           health={c["id"]: insights.health(g.db, c["id"]) for c in rows})


@app.route("/companies/<int:company_id>")
@login_required
def company(company_id):
    c = own_company(company_id)
    people = g.db.execute("SELECT * FROM people WHERE company_id = ? ORDER BY name",
                          (company_id,)).fetchall()
    devices = g.db.execute(
        "SELECT d.*, p.name AS person_name FROM devices d"
        " LEFT JOIN people p ON p.id = d.person_id WHERE d.company_id = ? ORDER BY d.name",
        (company_id,)).fetchall()
    runs = g.db.execute(
        "SELECT r.*, (SELECT COUNT(*) FROM checklist_items i WHERE i.run_id = r.id) AS total,"
        " (SELECT COUNT(*) FROM checklist_items i WHERE i.run_id = r.id AND i.done = 1) AS done"
        " FROM checklist_runs r WHERE r.company_id = ? ORDER BY r.id DESC", (company_id,)).fetchall()
    templates = g.db.execute("SELECT * FROM checklist_templates WHERE provider_id = ?",
                             (g.provider["id"],)).fetchall()
    return render_template("company.html", c=c, people=people, devices=devices, runs=runs,
                           templates=templates, m=metrics(g.provider["id"], company_id),
                           portal_url=url_for("portal", token=c["portal_token"], _external=True),
                           today=datetime.now(timezone.utc).strftime("%Y-%m-%d"))


@app.route("/companies/<int:company_id>/people", methods=["POST"])
@login_required
def add_person(company_id):
    own_company(company_id)
    name = request.form.get("name", "").strip()
    if name:
        g.db.execute(
            "INSERT INTO people (company_id, name, email, phone, job_title, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (company_id, name, request.form.get("email", "").strip().lower(),
             channels.normalize_phone(request.form.get("phone", "")),
             request.form.get("job_title", "").strip(), db.now()))
        g.db.commit()
    return redirect(url_for("company", company_id=company_id) + "#people")


@app.route("/companies/<int:company_id>/devices", methods=["POST"])
@login_required
def add_device(company_id):
    own_company(company_id)
    name = request.form.get("name", "").strip()
    person_id = request.form.get("person_id", type=int)
    if person_id and not g.db.execute("SELECT 1 FROM people WHERE id = ? AND company_id = ?",
                                      (person_id, company_id)).fetchone():
        person_id = None
    if name:
        g.db.execute(
            "INSERT INTO devices (company_id, person_id, name, kind, serial, warranty_end, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (company_id, person_id, name, request.form.get("kind", "Laptop"),
             request.form.get("serial", "").strip(), request.form.get("warranty_end", ""), db.now()))
        g.db.commit()
    return redirect(url_for("company", company_id=company_id) + "#devices")


@app.route("/companies/<int:company_id>/remove", methods=["POST"])
@login_required
def remove_row(company_id):
    own_company(company_id)
    table = {"person": "people", "device": "devices"}.get(request.form.get("kind"))
    if table:
        g.db.execute(f"DELETE FROM {table} WHERE id = ? AND company_id = ?",
                     (request.form.get("id", type=int), company_id))
        g.db.commit()
    return redirect(url_for("company", company_id=company_id))


@app.route("/companies/<int:company_id>/settings", methods=["POST"])
@login_required
def company_settings(company_id):
    own_company(company_id)
    action = request.form.get("action")
    if action == "toggle_ai":
        g.db.execute("UPDATE companies SET ai_enabled = 1 - ai_enabled WHERE id = ?", (company_id,))
    elif action == "new_link":
        g.db.execute("UPDATE companies SET portal_token = ? WHERE id = ?",
                     (secrets.token_urlsafe(9), company_id))
        flash("New help link created. The old link no longer works.")
    elif action == "email_domain":
        domain = request.form.get("email_domain", "").strip().lower().lstrip("@")
        g.db.execute("UPDATE companies SET email_domain = ? WHERE id = ?", (domain, company_id))
        flash("Email domain saved.")
    elif action in ("logo", "remove_logo"):
        c = own_company(company_id)
        if action == "logo":
            stored, problem = files.save_picture(request.files.get("logo"), c["logo"])
            flash(problem or "Logo updated. Their staff see it on the help page.")
        else:
            files.remove_picture(c["logo"])
            stored = ""
        g.db.execute("UPDATE companies SET logo = ? WHERE id = ?", (stored, company_id))
    elif action == "report":
        email = request.form.get("report_email", "").strip().lower()
        hours_included = request.form.get("monthly_hours", type=float) or 0
        g.db.execute("UPDATE companies SET report_email = ?, report_auto = ?, monthly_hours = ? WHERE id = ?",
                     (email if "@" in email else "", 1 if request.form.get("report_auto") else 0,
                      max(hours_included, 0), company_id))
        flash("Report settings saved.")
    g.db.commit()
    return redirect(url_for("company", company_id=company_id))


def month_bounds(key):
    """'2026-10' -> ('2026-10-01 00:00:00', '2026-11-01 00:00:00', 'October 2026')."""
    year, month = (int(x) for x in key.split("-"))
    nxt = (year + month // 12, month % 12 + 1)
    return (f"{year:04d}-{month:02d}-01 00:00:00", f"{nxt[0]:04d}-{nxt[1]:02d}-01 00:00:00",
            f"{calendar.month_name[month]} {year}")


def previous_month(moment=None):
    first = (moment or utcnow()).replace(day=1)
    return (first - timedelta(days=1)).strftime("%Y-%m")


def build_report(c, key):
    """Everything in one company's monthly report."""
    start, end, label = month_bounds(key)
    m = metrics(c["provider_id"], c["id"], since=start, until=end)
    tickets = g.db.execute(
        "SELECT t.*, (SELECT COALESCE(SUM(e.minutes), 0) FROM time_entries e WHERE e.ticket_id = t.id"
        "   AND e.created_at >= ? AND e.created_at < ?) AS minutes"
        " FROM tickets t WHERE t.company_id = ? AND ((t.created_at >= ? AND t.created_at < ?)"
        "   OR t.id IN (SELECT e.ticket_id FROM time_entries e WHERE e.created_at >= ? AND e.created_at < ?))"
        " ORDER BY t.created_at", (start, end, c["id"], start, end, start, end)).fetchall()
    minutes = sum(t["minutes"] for t in tickets)
    categories = g.db.execute(
        "SELECT category, COUNT(*) AS n FROM tickets WHERE company_id = ? AND created_at >= ?"
        " AND created_at < ? GROUP BY category ORDER BY n DESC", (c["id"], start, end)).fetchall()
    soon = (parse_time(end) + timedelta(days=60)).strftime("%Y-%m-%d")
    warranties = g.db.execute(
        "SELECT d.*, p.name AS person_name FROM devices d LEFT JOIN people p ON p.id = d.person_id"
        " WHERE d.company_id = ? AND d.warranty_end != '' AND d.warranty_end <= ? ORDER BY d.warranty_end",
        (c["id"], soon)).fetchall()
    checklists = g.db.execute(
        "SELECT r.*, (SELECT COUNT(*) FROM checklist_items i WHERE i.run_id = r.id) AS total,"
        " (SELECT COUNT(*) FROM checklist_items i WHERE i.run_id = r.id AND i.done = 1) AS done"
        " FROM checklist_runs r WHERE r.company_id = ? AND r.created_at >= ? AND r.created_at < ?",
        (c["id"], start, end)).fetchall()
    provider = g.db.execute("SELECT * FROM providers WHERE id = ?", (c["provider_id"],)).fetchone()
    return dict(c=c, key=key, label=label, m=m, tickets=tickets, minutes=minutes,
                included=c["monthly_hours"] * 60, categories=categories, warranties=warranties,
                checklists=checklists, provider=provider,
                today=utcnow().strftime("%Y-%m-%d"))


def report_text(r):
    """The plain-text email version of a monthly report."""
    m = r["m"]
    lines = [f"IT support report for {r['c']['name']}: {r['label']}", f"From {r['provider']['name']}", "",
             f"Requests: {m['total']}  (resolved: {m['resolved']}, still open: {m['open'] + m['waiting']})"]
    if m["by_ai"]:
        lines.append(f"Answered instantly, no technician needed: {m['by_ai']}")
    if m["on_time_rate"] is not None:
        lines.append(f"Technician replies within the response target: {m['on_time_rate']}%")
    if m["happy_rate"] is not None:
        lines.append(f"Staff who rated the help as good: {m['happy_rate']}% of {m['rated']} ratings")
    used = f"Technician time: {hours(r['minutes'])}"
    if r["included"]:
        extra = r["minutes"] - r["included"]
        used += (f" of {hours(r['included'])} included ({hours(extra)} extra)" if extra > 0
                 else f" of {hours(r['included'])} included")
    lines += [used, ""]
    if r["categories"]:
        lines.append("Most common problems:")
        lines += [f"  - {cat['category']}: {cat['n']}" for cat in r["categories"]]
        lines.append("")
    if r["tickets"]:
        lines.append("Requests:")
        for t in r["tickets"]:
            spent = f", {hours(t['minutes'])}" if t["minutes"] else ""
            lines.append(f"  {t['created_at'][:10]}  {t['subject']} ({t['requester_name']},"
                         f" {STATUS_LABELS[t['status']].lower()}{spent})")
        lines.append("")
    if r["checklists"]:
        lines.append("Starters and leavers:")
        lines += [f"  - {x['template_name']}: {x['subject_name']} ({x['done']} of {x['total']} steps done)"
                  for x in r["checklists"]]
        lines.append("")
    if r["warranties"]:
        lines.append("Warranties ended or ending in the next 60 days:")
        lines += [f"  - {d['name']} ({d['person_name'] or 'unassigned'}): {d['warranty_end']}"
                  for d in r["warranties"]]
        lines.append("")
    lines.append("Questions about this report? Just reply to this email.")
    return "\n".join(lines)


@app.route("/companies/<int:company_id>/report", methods=["GET", "POST"])
@login_required
def company_report(company_id):
    c = own_company(company_id)
    key = request.values.get("month") or previous_month()
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", key):
        abort(400)
    r = build_report(c, key)
    if request.method == "POST":
        to = request.form.get("to", "").strip() or c["report_email"]
        if "@" not in to:
            flash("Add the client's email address first (Report settings on the company page).")
        else:
            error = channels.send_email(to, f"IT support report: {r['label']} - {c['name']}", report_text(r))
            flash(f"Not sent: {error}" if error else f"Report emailed to {to}.")
        return redirect(url_for("company_report", company_id=company_id, month=key))
    now = utcnow()
    months = [f"{(now.year * 12 + now.month - 1 - i) // 12:04d}-{(now.month - 1 - i) % 12 + 1:02d}"
              for i in range(13)]          # this month and the twelve before it
    start = parse_time(month_bounds(key)[0])
    length = calendar.monthrange(start.year, start.month)[1]
    days = insights.daily(g.db, c["provider_id"], length, company_id=c["id"], start=start)
    return render_template("report.html", months=months, days=days,
                           days_max=max([d["total"] for d in days] + [1]), **r)


@app.route("/companies/<int:company_id>/delete", methods=["POST"])
@owner_required
def company_delete(company_id):
    c = own_company(company_id)
    if request.form.get("confirm", "").strip() != c["name"]:
        flash("Type the company name exactly to confirm deletion. Nothing was deleted.")
        return redirect(url_for("company", company_id=company_id))
    files.remove_for_company(g.db, company_id)
    g.db.execute("DELETE FROM companies WHERE id = ?", (company_id,))
    g.db.commit()
    flash(f"{c['name']} and all of its tickets, people, devices and guides were deleted.")
    return redirect(url_for("companies"))


# ---------------------------------------------------------------------------
# Data export: a company can take everything with them
# ---------------------------------------------------------------------------

def _csv(rows, columns):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(columns)
    for r in rows:
        writer.writerow([r[col] for col in columns])
    return "﻿" + out.getvalue()      # BOM so Excel opens it correctly


def _company_files(c):
    cid = c["id"]
    q = g.db.execute
    out = {}
    columns = ["id", "created_at", "requester_name", "requester_email", "subject", "category",
               "priority", "status", "resolved_by", "escalation_reason", "first_reply_at",
               "resolved_at", "target_met", "rating", "rating_note"]
    out["tickets.csv"] = _csv(
        q(f"SELECT {', '.join(columns)} FROM tickets WHERE company_id = ? ORDER BY id", (cid,)).fetchall(),
        columns)
    out["time_spent.csv"] = _csv(
        q("SELECT e.ticket_id, e.created_at, e.tech_name, e.minutes, e.note FROM time_entries e"
          " JOIN tickets t ON t.id = e.ticket_id WHERE t.company_id = ? ORDER BY e.id", (cid,)).fetchall(),
        ["ticket_id", "created_at", "tech_name", "minutes", "note"])
    for a in q("SELECT a.* FROM attachments a JOIN tickets t ON t.id = a.ticket_id"
               " WHERE t.company_id = ? ORDER BY a.id", (cid,)).fetchall():
        try:
            out[f"attachments/ticket {a['ticket_id']}/{a['id']} {a['filename']}"] = files.path(a).read_bytes()
        except OSError:
            pass
    out["ticket_messages.csv"] = _csv(
        q("SELECT m.ticket_id, m.created_at, m.sender, m.author_name, m.body FROM messages m"
          " JOIN tickets t ON t.id = m.ticket_id WHERE t.company_id = ? ORDER BY m.id",
          (cid,)).fetchall(),
        ["ticket_id", "created_at", "sender", "author_name", "body"])
    out["people.csv"] = _csv(
        q("SELECT id, name, email, job_title FROM people WHERE company_id = ? ORDER BY name",
          (cid,)).fetchall(), ["id", "name", "email", "job_title"])
    out["devices.csv"] = _csv(
        q("SELECT d.id, d.name, d.kind, d.serial, d.warranty_end, p.name AS assigned_to"
          " FROM devices d LEFT JOIN people p ON p.id = d.person_id WHERE d.company_id = ?"
          " ORDER BY d.name", (cid,)).fetchall(),
        ["id", "name", "kind", "serial", "warranty_end", "assigned_to"])
    for a in q("SELECT * FROM articles WHERE company_id = ? ORDER BY id", (cid,)):
        safe = "".join(ch if ch.isalnum() or ch in " -_" else "" for ch in a["title"])[:60].strip()
        out[f"guides/{a['id']} {safe}.txt"] = f"{a['title']}\n\n{a['body']}\n"
    lines = []
    for r in q("SELECT * FROM checklist_runs WHERE company_id = ? ORDER BY id", (cid,)).fetchall():
        lines.append(f"{r['template_name']}: {r['subject_name']} ({r['created_at']})")
        for i in q("SELECT * FROM checklist_items WHERE run_id = ? ORDER BY id", (r["id"],)):
            lines.append(f"  [{'x' if i['done'] else ' '}] {i['text']}")
        lines.append("")
    out["checklists.txt"] = "\n".join(lines)
    out["README.txt"] = (
        f"Data export for {c['name']}\nCreated {db.now()} UTC by {config.APP_NAME}\n\n"
        "tickets.csv          every request\n"
        "ticket_messages.csv  the full conversation on each request\n"
        "time_spent.csv       technician time logged on each request\n"
        "attachments/         pictures and files sent with requests\n"
        "people.csv           staff\ndevices.csv          equipment and who has it\n"
        "guides/              how-to guides written for this company\n"
        "checklists.txt       starter and leaver checklists\n")
    return out


def _zip_response(file_map, filename):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in file_map.items():
            z.writestr(name, content)
    buffer.seek(0)
    return send_file(buffer, mimetype="application/zip", as_attachment=True,
                     download_name=filename)


def _slug(name):
    return "".join(ch if ch.isalnum() else "-" for ch in name).strip("-").lower() or "company"


@app.route("/companies/<int:company_id>/export")
@login_required
def company_export(company_id):
    c = own_company(company_id)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _zip_response(_company_files(c), f"{_slug(c['name'])}-export-{stamp}.zip")


@app.route("/export")
@owner_required
def export_all():
    out = {}
    for c in g.db.execute("SELECT * FROM companies WHERE provider_id = ? ORDER BY name",
                          (g.provider["id"],)).fetchall():
        for name, content in _company_files(c).items():
            out[f"{_slug(c['name'])}/{name}"] = content
    shared = g.db.execute(
        "SELECT title, keywords, body, source FROM articles"
        " WHERE provider_id = ? AND company_id IS NULL ORDER BY title", (g.provider["id"],)).fetchall()
    out["shared_guides.json"] = json.dumps([dict(r) for r in shared], indent=2)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _zip_response(out, f"{_slug(g.provider['name'])}-full-export-{stamp}.zip")


# ---------------------------------------------------------------------------
# Guides (knowledge base)
# ---------------------------------------------------------------------------

@app.route("/guides")
@login_required
def guides():
    rows = g.db.execute(
        "SELECT a.*, c.name AS company_name FROM articles a"
        " LEFT JOIN companies c ON c.id = a.company_id"
        " WHERE a.provider_id = ? ORDER BY a.company_id IS NOT NULL, c.name, a.title",
        (g.provider["id"],)).fetchall()
    return render_template("guides.html", articles=rows)


@app.route("/guides/new", methods=["GET", "POST"])
@app.route("/guides/<int:article_id>", methods=["GET", "POST"])
@login_required
def guide_edit(article_id=None):
    article = own_article(article_id) if article_id else None
    if request.method == "POST":
        if article and request.form.get("delete"):
            g.db.execute("DELETE FROM articles WHERE id = ?", (article_id,))
            g.db.commit()
            return redirect(url_for("guides"))
        title = request.form.get("title", "").strip()
        body = request.form.get("body", "").strip()
        company_id = request.form.get("company_id", type=int)
        if company_id:
            own_company(company_id)
        if title and body:
            values = (company_id or None, title, request.form.get("keywords", "").strip(), body)
            if article:
                g.db.execute("UPDATE articles SET company_id = ?, title = ?, keywords = ?, body = ?"
                             " WHERE id = ?", values + (article_id,))
            else:
                g.db.execute(
                    "INSERT INTO articles (company_id, title, keywords, body, provider_id, source, created_at)"
                    " VALUES (?, ?, ?, ?, ?, 'manual', ?)", values + (g.provider["id"], db.now()))
            g.db.commit()
            return redirect(url_for("guides"))
        flash("A guide needs a title and steps.")
    all_companies = g.db.execute("SELECT id, name FROM companies WHERE provider_id = ? ORDER BY name",
                                 (g.provider["id"],)).fetchall()
    return render_template("guide_edit.html", a=article, companies=all_companies)


@app.route("/replies", methods=["GET", "POST"])
@login_required
def saved_replies():
    """Ready-made answers technicians drop into a reply with one click."""
    pid = g.provider["id"]
    if request.method == "POST":
        reply_id = request.form.get("reply_id", type=int)
        title = request.form.get("title", "").strip()[:100]
        body = request.form.get("body", "").strip()
        if request.form.get("delete") and reply_id:
            g.db.execute("DELETE FROM saved_replies WHERE id = ? AND provider_id = ?", (reply_id, pid))
        elif not (title and body):
            flash("A saved reply needs a name and some text.")
        elif reply_id:
            g.db.execute("UPDATE saved_replies SET title = ?, body = ? WHERE id = ? AND provider_id = ?",
                         (title, body, reply_id, pid))
        else:
            g.db.execute("INSERT INTO saved_replies (provider_id, title, body) VALUES (?, ?, ?)",
                         (pid, title, body))
        g.db.commit()
        return redirect(url_for("saved_replies"))
    rows = g.db.execute("SELECT * FROM saved_replies WHERE provider_id = ? ORDER BY title", (pid,)).fetchall()
    return render_template("replies.html", replies=rows)


@app.route("/search")
@login_required
def search():
    q = request.args.get("q", "").strip()[:100]
    found = dict(tickets=[], people=[], devices=[], guides=[], companies=[])
    if re.fullmatch(r"#?\d+", q):
        t = g.db.execute("SELECT t.id FROM tickets t JOIN companies c ON c.id = t.company_id"
                         " WHERE t.id = ? AND c.provider_id = ?", (int(q.lstrip("#")), g.provider["id"])).fetchone()
        if t:
            return redirect(url_for("ticket", ticket_id=t["id"]))
    if len(q) >= 2:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        pid = g.provider["id"]
        run = lambda sql, n: g.db.execute(sql, (pid,) + (like,) * n).fetchall()   # noqa: E731
        found["tickets"] = run(
            "SELECT t.*, c.name AS company_name FROM tickets t JOIN companies c ON c.id = t.company_id"
            " WHERE c.provider_id = ? AND (t.subject LIKE ? ESCAPE '\\' OR t.requester_name LIKE ? ESCAPE '\\'"
            " OR t.requester_email LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM messages m"
            "   WHERE m.ticket_id = t.id AND m.body LIKE ? ESCAPE '\\'))"
            " ORDER BY t.created_at DESC LIMIT 25", 4)
        found["people"] = run(
            "SELECT p.*, c.name AS company_name FROM people p JOIN companies c ON c.id = p.company_id"
            " WHERE c.provider_id = ? AND (p.name LIKE ? ESCAPE '\\' OR p.email LIKE ? ESCAPE '\\'"
            " OR p.phone LIKE ? ESCAPE '\\' OR p.job_title LIKE ? ESCAPE '\\') ORDER BY p.name LIMIT 25", 4)
        found["devices"] = run(
            "SELECT d.*, c.name AS company_name, p.name AS person_name FROM devices d"
            " JOIN companies c ON c.id = d.company_id LEFT JOIN people p ON p.id = d.person_id"
            " WHERE c.provider_id = ? AND (d.name LIKE ? ESCAPE '\\' OR d.serial LIKE ? ESCAPE '\\'"
            " OR d.notes LIKE ? ESCAPE '\\') ORDER BY d.name LIMIT 25", 3)
        found["guides"] = run(
            "SELECT a.* FROM articles a WHERE a.provider_id = ? AND (a.title LIKE ? ESCAPE '\\'"
            " OR a.keywords LIKE ? ESCAPE '\\' OR a.body LIKE ? ESCAPE '\\') ORDER BY a.title LIMIT 25", 3)
        found["companies"] = run(
            "SELECT * FROM companies WHERE provider_id = ? AND name LIKE ? ESCAPE '\\' ORDER BY name LIMIT 25", 1)
    return render_template("search.html", q=q, found=found,
                           total=sum(len(v) for v in found.values()))


# ---------------------------------------------------------------------------
# Checklists
# ---------------------------------------------------------------------------

@app.route("/companies/<int:company_id>/checklists", methods=["POST"])
@login_required
def checklist_start(company_id):
    own_company(company_id)
    template = g.db.execute("SELECT * FROM checklist_templates WHERE id = ? AND provider_id = ?",
                            (request.form.get("template_id", type=int), g.provider["id"])).fetchone()
    subject = request.form.get("subject", "").strip()
    if template and subject:
        cur = g.db.execute(
            "INSERT INTO checklist_runs (company_id, template_name, subject_name, created_at)"
            " VALUES (?, ?, ?, ?)", (company_id, template["name"], subject, db.now()))
        g.db.executemany("INSERT INTO checklist_items (run_id, text) VALUES (?, ?)",
                         [(cur.lastrowid, line.strip())
                          for line in template["items"].splitlines() if line.strip()])
        g.db.commit()
        return redirect(url_for("checklist", run_id=cur.lastrowid))
    return redirect(url_for("company", company_id=company_id))


def own_run(run_id):
    row = g.db.execute(
        "SELECT r.*, c.name AS company_name FROM checklist_runs r"
        " JOIN companies c ON c.id = r.company_id WHERE r.id = ? AND c.provider_id = ?",
        (run_id, g.provider["id"])).fetchone()
    if not row:
        abort(404)
    return row


@app.route("/checklists/<int:run_id>", methods=["GET", "POST"])
@login_required
def checklist(run_id):
    run = own_run(run_id)
    if request.method == "POST":
        done = set(request.form.getlist("done", type=int))
        for item in g.db.execute("SELECT id FROM checklist_items WHERE run_id = ?", (run_id,)).fetchall():
            g.db.execute("UPDATE checklist_items SET done = ? WHERE id = ?",
                         (1 if item["id"] in done else 0, item["id"]))
        g.db.commit()
        flash("Checklist saved.")
        return redirect(url_for("checklist", run_id=run_id))
    items = g.db.execute("SELECT * FROM checklist_items WHERE run_id = ? ORDER BY id",
                         (run_id,)).fetchall()
    return render_template("checklist.html", run=run, items=items)


@app.route("/checklists", methods=["GET", "POST"])
@login_required
def checklist_templates():
    if request.method == "POST":
        template_id = request.form.get("template_id", type=int)
        name = request.form.get("name", "").strip()
        items = "\n".join(line.strip() for line in request.form.get("items", "").splitlines()
                          if line.strip())
        if request.form.get("delete") and template_id:
            g.db.execute("DELETE FROM checklist_templates WHERE id = ? AND provider_id = ?",
                         (template_id, g.provider["id"]))
        elif not (name and items):
            flash("A checklist needs a name and at least one step.")
        elif template_id:
            g.db.execute("UPDATE checklist_templates SET name = ?, items = ?"
                         " WHERE id = ? AND provider_id = ?",
                         (name, items, template_id, g.provider["id"]))
        else:
            g.db.execute("INSERT INTO checklist_templates (provider_id, name, items) VALUES (?, ?, ?)",
                         (g.provider["id"], name, items))
        g.db.commit()
        return redirect(url_for("checklist_templates"))
    rows = g.db.execute("SELECT * FROM checklist_templates WHERE provider_id = ? ORDER BY name",
                        (g.provider["id"],)).fetchall()
    return render_template("checklist_templates.html", templates=rows)


# ---------------------------------------------------------------------------
# Channels: status of email and WhatsApp
# ---------------------------------------------------------------------------

@app.route("/channels")
@login_required
def channels_page():
    failed = g.db.execute(
        "SELECT m.delivery, m.created_at, t.id AS ticket_id, t.subject FROM messages m"
        " JOIN tickets t ON t.id = m.ticket_id JOIN companies c ON c.id = t.company_id"
        " WHERE c.provider_id = ? AND m.delivery LIKE 'failed:%' ORDER BY m.id DESC LIMIT 10",
        (g.provider["id"],)).fetchall()
    email_out = channels.email_out()
    email_in = channels.email_in()
    return render_template(
        "channels.html", failed=failed,
        email_out=email_out["from_address"] if email_out else None,
        email_in=email_in["username"] if email_in else None,
        whatsapp=bool(channels.whatsapp()),
        paystack=bool(payments.settings()),
        paystack_test=(payments.settings() or {}).get("secret_key", "").startswith("sk_test_"),
        paystack_webhook=channels.public_url() + url_for("paystack_webhook"),
        webhook_url=channels.public_url() + url_for("whatsapp_webhook"),
        public_url=channels.public_url(),
        settings_exists=channels.SETTINGS_PATH.exists())


# ---------------------------------------------------------------------------
# Team, plan and privacy
# ---------------------------------------------------------------------------

@app.route("/team", methods=["GET", "POST"])
@login_required
def team():
    bill = billing(g.provider)
    pid = g.provider["id"]
    if request.method == "POST":
        action = request.form.get("action")
        if g.tech["role"] != "owner":
            abort(403)
        elif action == "add":
            return invite_technician(bill)
        elif action in ("resend", "reset_link"):
            tech = g.db.execute("SELECT * FROM technicians WHERE id = ? AND provider_id = ? AND active = 1",
                                (request.form.get("tech_id", type=int), pid)).fetchone()
            if not tech:
                abort(404)
            kind = "invite" if not tech["password_hash"] else "reset"
            share_link(tech, kind)
        elif action == "remove":
            g.db.execute(
                "UPDATE technicians SET active = 0 WHERE id = ? AND provider_id = ? AND role != 'owner'",
                (request.form.get("tech_id", type=int), pid))
        elif action in ("pay", "plan", "forget_card"):
            if g.android:
                abort(403)          # plans and payment are handled on the website only
            if action == "pay":
                return start_payment(bill)
            if action == "forget_card":
                g.db.execute("UPDATE providers SET card_code = '', card_label = '' WHERE id = ?", (pid,))
                flash("Saved card removed. You'll get a reminder to pay before your plan ends.")
            elif request.form.get("plan") == "free":
                if bill["seats"] > 1:
                    flash("Remove technicians first: the Starter plan allows 1.")
                else:
                    g.db.execute("UPDATE providers SET plan = 'free', card_code = '', card_label = ''"
                                 " WHERE id = ?", (pid,))
                    flash("You're on the Starter plan now.")
            elif request.form.get("plan") == "pro" and not payments.settings():
                # Only while Paystack isn't set up (testing): switch without paying.
                g.db.execute("UPDATE providers SET plan = 'pro', paid_until = ? WHERE id = ?",
                             (fmt_time(utcnow() + timedelta(days=config.DAYS_PER_MONTH_PAID)), pid))
        g.db.commit()
        return redirect(url_for("team"))
    techs = g.db.execute(
        "SELECT * FROM technicians WHERE provider_id = ? AND active = 1 ORDER BY role != 'owner', name",
        (pid,)).fetchall()
    history = g.db.execute("SELECT * FROM payments WHERE provider_id = ? AND status = 'paid'"
                           " ORDER BY id DESC LIMIT 12", (pid,)).fetchall()
    return render_template("team.html", techs=techs, bill=bill, plans=config.PLANS, history=history,
                           badges=insights.badges(g.db, pid),
                           payments_ready=bool(payments.settings()),
                           grace_days=config.PAYMENT_GRACE_DAYS)


def reload_me():
    return g.db.execute("SELECT * FROM technicians WHERE id = ?", (g.tech["id"],)).fetchone()


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    """Your own profile, password, alerts and how your month is going."""
    me = g.tech["id"]
    if request.method == "POST":
        action = request.form.get("action")
        if action == "profile":
            name = request.form.get("name", "").strip()[:100]
            email = request.form.get("email", "").strip().lower()
            if not name or "@" not in email:
                flash("Enter your name and email.")
            elif email != g.tech["email"] and not check_password_hash(
                    g.tech["password_hash"], request.form.get("password", "")):
                flash("To change your email, enter your current password too.")
            elif email != g.tech["email"] and g.db.execute(
                    "SELECT 1 FROM technicians WHERE email = ?", (email,)).fetchone():
                flash("That email already has an account.")
            else:
                g.db.execute("UPDATE technicians SET name = ?, email = ? WHERE id = ?", (name, email, me))
                flash("Profile saved.")
        elif action == "alerts":
            notify = request.form.get("notify")
            if notify in ("all", "mine", "none"):
                g.db.execute("UPDATE technicians SET notify = ? WHERE id = ?", (notify, me))
                flash("Email alerts saved.")
        elif action == "password":
            new = request.form.get("new_password", "")
            if not check_password_hash(g.tech["password_hash"], request.form.get("current_password", "")):
                flash("Your current password is not right.")
            elif len(new) < 8:
                flash("The new password needs at least 8 characters.")
            elif new != request.form.get("confirm_password", new):
                flash("The two new passwords don't match.")
            else:
                g.db.execute("UPDATE technicians SET password_hash = ? WHERE id = ?",
                             (generate_password_hash(new), me))
                g.db.commit()
                sign_in(reload_me())
                flash("Password changed. Other devices have been signed out.")
        elif action == "sign_out_others":
            g.db.execute("UPDATE technicians SET session_key = ? WHERE id = ?", (secrets.token_hex(8), me))
            g.db.commit()
            sign_in(reload_me())
            flash("Signed out of every other device. You're still signed in here.")
        elif action in ("photo", "remove_photo"):
            if action == "photo":
                stored, problem = files.save_picture(request.files.get("photo"), g.tech["photo"])
                flash(problem or "Photo updated.")
            else:
                files.remove_picture(g.tech["photo"])
                stored = ""
            g.db.execute("UPDATE technicians SET photo = ? WHERE id = ?", (stored, me))
        g.db.commit()
        return redirect(url_for("account"))
    month = month_start()
    mine = g.db.execute(
        "SELECT SUM(status = 'resolved' AND resolved_at >= ?) AS resolved,"
        " AVG(CASE WHEN first_reply_at >= ? AND target_met IS NOT NULL THEN"
        "   (julianday(first_reply_at) - julianday(COALESCE(waiting_since, created_at))) * 1440 END) AS reply_min,"
        " SUM(rating = 3 AND resolved_at >= ?) AS happy, SUM(rating IS NOT NULL AND resolved_at >= ?) AS rated,"
        " SUM(status = 'open') AS open"
        " FROM tickets WHERE assigned_to = ?", (month, month, month, month, me)).fetchone()
    minutes = g.db.execute("SELECT COALESCE(SUM(minutes), 0) FROM time_entries WHERE tech_id = ? AND created_at >= ?",
                           (me, month)).fetchone()[0]
    return render_template("account.html", mine=mine, minutes=minutes,
                           my_badges=insights.badges(g.db, g.provider["id"]).get(me, []))


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    """Business details (owner), how the app looks and sounds (everyone), and the danger zone."""
    owner = g.tech["role"] == "owner"
    pid = g.provider["id"]
    if request.method == "POST":
        if not owner:
            abort(403)
        action = request.form.get("action")
        if action == "business":
            name = request.form.get("business_name", "").strip()[:100]
            billing_email = request.form.get("billing_email", "").strip().lower()
            if not name:
                flash("Your business needs a name.")
            else:
                g.db.execute("UPDATE providers SET name = ?, billing_email = ?, support_phone = ?, support_hours = ?"
                             " WHERE id = ?",
                             (name, billing_email if "@" in billing_email else "",
                              request.form.get("support_phone", "").strip()[:40],
                              request.form.get("support_hours", "").strip()[:80], pid))
                flash("Business details saved.")
        elif action == "delete_business":
            if request.form.get("confirm", "").strip() != g.provider["name"]:
                flash("Type your business name exactly to confirm. Nothing was deleted.")
            elif not check_password_hash(g.tech["password_hash"], request.form.get("password", "")):
                flash("Your password is not right. Nothing was deleted.")
            else:
                for c in g.db.execute("SELECT id FROM companies WHERE provider_id = ?", (pid,)).fetchall():
                    files.remove_for_company(g.db, c["id"])
                for t in g.db.execute("SELECT photo FROM technicians WHERE provider_id = ?", (pid,)).fetchall():
                    files.remove_picture(t["photo"])
                g.db.execute("DELETE FROM technicians WHERE provider_id = ?", (pid,))
                g.db.execute("DELETE FROM providers WHERE id = ?", (pid,))
                g.db.commit()
                session.clear()
                flash("Your business account and all its data have been deleted.")
                return redirect(url_for("login"))
        g.db.commit()
        return redirect(url_for("settings"))
    counts = g.db.execute(
        "SELECT (SELECT COUNT(*) FROM companies WHERE provider_id = ?),"
        " (SELECT COUNT(*) FROM tickets t JOIN companies c ON c.id = t.company_id WHERE c.provider_id = ?),"
        " (SELECT COUNT(*) FROM technicians WHERE provider_id = ? AND active = 1)", (pid, pid, pid)).fetchone()
    return render_template("settings.html", owner=owner, counts=counts, priorities=config.PRIORITIES)


@app.route("/about")
def about():
    """Public page about SimpleDesk and the business behind it."""
    return render_template("about.html", business=config.BUSINESS_NAME, contact=config.CONTACT_EMAIL,
                           story=config.ABOUT_STORY, location=config.BUSINESS_LOCATION,
                           priorities=config.PRIORITIES)


def share_link(tech, kind):
    """Email an invite or reset link, and show it to the owner in case email fails."""
    url = make_link(tech["id"], kind)
    g.db.commit()
    error = channels.send_email(tech["email"], *link_email(tech, kind, url)) if channels.email_out() else "x"
    what = "invite" if kind == "invite" else "password reset link"
    if error:
        flash(Markup("Email isn't working, so send {} this {} yourself (it works once):"
                     "<div class='copy'>{}</div>").format(tech["name"], what, url))
    else:
        flash(f"{tech['name']} has been emailed a {what}.")


def invite_technician(bill):
    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    limit = bill["plan"]["max_techs"]
    existing = g.db.execute("SELECT * FROM technicians WHERE email = ?", (email,)).fetchone()
    if limit is not None and bill["seats"] >= limit:
        flash(f"The {bill['plan']['name']} plan includes {limit} technician. "
              "Move to the Team plan to add more.")
    elif not (name and "@" in email):
        flash("Enter their name and email.")
    elif existing and (existing["active"] or existing["provider_id"] != g.provider["id"]):
        flash("That email already has an account.")
    else:
        if existing:                 # someone removed earlier, coming back
            g.db.execute("UPDATE technicians SET name = ?, active = 1, password_hash = '', role = 'tech'"
                         " WHERE id = ?", (name, existing["id"]))
            tech_id = existing["id"]
        else:
            tech_id = g.db.execute(
                "INSERT INTO technicians (provider_id, name, email, password_hash, created_at)"
                " VALUES (?, ?, ?, '', ?)", (g.provider["id"], name, email, db.now())).lastrowid
        share_link(g.db.execute("SELECT * FROM technicians WHERE id = ?", (tech_id,)).fetchone(), "invite")
    g.db.commit()
    return redirect(url_for("team"))


def start_payment(bill):
    """Send the owner to Paystack to pay for the Team plan (M-Pesa or card)."""
    months = request.form.get("months", type=int)
    if months not in (1, 3, 12):
        abort(400)
    seats = max(bill["seats"], 1)
    amount = config.PLANS["pro"]["price_per_tech"] * seats * months
    reference = f"SD-{g.provider['id']}-{secrets.token_hex(6)}"
    g.db.execute("INSERT INTO payments (provider_id, reference, amount, seats, months, status, created_at)"
                 " VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                 (g.provider["id"], reference, amount, seats, months, db.now()))
    g.db.commit()
    try:
        checkout = payments.start(
            g.provider["billing_email"] or g.tech["email"], amount, reference,
            url_for("payment_return", _external=True),
            f"{config.APP_NAME} Team plan, {seats} technician(s), {months} month(s)")
    except payments.PaymentError as exc:
        g.db.execute("UPDATE payments SET status = 'failed' WHERE reference = ?", (reference,))
        g.db.commit()
        flash(str(exc))
        return redirect(url_for("team"))
    return redirect(checkout)


@app.route("/billing/return")
@owner_required
def payment_return():
    """Paystack sends the owner back here after paying."""
    reference = request.args.get("reference", "")
    pay = g.db.execute("SELECT * FROM payments WHERE reference = ? AND provider_id = ?",
                       (reference, g.provider["id"])).fetchone()
    if not pay:
        abort(404)
    if pay["status"] == "paid":
        flash("Payment received. Thank you!")
        return redirect(url_for("team"))
    try:
        tx = payments.check(reference)
    except payments.PaymentError as exc:
        flash(f"{exc} If you paid, it will show here within a few minutes.")
        return redirect(url_for("team"))
    if apply_payment(reference, tx):
        flash("Payment received. Thank you! Your Team plan is active.")
    elif tx.get("status") in ("ongoing", "pending", "processing"):
        flash("Your payment is still being confirmed (M-Pesa can take a minute). "
              "Refresh this page shortly.")
    else:
        flash("The payment didn't go through, and you were not charged. You can try again.")
    return redirect(url_for("team"))


@app.route("/webhooks/paystack", methods=["POST"])
def paystack_webhook():
    """Paystack tells us about payments here, even if the owner closed the page."""
    if not payments.signature_ok(request.get_data(), request.headers.get("X-Paystack-Signature")):
        abort(403)
    event = request.get_json(silent=True) or {}
    reference = (event.get("data") or {}).get("reference", "")
    if event.get("event") == "charge.success" and reference:
        try:
            apply_payment(reference, payments.check(reference))    # trust Paystack's own record
        except payments.PaymentError as exc:
            app.logger.error("Could not confirm Paystack payment %s: %s", reference, exc)
            abort(500)       # Paystack retries later
    return "", 200


@app.route("/tour/done", methods=["POST"])
@login_required
def tour_done():
    """The welcome tour was finished or skipped; don't show it again on its own."""
    g.db.execute("UPDATE technicians SET tour_done = 1 WHERE id = ?", (g.tech["id"],))
    g.db.commit()
    return "", 204


@app.route("/privacy")
@login_required
def privacy():
    return render_template("privacy.html", db_path=f"{config.DATA_DIR.name}/{config.DB_PATH.name}", model=config.AI_MODEL,
                           max_replies=config.AI_MAX_REPLIES_PER_TICKET,
                           rules=[r[0] for r in ai.REQUEST_RULES])


# ---------------------------------------------------------------------------
# The AI's turn on a ticket
# ---------------------------------------------------------------------------

def run_ai(ticket_row, company_row):
    """Give the AI its turn on a ticket, inside every limit. Commits nothing."""
    tid = ticket_row["id"]
    provider = g.db.execute("SELECT * FROM providers WHERE id = ?",
                            (company_row["provider_id"],)).fetchone()

    def hand_over(reason, message=ai.HANDOVER, category=None):
        g.db.execute("INSERT INTO messages (ticket_id, sender, author_name, body, created_at)"
                     " VALUES (?, 'system', '', ?, ?)", (tid, message, db.now()))
        g.db.execute("UPDATE tickets SET status = 'open', escalation_reason = ?,"
                     " category = COALESCE(?, category) WHERE id = ?", (reason, category, tid))

    if not company_row["ai_enabled"]:
        return hand_over("AI replies are switched off for this company")
    replies = g.db.execute("SELECT COUNT(*) FROM messages WHERE ticket_id = ? AND sender = 'ai'",
                           (tid,)).fetchone()[0]
    if replies >= config.AI_MAX_REPLIES_PER_TICKET:
        return hand_over("AI already tried and the problem continues")
    if billing(provider)["ai_left"] <= 0:
        return hand_over("Monthly AI reply allowance used up")

    msgs = g.db.execute("SELECT sender, body FROM messages WHERE ticket_id = ?"
                        " AND sender IN ('user', 'ai') ORDER BY id", (tid,)).fetchall()
    conversation = [("Staff member" if m["sender"] == "user" else "Assistant", m["body"])
                    for m in msgs]
    # The AI only ever sees shared guides and this company's own guides.
    articles = g.db.execute(
        "SELECT id, company_id, title, keywords, body FROM articles"
        " WHERE provider_id = ? AND (company_id IS NULL OR company_id = ?)",
        (provider["id"], company_row["id"])).fetchall()

    result = ai.triage(conversation, articles)
    if result["action"] == "escalate":
        return hand_over(result["reason"], result["reply"], result["category"])
    g.db.execute("INSERT INTO messages (ticket_id, sender, author_name, body, created_at)"
                 " VALUES (?, 'ai', ?, ?, ?)",
                 (tid, "AI assistant" if result["engine"] == "model" else "Automatic guide",
                  result["reply"], db.now()))
    g.db.execute("UPDATE tickets SET status = 'ai_answered', category = ?,"
                 " first_reply_at = COALESCE(first_reply_at, ?) WHERE id = ?",
                 (result["category"], db.now(), tid))
    g.db.execute("INSERT INTO ai_usage (provider_id, ticket_id, engine, created_at) VALUES (?, ?, ?, ?)",
                 (provider["id"], tid, result["engine"], db.now()))
    log_activity("ai", f"Answered instantly: {ticket_row['subject']}", company_row["id"], tid,
                 provider_id=provider["id"])



# ---------------------------------------------------------------------------
# What staff can do, whichever way they reach us (help page, email, WhatsApp)
# ---------------------------------------------------------------------------

def log_activity(kind, text, company_id=None, ticket_id=None, actor="", provider_id=None):
    """Add a line to the activity feed (and so to everyone's live updates)."""
    if provider_id is None:
        provider_id = g.db.execute("SELECT provider_id FROM companies WHERE id = ?", (company_id,)).fetchone()[0]
    g.db.execute("INSERT INTO activity (provider_id, company_id, ticket_id, kind, actor, text, created_at)"
                 " VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (provider_id, company_id, ticket_id, kind, actor, text[:300], db.now()))


RATING_FACES = {3: "😀", 2: "😐", 1: "🙁"}


def set_due(ticket_id):
    """Work out when a technician's reply is due, from the ticket's priority."""
    t = g.db.execute("SELECT priority, waiting_since FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if t and t["waiting_since"]:
        hours_allowed = config.PRIORITIES.get(t["priority"], config.PRIORITIES["normal"])["hours"]
        g.db.execute("UPDATE tickets SET due_at = ? WHERE id = ?",
                     (fmt_time(parse_time(t["waiting_since"]) + timedelta(hours=hours_allowed)), ticket_id))


EVENTS = {"needs_tech": "Needs a technician", "reply": "New reply", "assigned": "Assigned to you",
          "late": "Reply overdue"}


def notify_techs(ticket_id, event, only=None):
    """Email the technicians who should know about this ticket, in the background."""
    if not channels.email_out():
        return
    t = g.db.execute("SELECT t.*, c.name AS company_name, c.provider_id FROM tickets t"
                     " JOIN companies c ON c.id = t.company_id WHERE t.id = ?", (ticket_id,)).fetchone()
    techs = g.db.execute("SELECT * FROM technicians WHERE provider_id = ? AND active = 1"
                         " AND password_hash != '' AND notify != 'none'", (t["provider_id"],)).fetchall()
    if only:
        to = [x for x in techs if x["id"] == only]
    elif t["assigned_to"] and event != "late":
        to = [x for x in techs if x["id"] == t["assigned_to"]]
    else:
        to = [x for x in techs if x["notify"] == "all" or x["id"] == t["assigned_to"]]
    me = g.get("tech")
    to = [x for x in to if not (me and x["id"] == me["id"])]
    if not to:
        return
    last = g.db.execute("SELECT body FROM messages WHERE ticket_id = ? AND sender = 'user'"
                        " ORDER BY id DESC LIMIT 1", (ticket_id,)).fetchone()
    priority = config.PRIORITIES.get(t["priority"], config.PRIORITIES["normal"])["label"]
    subject = f"{EVENTS[event]}: {t['subject']} [{t['company_name']}] #{t['id']}"
    lines = [f"{EVENTS[event]} - {t['company_name']}", "",
             f"From: {t['requester_name']}", f"Priority: {priority}"]
    if t["due_at"] and t["target_met"] is None and t["status"] == "open":
        lines.append(f"Reply due by: {t['due_at']} UTC")
    if t["escalation_reason"] and event == "needs_tech":
        lines.append(f"Why: {t['escalation_reason']}")
    if last:
        excerpt = last["body"] if len(last["body"]) <= 600 else last["body"][:600] + "..."
        lines += ["", excerpt]
    lines += ["", f"Open the ticket: {channels.public_url()}/tickets/{t['id']}", "",
              f"To change which emails you get: {channels.public_url()}/team"]
    for tech in to:
        send_in_background(tech["email"], subject, "\n".join(lines))


def finish_staff_turn(ticket_id, before, wrote=True):
    """After a staff member acts (help page, email or WhatsApp): if the ticket
    has just come to a technician, start its response clock and alert the team;
    if it was already with one and they wrote more, tell the technician."""
    t = g.db.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if t["status"] != "open":
        return
    if before != "open":
        g.db.execute("UPDATE tickets SET waiting_since = ?, target_met = NULL, late_alerted = 0"
                     " WHERE id = ?", (db.now(), ticket_id))
        set_due(ticket_id)
        urgent = "🔴 " if t["priority"] == "urgent" else ""
        log_activity("needs_tech", f"{urgent}{t['subject']} needs a technician", t["company_id"], ticket_id,
                     t["requester_name"])
        g.db.commit()
        notify_techs(ticket_id, "needs_tech")
    elif wrote:
        log_activity("reply", f"{t['requester_name']} replied: {t['subject']}", t["company_id"], ticket_id,
                     t["requester_name"])
        g.db.commit()
        notify_techs(ticket_id, "reply")


def open_ticket(c, name, email, problem, channel="web", person=None, subject="", priority="normal"):
    """Create a ticket and give the AI its turn. Returns the ticket row."""
    problem = ai.redact(problem.strip())
    if person is None and email:
        person = g.db.execute("SELECT * FROM people WHERE company_id = ? AND email = ?",
                              (c["id"], email)).fetchone()
    if person:
        person_id = person["id"]
    else:
        person_id = g.db.execute(
            "INSERT INTO people (company_id, name, email, created_at) VALUES (?, ?, ?, ?)",
            (c["id"], name, email, db.now())).lastrowid
    device = g.db.execute("SELECT id FROM devices WHERE company_id = ? AND person_id = ?"
                          " ORDER BY id LIMIT 1", (c["id"], person_id)).fetchone()
    subject = ai.redact(subject.strip()) or problem.splitlines()[0].split(". ")[0].rstrip(".")
    if len(subject) > 80:
        subject = subject[:77].rsplit(" ", 1)[0] + "..."
    cur = g.db.execute(
        "INSERT INTO tickets (company_id, person_id, device_id, requester_name, requester_email,"
        " subject, category, status, public_token, channel, priority, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)",
        (c["id"], person_id, device["id"] if device else None, name, email, subject,
         ai.categorize(problem), secrets.token_urlsafe(16), channel,
         priority if priority in config.PRIORITIES else "normal", db.now()))
    g.db.execute("INSERT INTO messages (ticket_id, sender, author_name, body, delivery, created_at)"
                 " VALUES (?, 'user', ?, ?, 'n/a', ?)", (cur.lastrowid, name, problem, db.now()))
    log_activity("new", f"{name} at {c['name']} asked: {subject}", c["id"], cur.lastrowid, name)
    t = g.db.execute("SELECT * FROM tickets WHERE id = ?", (cur.lastrowid,)).fetchone()
    run_ai(t, c)
    return t


def first_message_id(ticket_id):
    return g.db.execute("SELECT MIN(id) FROM messages WHERE ticket_id = ?", (ticket_id,)).fetchone()[0]


def staff_reply(t, c, body, attach=()):
    """The staff member adds detail (and maybe files) to their ticket.

    attach: (filename, bytes) pairs. Returns reasons any file was refused."""
    body = ai.redact(body.strip())
    if not body and not attach:
        return []
    if t["status"] == "resolved":
        g.db.execute("UPDATE tickets SET status = 'open', resolved_by = NULL, resolved_at = NULL,"
                     " escalation_reason = 'Reopened by staff member' WHERE id = ?", (t["id"],))
    cur = g.db.execute("INSERT INTO messages (ticket_id, sender, author_name, body, delivery, created_at)"
                       " VALUES (?, 'user', ?, ?, 'n/a', ?)",
                       (t["id"], t["requester_name"], body or "(sent a picture or file)", db.now()))
    problems = [p for name, content in attach[:config.MAX_FILES_PER_MESSAGE]
                if (p := files.save(g.db, t["id"], cur.lastrowid, name, content))]
    if t["status"] == "ai_answered" and body:
        run_ai(t, c)
    return problems


def staff_marks(t, action):
    """The staff member says it is fixed ('solved') or asks for a person ('human')."""
    def note(text):
        g.db.execute("INSERT INTO messages (ticket_id, sender, author_name, body, created_at)"
                     " VALUES (?, 'system', '', ?, ?)", (t["id"], text, db.now()))

    if action == "solved" and t["status"] != "resolved":
        # Credit the AI only if no technician was involved.
        by = "ai" if t["status"] == "ai_answered" else "human"
        g.db.execute("UPDATE tickets SET status = 'resolved', resolved_by = ?, resolved_at = ?"
                     " WHERE id = ?", (by, db.now(), t["id"]))
        log_activity("resolved", ("Solved instantly, no technician needed: " if by == "ai"
                                  else f"{t['requester_name']} confirmed it's fixed: ") + t["subject"],
                     t["company_id"], t["id"], t["requester_name"])
        if t["channel"] != "web":
            note("Glad that is sorted. This request is now closed.")
    elif action == "human" and t["status"] == "ai_answered":
        note(ai.HANDOVER)
        g.db.execute("UPDATE tickets SET status = 'open',"
                     " escalation_reason = 'Staff member asked for a technician' WHERE id = ?",
                     (t["id"],))


FIXED_WORDS = {"1", "fixed", "solved", "yes", "it is fixed", "its fixed", "it's fixed", "done", "resolved"}
HELP_WORDS = {"2", "help", "no", "technician", "human", "still need help", "not fixed"}


def handle_inbound(c, person, text, channel, ticket=None, subject="", attach=()):
    """Route one incoming email or WhatsApp message, with any attached files.

    Alerts technicians if needed. Returns the ticket id."""
    before = ticket["status"] if ticket else None
    word = text.strip().lower().strip(".! ")
    wrote = False
    if ticket and word in FIXED_WORDS and not attach:
        staff_marks(ticket, "solved")       # does nothing if it is already closed
    elif ticket and ticket["status"] == "ai_answered" and word in HELP_WORDS and not attach:
        staff_marks(ticket, "human")
    elif ticket:
        staff_reply(ticket, c, text, attach)
        wrote = True
    else:
        ticket = open_ticket(c, person["name"], person["email"], text, channel, person, subject)
        for name, content in attach[:config.MAX_FILES_PER_MESSAGE]:
            files.save(g.db, ticket["id"], first_message_id(ticket["id"]), name, content)
    g.db.commit()
    finish_staff_turn(ticket["id"], before, wrote)
    return ticket["id"]


# ---------------------------------------------------------------------------
# Sending replies back out by email or WhatsApp
# ---------------------------------------------------------------------------

def ticket_tag(t):
    """Goes in email subjects so a reply finds its ticket. Hard to guess."""
    return f"[SD-{t['id']}-{t['public_token'][:6]}]"


def flush_outbox(ticket_id):
    """Send any replies on this ticket that have not gone out yet, then commit."""
    t = g.db.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    pending = g.db.execute("SELECT * FROM messages WHERE ticket_id = ? AND delivery = ''"
                           " AND sender != 'user' ORDER BY id", (ticket_id,)).fetchall()
    if not t or not pending:
        return
    person = g.db.execute("SELECT * FROM people WHERE id = ?", (t["person_id"],)).fetchone()
    link = f"{channels.public_url()}/help/t/{t['public_token']}"
    last_id = pending[-1]["id"]
    with_files = {r[0] for r in g.db.execute(
        "SELECT DISTINCT message_id FROM attachments WHERE ticket_id = ?", (ticket_id,))}
    for m in pending:
        waiting = m["id"] == last_id and t["status"] == "ai_answered"
        closed = m["id"] == last_id and t["status"] == "resolved" and m["sender"] == "tech"
        if t["channel"] == "whatsapp":
            body = m["body"]
            if m["id"] in with_files:
                body += f"\n\nSee the attached files: {link}"
            if waiting:
                body += "\n\nReply 1 if that fixed it, or 2 to get a technician."
            if closed:
                body += f"\n\nThis request is now closed. How did we do? Tap to rate: {link}"
            error = (channels.send_whatsapp(person["phone"], body) if person and person["phone"]
                     else "no WhatsApp number on record")
            result = "sent by WhatsApp"
        elif t["requester_email"] and (t["channel"] == "email"
                                       or (m["sender"] == "tech" and channels.email_out())):
            # Help-page tickets get an email only when a technician replies.
            who = f"{m['author_name']}, technician, wrote:\n\n" if m["sender"] == "tech" else ""
            if m["id"] in with_files:
                who = f"{who}(Files are attached on the conversation page below.)\n\n"
            if waiting:
                footer = "Did that fix it? Reply to this email with FIXED, or with HELP to get a technician."
            elif closed:
                footer = (f"This request is now closed. How did we do? Rate it in one tap: {link}\n"
                          "If the problem comes back, just reply to this email.")
            else:
                footer = "Reply to this email to add more detail."
            body = f"{who}{m['body']}\n\n--\n{footer}\nSee the whole conversation: {link}"
            error = channels.send_email(t["requester_email"], f"Re: {t['subject']} {ticket_tag(t)}", body)
            result = "sent by email"
        else:
            error, result = "", "n/a"
        g.db.execute("UPDATE messages SET delivery = ? WHERE id = ?",
                     (f"failed: {error}" if error else result, m["id"]))
    g.db.commit()


def seen_before(key):
    """True if this incoming message was already handled. Records it if not."""
    try:
        g.db.execute("INSERT INTO inbound_seen (id, created_at) VALUES (?, ?)", (key, db.now()))
        g.db.commit()
        return False
    except sqlite3.IntegrityError:
        return True


UNKNOWN_SENDER = ("Thanks for your message. We could not match you to a company we support, "
                  "so no request was opened. Please ask your IT contact to add your details, "
                  "or use your company's IT help link.")


# ---- Email in ----

def process_email(mail):
    if mail["automatic"] or seen_before(mail["id"]):
        return
    own = [cfg["username"].lower() for cfg in (channels.email_in(), channels.email_out()) if cfg]
    if not mail["address"] or mail["address"] in own:
        return
    text = mail["body"] or mail["subject"]
    if not text:
        return

    ticket = None
    tag = re.search(r"\[SD-(\d+)-([\w-]{6})\]", mail["subject"])
    if tag:
        ticket = g.db.execute("SELECT * FROM tickets WHERE id = ? AND requester_email = ?",
                              (int(tag.group(1)), mail["address"])).fetchone()
        if ticket and ticket["public_token"][:6] != tag.group(2):
            ticket = None

    person = None
    if ticket:
        company_id = ticket["company_id"]
    else:
        person = g.db.execute("SELECT * FROM people WHERE email = ? ORDER BY id LIMIT 1",
                              (mail["address"],)).fetchone()
        if person:
            company_id = person["company_id"]
        else:
            # Not a known person: accept them if their email domain belongs to a company.
            domain = mail["address"].rsplit("@", 1)[-1]
            match = g.db.execute("SELECT id FROM companies WHERE email_domain = ? ORDER BY id LIMIT 1",
                                 (domain,)).fetchone()
            if not match:
                if not seen_before("unknown:" + mail["address"]):
                    channels.send_email(mail["address"], "Re: " + mail["subject"], UNKNOWN_SENDER)
                return
            company_id = match["id"]
            new_id = g.db.execute(
                "INSERT INTO people (company_id, name, email, created_at) VALUES (?, ?, ?, ?)",
                (company_id, mail["name"], mail["address"], db.now())).lastrowid
            person = g.db.execute("SELECT * FROM people WHERE id = ?", (new_id,)).fetchone()
    c = g.db.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    ticket_id = handle_inbound(c, person, text, "email", ticket, mail["subject"],
                               attach=mail.get("files") or ())
    g.db.commit()
    flush_outbox(ticket_id)


# ---------------------------------------------------------------------------
# Background jobs: renewals, overdue tickets, monthly reports
# ---------------------------------------------------------------------------

BILLING_STAGES = {"reminder": 1, "charged": 2, "due": 2, "downgraded": 3}


def billing_round():
    """Remind, renew or downgrade Team plans as their paid time runs out."""
    team_page = f"{channels.public_url()}/team"
    for p in g.db.execute("SELECT * FROM providers WHERE plan = 'pro' AND paid_until IS NOT NULL").fetchall():
        owner = g.db.execute("SELECT * FROM technicians WHERE provider_id = ? AND role = 'owner'"
                             " AND active = 1 ORDER BY id LIMIT 1", (p["id"],)).fetchone()
        if not owner:
            continue
        to = p["billing_email"] or owner["email"]
        until = parse_time(p["paid_until"])
        done_stage, _, done_for = p["billing_notice"].partition(":")
        done = BILLING_STAGES.get(done_stage, 0) if done_for == p["paid_until"] else 0
        now = utcnow()

        def mark(stage):
            g.db.execute("UPDATE providers SET billing_notice = ? WHERE id = ?",
                         (f"{stage}:{p['paid_until']}", p["id"]))
            g.db.commit()

        if now < until:
            if done < 1 and until - now <= timedelta(days=config.RENEWAL_REMINDER_DAYS):
                mark("reminder")
                how = (f"We'll renew it automatically with your {p['card_label']}." if p["card_code"]
                       else f"To keep it, pay with M-Pesa or card here: {team_page}")
                send_in_background(to, f"Your {config.APP_NAME} Team plan renews on {until:%d %B}",
                                   f"Hello {owner['name']},\n\nYour Team plan is paid until "
                                   f"{until:%d %B %Y}. {how}\n\nThank you for using {config.APP_NAME}.")
        elif now < until + timedelta(days=config.PAYMENT_GRACE_DAYS):
            if done >= 2:
                continue
            if p["card_code"]:
                mark("charged")           # before charging, so a crash can never charge twice
                renew_with_card(p, owner, to, team_page)
            else:
                mark("due")
                send_in_background(to, f"Your {config.APP_NAME} Team plan payment is due",
                                   f"Hello {owner['name']},\n\nYour Team plan ended on {until:%d %B}. "
                                   f"It keeps working for {config.PAYMENT_GRACE_DAYS} more days while you "
                                   f"pay. Pay with M-Pesa or card here: {team_page}")
        elif done < 3:
            mark("downgraded")
            g.db.execute("UPDATE providers SET plan = 'free' WHERE id = ?", (p["id"],))
            g.db.commit()
            send_in_background(to, f"Your {config.APP_NAME} account moved to the Starter plan",
                               f"Hello {owner['name']},\n\nWe didn't receive payment for the Team plan, so "
                               "your account is now on the free Starter plan. Nothing has been deleted. "
                               f"Pay any time to switch back: {team_page}")


def renew_with_card(p, owner, to, team_page):
    seats = max(g.db.execute("SELECT COUNT(*) FROM technicians WHERE provider_id = ? AND active = 1",
                             (p["id"],)).fetchone()[0], 1)
    amount = config.PLANS["pro"]["price_per_tech"] * seats
    reference = f"SD-{p['id']}-{secrets.token_hex(6)}"
    g.db.execute("INSERT INTO payments (provider_id, reference, amount, seats, months, status, automatic,"
                 " created_at) VALUES (?, ?, ?, ?, 1, 'pending', 1, ?)",
                 (p["id"], reference, amount, seats, db.now()))
    g.db.commit()
    try:
        tx = payments.charge_saved_card(p["card_code"], to, amount, reference)
    except payments.PaymentError as exc:
        tx = {"status": "failed", "gateway_response": str(exc)}
    if apply_payment(reference, tx):
        send_in_background(to, f"{config.APP_NAME} payment received: {money(amount)}",
                           f"Hello {owner['name']},\n\nWe charged {money(amount)} to your {p['card_label']} "
                           f"for 1 month of the Team plan ({seats} technician{'s' if seats != 1 else ''}). "
                           f"Thank you!\n\nReceipts and billing: {team_page}")
    elif tx.get("status") == "failed":
        g.db.execute("UPDATE payments SET status = 'failed' WHERE reference = ?", (reference,))
        g.db.commit()
        send_in_background(to, f"{config.APP_NAME}: we couldn't charge your card",
                           f"Hello {owner['name']},\n\nWe tried to renew your Team plan with your "
                           f"{p['card_label']}, but the payment didn't go through. Please pay with "
                           f"M-Pesa or another card within {config.PAYMENT_GRACE_DAYS} days to keep the "
                           f"Team plan: {team_page}")
    # Anything else (still processing) is confirmed later by the Paystack webhook.


def late_round():
    """Alert technicians once about each ticket whose reply is overdue."""
    for t in g.db.execute("SELECT id FROM tickets WHERE status = 'open' AND target_met IS NULL"
                          " AND due_at < ? AND late_alerted = 0", (db.now(),)).fetchall():
        g.db.execute("UPDATE tickets SET late_alerted = 1 WHERE id = ?", (t["id"],))
        g.db.commit()
        notify_techs(t["id"], "late")


def report_round():
    """On the 1st of the month, email last month's report to clients who get it automatically."""
    if utcnow().day != 1 or not channels.email_out():
        return
    key = previous_month()
    for c in g.db.execute("SELECT * FROM companies WHERE report_auto = 1 AND report_email != ''"
                          " AND last_report != ?", (key,)).fetchall():
        r = build_report(c, key)
        error = channels.send_email(c["report_email"], f"IT support report: {r['label']} - {c['name']}",
                                    report_text(r))
        if error:
            app.logger.error("Could not email the monthly report for company %s: %s", c["id"], error)
        else:
            g.db.execute("UPDATE companies SET last_report = ? WHERE id = ?", (key, c["id"]))
            g.db.commit()


def run_chores():
    with app.app_context():
        g.db = db.connect()
        try:
            for chore in (billing_round, late_round, report_round):
                try:
                    chore()
                except Exception:
                    g.db.rollback()
                    app.logger.exception("Background job %s failed", chore.__name__)
        finally:
            g.db.close()


CHORE_EVERY_SECONDS = 15 * 60


def email_poller():
    """Background loop: check the helpdesk mailbox once a minute, and run the
    background jobs every 15 minutes."""
    mailbox_failing = False
    last_chores = 0.0
    while True:
        monitoring.mail_beat()
        if time.time() - last_chores >= CHORE_EVERY_SECONDS:
            last_chores = time.time()
            run_chores()
        if channels.email_in():
            with app.app_context():
                g.db = db.connect()
                try:
                    for mail in channels.fetch_emails():
                        try:
                            process_email(mail)
                        except Exception:                    # one bad email must not stop the rest
                            g.db.rollback()
                            app.logger.exception("Could not process email from %s", mail["address"])
                    if mailbox_failing:
                        app.logger.warning("Mailbox reachable again.")
                    mailbox_failing = False
                except Exception as exc:
                    # Report the first failure only, not one alert a minute while it lasts.
                    log = app.logger.warning if mailbox_failing else app.logger.error
                    log("Could not check the mailbox: %s", exc)
                    mailbox_failing = True
                finally:
                    g.db.close()
        time.sleep(60)


# ---- WhatsApp in ----

@app.route("/webhooks/whatsapp", methods=["GET", "POST"])
def whatsapp_webhook():
    cfg = channels.whatsapp()
    if not cfg:
        abort(404)
    if request.method == "GET":       # Meta checking the address when you set it up
        if (request.args.get("hub.mode") == "subscribe"
                and secrets.compare_digest(request.args.get("hub.verify_token", ""), cfg["verify_token"])):
            return request.args.get("hub.challenge", ""), 200
        abort(403)
    if not channels.whatsapp_signature_ok(request.get_data(), request.headers.get("X-Hub-Signature-256")):
        abort(403)
    for m in channels.parse_whatsapp(request.get_json(silent=True) or {}):
        if not m["phone"] or seen_before(m["id"]):
            continue
        person = g.db.execute("SELECT * FROM people WHERE phone = ? ORDER BY id LIMIT 1",
                              (m["phone"],)).fetchone()
        if not person:
            if not seen_before("unknown:" + m["phone"]):
                channels.send_whatsapp(m["phone"], UNKNOWN_SENDER)
            continue
        c = g.db.execute("SELECT * FROM companies WHERE id = ?", (person["company_id"],)).fetchone()
        ticket = g.db.execute(
            "SELECT * FROM tickets WHERE person_id = ? AND channel = 'whatsapp'"
            " AND status != 'resolved' ORDER BY id DESC LIMIT 1", (person["id"],)).fetchone()
        attach = ()
        if m.get("media"):
            content = channels.fetch_whatsapp_media(m["media"]["id"])
            attach = [(m["media"]["filename"], content)] if content else ()
        # A new request needs words; a picture alone can only add to an open one.
        if not m["text"] and not (ticket and attach):
            channels.send_whatsapp(m["phone"], "Please describe the problem in a text message. "
                                   "You can send pictures with it too.")
            continue
        ticket_id = handle_inbound(c, person, m["text"] or "", "whatsapp", ticket, attach=attach)
        g.db.commit()
        flush_outbox(ticket_id)
    return "", 200


# ---------------------------------------------------------------------------
# Public help page: where a client's staff ask for help. No login.
# ---------------------------------------------------------------------------

@app.route("/help/<token>", methods=["GET", "POST"])
def portal(token):
    c = g.db.execute("SELECT * FROM companies WHERE portal_token = ?", (token,)).fetchone()
    if not c:
        abort(404)
    if request.method == "POST":
        name = request.form.get("name", "").strip()[:100]
        email = request.form.get("email", "").strip().lower()[:200]
        problem = request.form.get("problem", "").strip()
        if not (name and problem):
            flash("Please add your name and describe the problem.")
            return render_template("portal.html", c=c, public=True)
        # Staff choose between three plain-language levels; technicians can set "high" too.
        urgency = request.form.get("urgency")
        t = open_ticket(c, name, email, problem,
                        priority=urgency if urgency in ("urgent", "normal", "low") else "normal")
        for problem_note in files.save_uploads(g.db, t["id"], first_message_id(t["id"]),
                                               request.files.getlist("files")):
            flash(f"Not attached: {problem_note}")
        g.db.commit()
        finish_staff_turn(t["id"], None)
        flush_outbox(t["id"])
        return redirect(url_for("portal_ticket", public_token=t["public_token"], sent=1))
    return render_template("portal.html", c=c, public=True, topics=HELP_TOPICS)


# Quick-pick buttons on the help page: (emoji, label, words to start the description)
HELP_TOPICS = [
    ("🖨", "Printer", "The printer "), ("📶", "Internet", "The internet "), ("🔑", "Password", "I can't sign in to "),
    ("✉️", "Email", "My email "), ("💻", "Laptop", "My computer "), ("📱", "Phone", "My phone "),
    ("🧩", "Software", "I need help with the program "), ("💬", "Something else", ""),
]


@app.route("/help/t/<public_token>", methods=["GET", "POST"])
def portal_ticket(public_token):
    t = g.db.execute("SELECT * FROM tickets WHERE public_token = ?", (public_token,)).fetchone()
    if not t:
        abort(404)
    c = g.db.execute("SELECT * FROM companies WHERE id = ?", (t["company_id"],)).fetchone()
    if request.method == "POST":
        action = request.form.get("action")
        wrote = False
        if action in ("solved", "human"):
            staff_marks(t, action)
        elif action == "reply":
            uploads = [(u.filename, u.read(config.MAX_FILE_MB * 1024 * 1024 + 1))
                       for u in request.files.getlist("files") if u and u.filename]
            wrote = bool(request.form.get("body", "").strip() or uploads)
            for problem in staff_reply(t, c, request.form.get("body", ""), uploads):
                flash(f"Not attached: {problem}")
        elif action == "rate" and t["status"] == "resolved":
            rating = request.form.get("rating", type=int)
            if rating in (1, 2, 3):
                g.db.execute("UPDATE tickets SET rating = ?, rating_note = ? WHERE id = ?",
                             (rating, ai.redact(request.form.get("note", "").strip()[:1000]), t["id"]))
                if not t["rating"]:
                    log_activity("rated", f"{t['requester_name']} rated the help {RATING_FACES[rating]}",
                                 t["company_id"], t["id"], t["requester_name"])
                flash("Thank you for letting us know.")
        g.db.commit()
        finish_staff_turn(t["id"], t["status"], wrote)
        flush_outbox(t["id"])
        return redirect(url_for("portal_ticket", public_token=public_token))
    msgs = g.db.execute("SELECT * FROM messages WHERE ticket_id = ? ORDER BY id", (t["id"],)).fetchall()
    last_user = max((m["created_at"] for m in msgs if m["sender"] == "user"), default="")
    seen = bool(t["tech_seen_at"] and t["tech_seen_at"] >= last_user and t["status"] == "open")
    return render_template("portal_ticket.html", t=t, c=c, messages=msgs, public=True, seen=seen,
                           just_sent=request.args.get("sent") == "1",
                           attached=files.by_message(g.db, t["id"]))


@app.route("/help/t/<public_token>/state")
def portal_state(public_token):
    """What the staff member's page needs to know: new replies, typing, seen."""
    t = g.db.execute("SELECT * FROM tickets WHERE public_token = ?", (public_token,)).fetchone()
    if not t:
        abort(404)
    last = g.db.execute("SELECT COALESCE(MAX(id), 0) FROM messages WHERE ticket_id = ?", (t["id"],)).fetchone()[0]
    typing = t["tech_typing_until"] and t["tech_typing_until"] > db.now()
    last_user = g.db.execute("SELECT MAX(created_at) FROM messages WHERE ticket_id = ? AND sender = 'user'",
                             (t["id"],)).fetchone()[0] or ""
    seen = int(bool(t["tech_seen_at"] and t["tech_seen_at"] >= last_user and t["status"] == "open"))
    return jsonify(key=f"{last}-{t['status']}-{t['rating'] or 0}-{seen}",
                   typing=t["tech_typing_name"] if typing else "")


@app.route("/help/t/<public_token>/typing", methods=["POST"])
def portal_typing(public_token):
    g.db.execute("UPDATE tickets SET user_typing_until = ? WHERE public_token = ?", (typing_until(), public_token))
    g.db.commit()
    return "", 204


@app.route("/help/t/<public_token>/files/<int:file_id>")
def portal_file(public_token, file_id):
    row = g.db.execute("SELECT a.* FROM attachments a JOIN tickets t ON t.id = a.ticket_id"
                       " WHERE a.id = ? AND t.public_token = ?", (file_id, public_token)).fetchone()
    return send_attachment(row)


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
def error_page(err):
    return render_template("error.html", code=err.code, message=err.description), err.code


if __name__ == "__main__":
    threading.Thread(target=email_poller, daemon=True).start()
    app.run(host="127.0.0.1", port=5000, debug=False)
