"""Make a one-time password reset link for any account, for when email isn't working.

On your computer:  .venv\\Scripts\\python.exe tools\\reset_link.py someone@example.com
On the server:     see deploy/DEPLOY.md ("Useful commands").

Give the link only to the account's owner, after checking it is really them.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import app, make_link  # noqa: E402
import db  # noqa: E402
from flask import g  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: reset_link.py EMAIL")
    with app.test_request_context():
        g.db = db.connect()
        tech = g.db.execute("SELECT * FROM technicians WHERE email = ? AND active = 1",
                            (sys.argv[1].strip().lower(),)).fetchone()
        if not tech:
            raise SystemExit("No active account with that email.")
        link = make_link(tech["id"], "reset" if tech["password_hash"] else "invite")
        g.db.commit()
        print(f"One-time link for {tech['name']} (works once, expires soon):\n{link}")
