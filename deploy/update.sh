#!/usr/bin/env bash
# Puts a new version of SimpleDesk live. Run as root on the server, from the
# folder holding the newly uploaded files (deploy/upload.ps1 does all this).
# Takes a backup first, and goes back to the previous version if the new one
# does not start.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then echo "Please run as root."; exit 1; fi
SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/simpledesk/app
VENV=/opt/simpledesk/venv
PREVIOUS=/opt/simpledesk/previous

echo "==> Backup before updating"
systemctl start simpledesk-backup.service   # waits until the backup has finished

echo "==> Keeping the current version in case we need to go back"
rm -rf "$PREVIOUS"; cp -a "$APP" "$PREVIOUS"

echo "==> Installing the new version"
rsync -a --delete --exclude ".venv" --exclude "data" --exclude "__pycache__" --exclude ".claude" "$SRC/" "$APP/"
"$VENV/bin/pip" install -q -r "$APP/requirements.txt"
chown -R simpledesk:simpledesk "$APP"
cp "$APP"/deploy/systemd/*.service "$APP"/deploy/systemd/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl restart simpledesk simpledesk-mail

for i in $(seq 1 20); do
  if curl -fsS http://127.0.0.1:8000/healthz >/dev/null; then echo "Update is live."; exit 0; fi; sleep 1
done

echo "!! The new version did not start. Going back to the previous one."
journalctl -u simpledesk -n 30 --no-pager || true
rm -rf "$APP"; cp -a "$PREVIOUS" "$APP"
systemctl restart simpledesk simpledesk-mail
exit 1
