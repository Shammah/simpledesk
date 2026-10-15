"""SQLite storage. Every row belongs to one provider (your IT business) and,
below that, to one client company. All queries in app.py filter on those ids."""
import sqlite3
from datetime import datetime, timezone

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS providers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    plan TEXT NOT NULL DEFAULT 'free',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS technicians (
    id INTEGER PRIMARY KEY,
    provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'tech',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    portal_token TEXT NOT NULL UNIQUE,
    ai_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    email TEXT NOT NULL DEFAULT '',
    job_title TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'Laptop',
    serial TEXT NOT NULL DEFAULT '',
    warranty_end TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tickets (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    person_id INTEGER REFERENCES people(id) ON DELETE SET NULL,
    device_id INTEGER REFERENCES devices(id) ON DELETE SET NULL,
    assigned_to INTEGER REFERENCES technicians(id) ON DELETE SET NULL,
    requester_name TEXT NOT NULL,
    requester_email TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT 'Other',
    -- ai_answered: AI replied, waiting to hear if it worked
    -- open: needs a technician
    -- resolved: done
    status TEXT NOT NULL,
    resolved_by TEXT,              -- 'ai' or 'human'
    escalation_reason TEXT NOT NULL DEFAULT '',
    public_token TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    first_reply_at TEXT,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    sender TEXT NOT NULL,          -- user, ai, tech, system
    author_name TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY,
    provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    -- NULL = shared guide for all of this provider's clients.
    -- Set = private to that one company and never shown to another.
    company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    keywords TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',   -- starter, manual, ticket
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checklist_templates (
    id INTEGER PRIMARY KEY,
    provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    items TEXT NOT NULL            -- one step per line
);
CREATE TABLE IF NOT EXISTS checklist_runs (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    template_name TEXT NOT NULL,
    subject_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checklist_items (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES checklist_runs(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ai_usage (
    id INTEGER PRIMARY KEY,
    provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    ticket_id INTEGER,
    engine TEXT NOT NULL,          -- 'model' or 'guides'
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tickets_company ON tickets(company_id, status);
CREATE INDEX IF NOT EXISTS idx_messages_ticket ON messages(ticket_id);
CREATE INDEX IF NOT EXISTS idx_usage_provider ON ai_usage(provider_id, created_at);
"""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def connect():
    config.DATA_DIR.mkdir(exist_ok=True)
    # timeout: wait up to 15s if another worker is writing at the same moment.
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns and tables added after the first version. Safe to run on every start.
MIGRATIONS = [
    ("companies", "email_domain", "TEXT NOT NULL DEFAULT ''"),
    ("people", "phone", "TEXT NOT NULL DEFAULT ''"),
    ("tickets", "channel", "TEXT NOT NULL DEFAULT 'web'"),     # web, email, whatsapp
    # '' = waiting to be sent, 'n/a' = shown on the help page only,
    # 'sent by email', 'sent by WhatsApp', or 'failed: reason'
    ("messages", "delivery", "TEXT NOT NULL DEFAULT ''"),
    ("technicians", "tour_done", "INTEGER NOT NULL DEFAULT 0"),   # has seen the welcome tour
    # Which tickets to email them about: all, mine (assigned to them) or none
    ("technicians", "notify", "TEXT NOT NULL DEFAULT 'all'"),
    # Billing: paid up to this time (UTC). Saved card details are Paystack's
    # reusable code, never the card number itself.
    ("providers", "paid_until", "TEXT"),
    ("providers", "billing_email", "TEXT NOT NULL DEFAULT ''"),
    ("providers", "card_code", "TEXT NOT NULL DEFAULT ''"),
    ("providers", "card_label", "TEXT NOT NULL DEFAULT ''"),
    ("providers", "billing_notice", "TEXT NOT NULL DEFAULT ''"),   # last reminder sent, to avoid repeats
    # Response targets
    ("tickets", "priority", "TEXT NOT NULL DEFAULT 'normal'"),
    ("tickets", "waiting_since", "TEXT"),     # when it last came to a technician
    ("tickets", "due_at", "TEXT"),            # first technician reply due by
    ("tickets", "target_met", "INTEGER"),     # NULL until a technician replies, then 1 or 0
    ("tickets", "late_alerted", "INTEGER NOT NULL DEFAULT 0"),
    # Satisfaction: 3 good, 2 okay, 1 not good
    ("tickets", "rating", "INTEGER"),
    ("tickets", "rating_note", "TEXT NOT NULL DEFAULT ''"),
    # Monthly client report
    ("companies", "report_email", "TEXT NOT NULL DEFAULT ''"),
    ("companies", "report_auto", "INTEGER NOT NULL DEFAULT 0"),
    ("companies", "last_report", "TEXT NOT NULL DEFAULT ''"),      # 'YYYY-MM' last sent automatically
    ("companies", "monthly_hours", "REAL NOT NULL DEFAULT 0"),     # hours included in their contract; 0 = not set
    # Pictures: stored file names in data/uploads ('' = show initials instead)
    ("technicians", "photo", "TEXT NOT NULL DEFAULT ''"),
    ("companies", "logo", "TEXT NOT NULL DEFAULT ''"),
    # Live conversation: when a technician last looked, and who is typing right now
    ("tickets", "tech_seen_at", "TEXT"),
    ("tickets", "tech_typing_until", "TEXT"),
    ("tickets", "tech_typing_name", "TEXT NOT NULL DEFAULT ''"),
    ("tickets", "user_typing_until", "TEXT"),
    # Changing this signs a technician out of every other device.
    ("technicians", "session_key", "TEXT NOT NULL DEFAULT ''"),
    # Shown on clients' help pages: "Urgent? Call ..."
    ("providers", "support_phone", "TEXT NOT NULL DEFAULT ''"),
    ("providers", "support_hours", "TEXT NOT NULL DEFAULT ''"),
]

# Tables added after the first version.
LATER_TABLES = """
CREATE TABLE IF NOT EXISTS sign_in_links (
    id INTEGER PRIMARY KEY,
    tech_id INTEGER NOT NULL REFERENCES technicians(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,            -- reset or invite
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY,
    provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    reference TEXT NOT NULL UNIQUE,
    amount INTEGER NOT NULL,       -- whole shillings
    seats INTEGER NOT NULL,
    months INTEGER NOT NULL,
    status TEXT NOT NULL,          -- pending, paid, failed
    method TEXT NOT NULL DEFAULT '',
    automatic INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    paid_at TEXT
);
CREATE TABLE IF NOT EXISTS time_entries (
    id INTEGER PRIMARY KEY,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    tech_id INTEGER REFERENCES technicians(id) ON DELETE SET NULL,
    tech_name TEXT NOT NULL,
    minutes INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS attachments (
    id INTEGER PRIMARY KEY,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    message_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    stored_name TEXT NOT NULL UNIQUE,
    content_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS saved_replies (
    id INTEGER PRIMARY KEY,
    provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body TEXT NOT NULL
);
-- What happened, newest last: feeds the dashboard and the live updates.
CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY,
    provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    company_id INTEGER REFERENCES companies(id) ON DELETE CASCADE,
    ticket_id INTEGER REFERENCES tickets(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,            -- new, needs_tech, ai, reply, tech_reply, resolved, rated, payment
    actor TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_activity_provider ON activity(provider_id, id);
CREATE INDEX IF NOT EXISTS idx_time_ticket ON time_entries(ticket_id);
CREATE INDEX IF NOT EXISTS idx_files_ticket ON attachments(ticket_id);
CREATE INDEX IF NOT EXISTS idx_payments_provider ON payments(provider_id);
"""


def backfill_activity(conn):
    """Fill the activity feed from tickets that existed before it did."""
    base = ("INSERT INTO activity (provider_id, company_id, ticket_id, kind, actor, text, created_at)"
            " SELECT c.provider_id, t.company_id, t.id, {kind}, {actor}, {text}, {when}"
            " FROM tickets t JOIN companies c ON c.id = t.company_id WHERE {where}")
    conn.execute(base.format(kind="'new'", actor="t.requester_name",
                             text="t.requester_name || ' at ' || c.name || ' asked: ' || t.subject",
                             when="t.created_at", where="1"))
    conn.execute(base.format(kind="'resolved'", actor="''",
                             text="CASE WHEN t.resolved_by = 'ai' THEN 'Solved instantly, no technician needed: '"
                                  " ELSE 'Resolved: ' END || t.subject",
                             when="t.resolved_at", where="t.resolved_at IS NOT NULL"))
    conn.execute(base.format(kind="'rated'", actor="t.requester_name",
                             text="t.requester_name || ' rated the help ' || CASE t.rating WHEN 3 THEN '😀'"
                                  " WHEN 2 THEN '😐' ELSE '🙁' END",
                             when="COALESCE(t.resolved_at, t.created_at)", where="t.rating IS NOT NULL"))
    # Keep the feed in time order.
    conn.execute("CREATE TEMP TABLE a2 AS SELECT * FROM activity ORDER BY created_at, id")
    conn.execute("DELETE FROM activity")
    conn.execute("INSERT INTO activity (provider_id, company_id, ticket_id, kind, actor, text, created_at)"
                 " SELECT provider_id, company_id, ticket_id, kind, actor, text, created_at FROM a2")
    conn.execute("DROP TABLE a2")


def init():
    conn = connect()
    # Lets pages be read while something else is being saved, and lets the
    # backup script copy the database safely while the app is running.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    # Remembers incoming emails and WhatsApp messages already handled, so a
    # message delivered twice does not open two tickets.
    conn.execute("CREATE TABLE IF NOT EXISTS inbound_seen (id TEXT PRIMARY KEY, created_at TEXT NOT NULL)")
    for table, column, definition in MIGRATIONS:
        existing = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            if column == "delivery":
                conn.execute("UPDATE messages SET delivery = 'n/a'")
    conn.executescript(LATER_TABLES)
    if not conn.execute("SELECT 1 FROM activity LIMIT 1").fetchone():
        backfill_activity(conn)
    conn.commit()
    conn.close()
