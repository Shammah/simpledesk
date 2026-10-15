"""Pictures and documents attached to tickets.

Only pictures (PNG, JPEG, GIF, WebP) and PDFs are kept. The type is worked out
from the file's contents, never from its name, and files are stored under
random names in data/uploads, outside the website's public folders.
"""
import secrets

import config
import db

KINDS = [  # (how the file starts, extension, content type)
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
    (b"%PDF-", "pdf", "application/pdf"),
]
ACCEPT = "image/png,image/jpeg,image/gif,image/webp,application/pdf"


def detect(content):
    for start, ext, mime in KINDS:
        if content.startswith(start):
            return ext, mime
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


def clean_name(name, ext):
    keep = "".join(ch for ch in (name or "") if ch.isalnum() or ch in " .-_()").strip()[:80]
    stem = keep.rsplit(".", 1)[0] if "." in keep else keep
    return f"{stem or 'file'}.{ext}"


def save(conn, ticket_id, message_id, filename, content):
    """Store one file against a ticket. Returns '' or the reason it was refused."""
    if not content:
        return ""
    if len(content) > config.MAX_FILE_MB * 1024 * 1024:
        return f"{filename}: larger than {config.MAX_FILE_MB} MB"
    kind = detect(content)
    if not kind:
        return f"{filename}: only pictures and PDFs can be attached"
    ext, mime = kind
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    stored = f"{secrets.token_hex(16)}.{ext}"
    (config.UPLOAD_DIR / stored).write_bytes(content)
    conn.execute(
        "INSERT INTO attachments (ticket_id, message_id, filename, stored_name, content_type, size, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ticket_id, message_id, clean_name(filename, ext), stored, mime, len(content), db.now()))
    return ""


def save_uploads(conn, ticket_id, message_id, uploads):
    """Store files from a web form (werkzeug FileStorage list). Returns refusal reasons."""
    problems = []
    for upload in [u for u in uploads if u and u.filename][:config.MAX_FILES_PER_MESSAGE]:
        problem = save(conn, ticket_id, message_id, upload.filename,
                       upload.read(config.MAX_FILE_MB * 1024 * 1024 + 1))
        if problem:
            problems.append(problem)
    return problems


def path(row):
    return config.UPLOAD_DIR / row["stored_name"]


def by_message(conn, ticket_id):
    """{message_id: [attachment rows]} for showing files under each message."""
    found = {}
    for row in conn.execute("SELECT * FROM attachments WHERE ticket_id = ? ORDER BY id", (ticket_id,)):
        found.setdefault(row["message_id"], []).append(row)
    return found


def remove_for_company(conn, company_id):
    """Delete the stored files of a company that is being deleted."""
    for row in conn.execute("SELECT a.stored_name FROM attachments a JOIN tickets t ON t.id = a.ticket_id"
                            " WHERE t.company_id = ?", (company_id,)).fetchall():
        try:
            (config.UPLOAD_DIR / row["stored_name"]).unlink()
        except OSError:
            pass
