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
]


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
    conn.commit()
    conn.close()
