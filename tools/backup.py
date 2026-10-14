"""Daily backup of SimpleDesk.

Makes one zip file holding a safe copy of the database (taken while the app
keeps running), the email/WhatsApp settings and the sign-in key. Checks the
copy is not damaged, then deletes backups older than BACKUP_KEEP_DAYS.

Run by hand:      .venv\\Scripts\\python.exe tools\\backup.py
On the server:    runs every night by itself (see deploy/DEPLOY.md).

Backups land in data/backups (or SIMPLEDESK_BACKUP_DIR). They hold client
details and passwords for mail/WhatsApp, so keep copies somewhere private.
"""
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402


def make_backup():
    if not config.DB_PATH.exists():
        raise SystemExit(f"No database found at {config.DB_PATH}")
    config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M")
    target = config.BACKUP_DIR / f"simpledesk-backup-{stamp}.zip"

    with tempfile.TemporaryDirectory() as tmp:
        copy_path = Path(tmp) / "helpdesk.db"
        # SQLite's own backup copies a consistent snapshot even while people use the app.
        source = sqlite3.connect(config.DB_PATH, timeout=30)
        copy = sqlite3.connect(copy_path)
        with copy:
            source.backup(copy)
        result = copy.execute("PRAGMA integrity_check").fetchone()[0]
        copy.close()
        source.close()
        if result != "ok":
            raise SystemExit(f"Backup copy failed its check ({result}); nothing was saved.")

        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(copy_path, "helpdesk.db")
            for extra in ("channels.ini", "secret.key"):
                path = config.DATA_DIR / extra
                if path.exists():
                    z.write(path, extra)
            z.writestr("RESTORE.txt",
                       "To restore: stop SimpleDesk, put these files back in the data folder\n"
                       "(helpdesk.db, channels.ini, secret.key), delete any helpdesk.db-wal and\n"
                       "helpdesk.db-shm files there, then start SimpleDesk again.\n")
    return target


def remove_old():
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.BACKUP_KEEP_DAYS)
    removed = 0
    for path in config.BACKUP_DIR.glob("simpledesk-backup-*.zip"):
        if datetime.fromtimestamp(path.stat().st_mtime, timezone.utc) < cutoff:
            path.unlink()
            removed += 1
    return removed


if __name__ == "__main__":
    saved = make_backup()
    size = saved.stat().st_size / 1024
    removed = remove_old()
    print(f"Backup saved: {saved} ({size:.0f} KB). Removed {removed} backup(s) older than "
          f"{config.BACKUP_KEEP_DAYS} days.")
