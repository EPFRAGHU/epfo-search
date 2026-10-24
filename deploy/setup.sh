#!/usr/bin/env bash
# First-time setup on an Ubuntu/Debian VPS (e.g. Hostinger). Run as root:
#
#   bash setup.sh <git-repo-url> <domain-or-ip>
#
# Example:
#   bash setup.sh https://github.com/you/epfo-search.git epfo.example.com
#
# Installs Python, nginx and git, clones the repo to /opt/epfo-search,
# runs the app as user "epfo" under systemd, and puts nginx in front.
# Safe to run again: it updates instead of failing.
set -euo pipefail

REPO_URL="${1:?usage: setup.sh <git-repo-url> <domain-or-ip>}"
SERVER_NAME="${2:?usage: setup.sh <git-repo-url> <domain-or-ip>}"
APP_DIR=/opt/epfo-search
APP_USER=epfo

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root (or with sudo)." >&2
    exit 1
fi

echo "== Packages"
apt-get update -y
apt-get install -y python3 python3-venv git nginx

echo "== App user"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /bin/bash "$APP_USER"

echo "== Code"
if [ -d "$APP_DIR/.git" ]; then
    sudo -u "$APP_USER" git -C "$APP_DIR" pull --ff-only
else
    mkdir -p "$APP_DIR"
    chown "$APP_USER:$APP_USER" "$APP_DIR"
    sudo -u "$APP_USER" git clone "$REPO_URL" "$APP_DIR"
fi

echo "== Python packages"
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

echo "== systemd service"
cp "$APP_DIR/deploy/epfo-search.service" /etc/systemd/system/epfo-search.service
systemctl daemon-reload
systemctl enable --now epfo-search
systemctl restart epfo-search

# Lets GitHub Actions (logging in as $APP_USER) restart the app, nothing else.
echo "$APP_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart epfo-search" > /etc/sudoers.d/epfo-search
chmod 440 /etc/sudoers.d/epfo-search
visudo -cf /etc/sudoers.d/epfo-search

echo "== nginx"
sed "s/SERVER_NAME/$SERVER_NAME/" "$APP_DIR/deploy/nginx-epfo-search.conf" > /etc/nginx/sites-available/epfo-search
ln -sf /etc/nginx/sites-available/epfo-search /etc/nginx/sites-enabled/epfo-search
nginx -t
systemctl reload nginx

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    ufw allow 'Nginx Full'
fi

echo
echo "Done. Open http://$SERVER_NAME"
echo "For HTTPS (needs a domain pointing at this VPS):"
echo "  apt-get install -y certbot python3-certbot-nginx && certbot --nginx -d $SERVER_NAME"
