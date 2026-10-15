#!/usr/bin/env bash
# First-time setup of SimpleDesk on a fresh DigitalOcean droplet (Ubuntu 24.04).
#
#   bash setup.sh                      # uses a free address: <your-ip>.sslip.io
#   bash setup.sh help.yourdomain.com  # once you own a domain
#
# Run as root, from the folder holding the uploaded SimpleDesk files.
# Safe to run again: it keeps your data and settings.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then echo "Please run as root."; exit 1; fi
SRC="$(cd "$(dirname "$0")/.." && pwd)"
APP=/opt/simpledesk/app
VENV=/opt/simpledesk/venv
DATA=/var/lib/simpledesk
BACKUPS=/var/backups/simpledesk
ENVFILE=/etc/simpledesk.env

# The droplet's public address, from DigitalOcean's own metadata service.
IP="$(curl -fsS --max-time 3 http://169.254.169.254/metadata/v1/interfaces/public/0/ipv4/address || hostname -I | awk '{print $1}')"
DOMAIN="${1:-${IP//./-}.sslip.io}"
echo "==> Setting up SimpleDesk at https://$DOMAIN"

echo "==> Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip sqlite3 rsync ufw curl debian-keyring debian-archive-keyring apt-transport-https unattended-upgrades
if ! command -v caddy >/dev/null; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q && apt-get install -y -q caddy
fi
# Security updates for the server install themselves.
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "==> Firewall: allow SSH and web only"
ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

echo "==> App user and folders"
id simpledesk >/dev/null 2>&1 || useradd --system --home /opt/simpledesk --shell /usr/sbin/nologin simpledesk
mkdir -p "$APP" "$DATA" "$BACKUPS"

echo "==> Copying the app"
rsync -a --delete --exclude ".venv" --exclude "data" --exclude "__pycache__" --exclude ".claude" "$SRC/" "$APP/"

echo "==> Python packages"
[ -d "$VENV" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$APP/requirements.txt"

if [ ! -f "$ENVFILE" ]; then
  echo "==> Writing settings to $ENVFILE"
  cat > "$ENVFILE" <<EOF
# SimpleDesk server settings. After editing: systemctl restart simpledesk simpledesk-mail
SIMPLEDESK_DOMAIN=$DOMAIN
SIMPLEDESK_DATA_DIR=$DATA
SIMPLEDESK_BACKUP_DIR=$BACKUPS
SIMPLEDESK_HTTPS=1
SIMPLEDESK_BUSINESS_NAME=SimpleDesk
SIMPLEDESK_CONTACT_EMAIL=privacy@example.com
# From PWABuilder, once you build the Android app:
SIMPLEDESK_ANDROID_PACKAGE=
SIMPLEDESK_ANDROID_SHA256=
# Optional, for AI-written answers:
# ANTHROPIC_API_KEY=
# Optional, error alerts: Sentry > Project settings > Client Keys (DSN):
# SENTRY_DSN=
EOF
else
  sed -i "s|^SIMPLEDESK_DOMAIN=.*|SIMPLEDESK_DOMAIN=$DOMAIN|" "$ENVFILE"
fi
chown root:simpledesk "$ENVFILE"; chmod 640 "$ENVFILE"

# Email/WhatsApp settings: start from the example, with the public address filled in.
if [ ! -f "$DATA/channels.ini" ]; then
  sed "s|^public_url =.*|public_url = https://$DOMAIN|" "$APP/channels.example.ini" > "$DATA/channels.ini"
fi
chown -R simpledesk:simpledesk "$DATA" "$BACKUPS" /opt/simpledesk
chmod 750 "$DATA" "$BACKUPS"

echo "==> Services"
cp "$APP"/deploy/systemd/*.service "$APP"/deploy/systemd/*.timer /etc/systemd/system/
sed "s|{DOMAIN}|$DOMAIN|" "$APP/deploy/Caddyfile" > /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable --now simpledesk simpledesk-mail simpledesk-backup.timer
systemctl reload caddy || systemctl restart caddy

echo "==> Checking it works"
for i in $(seq 1 20); do
  if curl -fsS http://127.0.0.1:8000/healthz >/dev/null; then break; fi; sleep 1
done
if curl -fsS http://127.0.0.1:8000/healthz >/dev/null; then
  echo "App is running."
else
  echo "!! The app did not start. Recent messages:"; journalctl -u simpledesk -n 30 --no-pager; exit 1
fi
echo
echo "Done. Open https://$DOMAIN in a few minutes (the security certificate is fetched on first visit)."
echo "Next: edit $ENVFILE to set your contact email, then: systemctl restart simpledesk"
