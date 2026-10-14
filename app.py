"""SimpleDesk: a helpdesk for IT providers who look after several client companies.

Run:  python app.py   then open http://127.0.0.1:5000
"""
import csv
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

from flask import (Flask, abort, flash, g, redirect, render_template, request,
                   send_file, session, url_for)
from markupsafe import Markup
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import ai
import channels
import config
import db

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024


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
        if g.tech:
            g.provider = g.db.execute(
                "SELECT * FROM providers WHERE id = ?", (g.tech["provider_id"],)).fetchone()
        else:
            session.clear()
    if request.method == "POST" and request.endpoint != "whatsapp_webhook":
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


app.jinja_env.filters["ago"] = ago
app.jinja_env.globals.update(csrf=csrf_field, csrf_token=csrf_token, app_name=config.APP_NAME,
                             currency=config.CURRENCY)

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
    return dict(plan=plan, plan_key=provider["plan"], seats=seats,
                monthly_cost=plan["price_per_tech"] * seats,
                ai_used=used, ai_allowance=allowance,
                ai_left=max(allowance - used, 0))


# ---------------------------------------------------------------------------
# Sign up / sign in
# ---------------------------------------------------------------------------

@app.route("/favicon.ico")
def favicon():
    return redirect(url_for("static", filename="favicon.svg"), code=301)


@app.route("/healthz")
def healthz():
    """For an uptime monitor: answers 'ok' if the app and database respond."""
    g.db.execute("SELECT 1").fetchone()
    return "ok", 200, {"Content-Type": "text/plain", "Cache-Control": "no-store"}


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
            session.clear()
            session["tech_id"] = cur.lastrowid
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
        if row and check_password_hash(row["password_hash"], request.form.get("password", "")):
            session.clear()
            session["tech_id"] = row["id"]
            return redirect(url_for("dashboard"))
        flash("Email or password is not right.")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Dashboard and pitch metrics
# ---------------------------------------------------------------------------

def metrics(provider_id, company_id=None, since=None):
    where = "c.provider_id = ?"
    args = [provider_id]
    if company_id:
        where += " AND t.company_id = ?"
        args.append(company_id)
    if since:
        where += " AND t.created_at >= ?"
        args.append(since)
    row = g.db.execute(
        "SELECT COUNT(*) AS total,"
        " SUM(t.status = 'resolved') AS resolved,"
        " SUM(t.resolved_by = 'ai') AS by_ai,"
        " SUM(t.resolved_by = 'human') AS by_human,"
        " SUM(t.status = 'open') AS open,"
        " SUM(t.status = 'ai_answered') AS waiting,"
        " AVG(CASE WHEN t.first_reply_at IS NOT NULL THEN"
        "   (julianday(t.first_reply_at) - julianday(t.created_at)) * 1440 END) AS reply_minutes"
        f" FROM tickets t JOIN companies c ON c.id = t.company_id WHERE {where}", args).fetchone()
    m = {k: (row[k] or 0) for k in row.keys()}
    m["ai_rate"] = round(100 * m["by_ai"] / m["resolved"]) if m["resolved"] else None
    m["hours_saved"] = round(m["by_ai"] * config.MINUTES_SAVED_PER_AI_TICKET / 60, 1)
    m["reply_minutes"] = round(m["reply_minutes"], 1) if row["reply_minutes"] is not None else None
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
        " WHERE c.provider_id = ? AND t.status = 'open' ORDER BY t.created_at LIMIT 8",
        (pid,)).fetchall()
    return render_template("dashboard.html", all_time=metrics(pid),
                           month=metrics(pid, since=month_start()),
                           by_company=by_company, categories=categories,
                           warranties=warranties, queue=queue, bill=billing(g.provider),
                           today=datetime.now(timezone.utc).strftime("%Y-%m-%d"))


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
    rows = g.db.execute(
        "SELECT t.*, c.name AS company_name, tech.name AS tech_name FROM tickets t"
        " JOIN companies c ON c.id = t.company_id"
        " LEFT JOIN technicians tech ON tech.id = t.assigned_to"
        f" WHERE {' AND '.join(where)} ORDER BY t.created_at DESC LIMIT 200", args).fetchall()
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
    return render_template("ticket.html", t=t, messages=msgs, person=person,
                           devices=devices, history=history, techs=techs)


@app.route("/tickets/<int:ticket_id>/reply", methods=["POST"])
@login_required
def ticket_reply(ticket_id):
    t = own_ticket(ticket_id)
    body = request.form.get("body", "").strip()
    if body:
        g.db.execute(
            "INSERT INTO messages (ticket_id, sender, author_name, body, created_at)"
            " VALUES (?, 'tech', ?, ?, ?)", (ticket_id, g.tech["name"], body, db.now()))
        g.db.execute(
            "UPDATE tickets SET first_reply_at = COALESCE(first_reply_at, ?),"
            " assigned_to = COALESCE(assigned_to, ?),"
            " status = CASE WHEN status = 'ai_answered' THEN 'open' ELSE status END WHERE id = ?",
            (db.now(), g.tech["id"], ticket_id))
    if request.form.get("resolve"):
        g.db.execute(
            "UPDATE tickets SET status = 'resolved', resolved_by = 'human', resolved_at = ?"
            " WHERE id = ?", (db.now(), ticket_id))
        # Step 4 of the loop: the human fix becomes a guide the AI can use next time.
        title = request.form.get("guide_title", "").strip()
        if request.form.get("save_guide") and title and body:
            g.db.execute(
                "INSERT INTO articles (provider_id, company_id, title, keywords, body, source, created_at)"
                " VALUES (?, ?, ?, ?, ?, 'ticket', ?)",
                (g.provider["id"], t["company_id"], title, t["subject"], body, db.now()))
            flash("Ticket resolved and the fix was saved as a guide for this company.")
        else:
            flash("Ticket resolved.")
    g.db.commit()
    flush_outbox(ticket_id)
    return redirect(url_for("ticket", ticket_id=ticket_id))


@app.route("/tickets/<int:ticket_id>/update", methods=["POST"])
@login_required
def ticket_update(ticket_id):
    own_ticket(ticket_id)
    action = request.form.get("action")
    if action == "assign":
        tech_id = request.form.get("tech_id", type=int)
        valid = tech_id and g.db.execute(
            "SELECT 1 FROM technicians WHERE id = ? AND provider_id = ?",
            (tech_id, g.provider["id"])).fetchone()
        g.db.execute("UPDATE tickets SET assigned_to = ? WHERE id = ?",
                     (tech_id if valid else None, ticket_id))
    elif action == "reopen":
        g.db.execute("UPDATE tickets SET status = 'open', resolved_by = NULL, resolved_at = NULL"
                     " WHERE id = ?", (ticket_id,))
    elif action == "take":
        g.db.execute("UPDATE tickets SET status = 'open', assigned_to = ?,"
                     " escalation_reason = 'Taken over by a technician' WHERE id = ?",
                     (g.tech["id"], ticket_id))
    g.db.commit()
    return redirect(url_for("ticket", ticket_id=ticket_id))


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
    return render_template("companies.html", companies=rows)


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
    g.db.commit()
    return redirect(url_for("company", company_id=company_id))


@app.route("/companies/<int:company_id>/delete", methods=["POST"])
@owner_required
def company_delete(company_id):
    c = own_company(company_id)
    if request.form.get("confirm", "").strip() != c["name"]:
        flash("Type the company name exactly to confirm deletion. Nothing was deleted.")
        return redirect(url_for("company", company_id=company_id))
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
    return "Ã¯Â»Â¿" + out.getvalue()      # BOM so Excel opens it correctly


def _company_files(c):
    cid = c["id"]
    q = g.db.execute
    files = {}
    files["tickets.csv"] = _csv(
        q("SELECT id, created_at, requester_name, requester_email, subject, category, status,"
          " resolved_by, escalation_reason, first_reply_at, resolved_at FROM tickets"
          " WHERE company_id = ? ORDER BY id", (cid,)).fetchall(),
        ["id", "created_at", "requester_name", "requester_email", "subject", "category",
         "status", "resolved_by", "escalation_reason", "first_reply_at", "resolved_at"])
    files["ticket_messages.csv"] = _csv(
        q("SELECT m.ticket_id, m.created_at, m.sender, m.author_name, m.body FROM messages m"
          " JOIN tickets t ON t.id = m.ticket_id WHERE t.company_id = ? ORDER BY m.id",
          (cid,)).fetchall(),
        ["ticket_id", "created_at", "sender", "author_name", "body"])
    files["people.csv"] = _csv(
        q("SELECT id, name, email, job_title FROM people WHERE company_id = ? ORDER BY name",
          (cid,)).fetchall(), ["id", "name", "email", "job_title"])
    files["devices.csv"] = _csv(
        q("SELECT d.id, d.name, d.kind, d.serial, d.warranty_end, p.name AS assigned_to"
          " FROM devices d LEFT JOIN people p ON p.id = d.person_id WHERE d.company_id = ?"
          " ORDER BY d.name", (cid,)).fetchall(),
        ["id", "name", "kind", "serial", "warranty_end", "assigned_to"])
    for a in q("SELECT * FROM articles WHERE company_id = ? ORDER BY id", (cid,)):
        safe = "".join(ch if ch.isalnum() or ch in " -_" else "" for ch in a["title"])[:60].strip()
        files[f"guides/{a['id']} {safe}.txt"] = f"{a['title']}\n\n{a['body']}\n"
    lines = []
    for r in q("SELECT * FROM checklist_runs WHERE company_id = ? ORDER BY id", (cid,)).fetchall():
        lines.append(f"{r['template_name']}: {r['subject_name']} ({r['created_at']})")
        for i in q("SELECT * FROM checklist_items WHERE run_id = ? ORDER BY id", (r["id"],)):
            lines.append(f"  [{'x' if i['done'] else ' '}] {i['text']}")
        lines.append("")
    files["checklists.txt"] = "\n".join(lines)
    files["README.txt"] = (
        f"Data export for {c['name']}\nCreated {db.now()} UTC by {config.APP_NAME}\n\n"
        "tickets.csv          every request\n"
        "ticket_messages.csv  the full conversation on each request\n"
        "people.csv           staff\ndevices.csv          equipment and who has it\n"
        "guides/              how-to guides written for this company\n"
        "checklists.txt       starter and leaver checklists\n")
    return files


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
    files = {}
    for c in g.db.execute("SELECT * FROM companies WHERE provider_id = ? ORDER BY name",
                          (g.provider["id"],)).fetchall():
        for name, content in _company_files(c).items():
            files[f"{_slug(c['name'])}/{name}"] = content
    shared = g.db.execute(
        "SELECT title, keywords, body, source FROM articles"
        " WHERE provider_id = ? AND company_id IS NULL ORDER BY title", (g.provider["id"],)).fetchall()
    files["shared_guides.json"] = json.dumps([dict(r) for r in shared], indent=2)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _zip_response(files, f"{_slug(g.provider['name'])}-full-export-{stamp}.zip")


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
    if request.method == "POST":
        if g.tech["role"] != "owner":
            abort(403)
        action = request.form.get("action")
        if action == "add":
            name = request.form.get("name", "").strip()
            email = request.form.get("email", "").strip().lower()
            password = request.form.get("password", "")
            limit = bill["plan"]["max_techs"]
            if limit is not None and bill["seats"] >= limit:
                flash(f"The {bill['plan']['name']} plan includes {limit} technician. "
                      "Move to the Team plan to add more.")
            elif not (name and email) or len(password) < 8:
                flash("Enter a name, an email, and a starting password of at least 8 characters.")
            elif g.db.execute("SELECT 1 FROM technicians WHERE email = ?", (email,)).fetchone():
                flash("That email already has an account.")
            else:
                g.db.execute(
                    "INSERT INTO technicians (provider_id, name, email, password_hash, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (g.provider["id"], name, email, generate_password_hash(password), db.now()))
        elif action == "remove":
            g.db.execute(
                "UPDATE technicians SET active = 0 WHERE id = ? AND provider_id = ? AND role != 'owner'",
                (request.form.get("tech_id", type=int), g.provider["id"]))
        elif action == "plan":
            if g.android:
                abort(403)          # plans change on the website only
            plan = request.form.get("plan")
            limit = config.PLANS.get(plan, {}).get("max_techs")
            if plan not in config.PLANS:
                abort(400)
            if limit is not None and bill["seats"] > limit:
                flash(f"Remove technicians first: that plan allows {limit}.")
            else:
                g.db.execute("UPDATE providers SET plan = ? WHERE id = ?", (plan, g.provider["id"]))
        g.db.commit()
        return redirect(url_for("team"))
    techs = g.db.execute(
        "SELECT * FROM technicians WHERE provider_id = ? AND active = 1 ORDER BY role != 'owner', name",
        (g.provider["id"],)).fetchall()
    return render_template("team.html", techs=techs, bill=bill, plans=config.PLANS)


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



# ---------------------------------------------------------------------------
# What staff can do, whichever way they reach us (help page, email, WhatsApp)
# ---------------------------------------------------------------------------

def open_ticket(c, name, email, problem, channel="web", person=None, subject=""):
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
        " subject, category, status, public_token, channel, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
        (c["id"], person_id, device["id"] if device else None, name, email, subject,
         ai.categorize(problem), secrets.token_urlsafe(16), channel, db.now()))
    g.db.execute("INSERT INTO messages (ticket_id, sender, author_name, body, delivery, created_at)"
                 " VALUES (?, 'user', ?, ?, 'n/a', ?)", (cur.lastrowid, name, problem, db.now()))
    t = g.db.execute("SELECT * FROM tickets WHERE id = ?", (cur.lastrowid,)).fetchone()
    run_ai(t, c)
    return t


def staff_reply(t, c, body):
    """The staff member adds detail to their ticket."""
    body = ai.redact(body.strip())
    if not body:
        return
    if t["status"] == "resolved":
        g.db.execute("UPDATE tickets SET status = 'open', resolved_by = NULL, resolved_at = NULL,"
                     " escalation_reason = 'Reopened by staff member' WHERE id = ?", (t["id"],))
    g.db.execute("INSERT INTO messages (ticket_id, sender, author_name, body, delivery, created_at)"
                 " VALUES (?, 'user', ?, ?, 'n/a', ?)", (t["id"], t["requester_name"], body, db.now()))
    if t["status"] == "ai_answered":
        run_ai(t, c)


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
        if t["channel"] != "web":
            note("Glad that is sorted. This request is now closed.")
    elif action == "human" and t["status"] == "ai_answered":
        note(ai.HANDOVER)
        g.db.execute("UPDATE tickets SET status = 'open',"
                     " escalation_reason = 'Staff member asked for a technician' WHERE id = ?",
                     (t["id"],))


FIXED_WORDS = {"1", "fixed", "solved", "yes", "it is fixed", "its fixed", "it's fixed", "done", "resolved"}
HELP_WORDS = {"2", "help", "no", "technician", "human", "still need help", "not fixed"}


def handle_inbound(c, person, text, channel, ticket=None, subject=""):
    """Route one incoming email or WhatsApp message. Returns the ticket id."""
    word = text.strip().lower().strip(".! ")
    if ticket and word in FIXED_WORDS:
        staff_marks(ticket, "solved")       # does nothing if it is already closed
    elif ticket and ticket["status"] == "ai_answered" and word in HELP_WORDS:
        staff_marks(ticket, "human")
    elif ticket:
        staff_reply(ticket, c, text)
    else:
        ticket = open_ticket(c, person["name"], person["email"], text, channel, person, subject)
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
    for m in pending:
        waiting = m["id"] == last_id and t["status"] == "ai_answered"
        if t["channel"] == "whatsapp":
            body = m["body"]
            if waiting:
                body += "\n\nReply 1 if that fixed it, or 2 to get a technician."
            error = (channels.send_whatsapp(person["phone"], body) if person and person["phone"]
                     else "no WhatsApp number on record")
            result = "sent by WhatsApp"
        elif t["requester_email"] and (t["channel"] == "email"
                                       or (m["sender"] == "tech" and channels.email_out())):
            # Help-page tickets get an email only when a technician replies.
            who = f"{m['author_name']}, technician, wrote:\n\n" if m["sender"] == "tech" else ""
            footer = ("Did that fix it? Reply to this email with FIXED, or with HELP to get a technician."
                      if waiting else "Reply to this email to add more detail.")
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
    ticket_id = handle_inbound(c, person, text, "email", ticket, mail["subject"])
    g.db.commit()
    flush_outbox(ticket_id)


def email_poller():
    """Background loop: check the helpdesk mailbox once a minute."""
    while True:
        if channels.email_in():
            with app.app_context():
                g.db = db.connect()
                try:
                    for mail in channels.fetch_emails():
                        try:
                            process_email(mail)
                        except Exception as exc:             # one bad email must not stop the rest
                            g.db.rollback()
                            app.logger.error("Could not process email from %s: %s", mail["address"], exc)
                except Exception as exc:
                    app.logger.error("Could not check the mailbox: %s", exc)
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
        if not m["text"]:
            channels.send_whatsapp(m["phone"], "Please describe the problem in a text message.")
            continue
        c = g.db.execute("SELECT * FROM companies WHERE id = ?", (person["company_id"],)).fetchone()
        ticket = g.db.execute(
            "SELECT * FROM tickets WHERE person_id = ? AND channel = 'whatsapp'"
            " AND status != 'resolved' ORDER BY id DESC LIMIT 1", (person["id"],)).fetchone()
        ticket_id = handle_inbound(c, person, m["text"], "whatsapp", ticket)
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
        t = open_ticket(c, name, email, problem)
        g.db.commit()
        flush_outbox(t["id"])
        return redirect(url_for("portal_ticket", public_token=t["public_token"]))
    return render_template("portal.html", c=c, public=True)


@app.route("/help/t/<public_token>", methods=["GET", "POST"])
def portal_ticket(public_token):
    t = g.db.execute("SELECT * FROM tickets WHERE public_token = ?", (public_token,)).fetchone()
    if not t:
        abort(404)
    c = g.db.execute("SELECT * FROM companies WHERE id = ?", (t["company_id"],)).fetchone()
    if request.method == "POST":
        action = request.form.get("action")
        if action in ("solved", "human"):
            staff_marks(t, action)
        elif action == "reply":
            staff_reply(t, c, request.form.get("body", ""))
        g.db.commit()
        flush_outbox(t["id"])
        return redirect(url_for("portal_ticket", public_token=public_token))
    msgs = g.db.execute("SELECT * FROM messages WHERE ticket_id = ? ORDER BY id", (t["id"],)).fetchall()
    return render_template("portal_ticket.html", t=t, c=c, messages=msgs, public=True)


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
def error_page(err):
    return render_template("error.html", code=err.code, message=err.description), err.code


if __name__ == "__main__":
    threading.Thread(target=email_poller, daemon=True).start()
    app.run(host="127.0.0.1", port=5000, debug=False)
