#!/usr/bin/env bash
# Set up the EPFO search app on a VPS that may already run other apps.
# Run as root:
#
#   bash setup.sh --check <git-repo-url> <domain-or-ip>   # only look, change nothing
#   bash setup.sh         <git-repo-url> <domain-or-ip>   # install
#
# Options (environment variables):
#   PUBLIC_PORT  port people open in the browser when no domain is given (default 8090)
#   APP_PORT     internal port the app listens on behind nginx (default 8765)
#
# What it touches, and nothing else:
#   - packages: installs python3-venv / git only if missing, never upgrades
#     anything (other apps keep their versions); never installs nginx
#   - user "epfo", folder /opt/epfo-search, service "epfo-search"
#   - nginx (only if already installed): one new file
#     /etc/nginx/sites-available/epfo-search, never the default site.
#     With an IP it listens on its own PUBLIC_PORT; with a domain it answers
#     on port 80 only for that exact domain name. Existing sites keep working.
#   - without nginx: the app itself listens on PUBLIC_PORT
#   - firewall (ufw, only if active): opens PUBLIC_PORT only
# It stops before changing anything if a port, user, folder, service or
# nginx site with these names is already taken by something else.
set -euo pipefail

CHECK_ONLY=0
if [ "${1:-}" = "--check" ]; then CHECK_ONLY=1; shift; fi
USAGE="usage: bash setup.sh [--check] <git-repo-url> <domain-or-ip>"
REPO_URL="${1:?$USAGE}"
SERVER_NAME="${2:?$USAGE}"
PUBLIC_PORT="${PUBLIC_PORT:-8090}"
APP_PORT="${APP_PORT:-8765}"
APP_DIR=/opt/epfo-search
APP_USER=epfo
SERVICE=epfo-search
SITE=/etc/nginx/sites-available/epfo-search
UNIT=/etc/systemd/system/$SERVICE.service

problems=0
say()  { echo "  $*"; }
fail() { echo "  STOP: $*"; problems=$((problems + 1)); }

if [ "$(id -u)" -ne 0 ]; then
    echo "Run as root (or with sudo)." >&2
    exit 1
fi

port_owner() { ss -ltnpH "sport = :$1" 2>/dev/null | head -1; }

# Our own install from an earlier run is fine to update.
ours=0
[ -f "$UNIT" ] && grep -q "/opt/epfo-search" "$UNIT" && ours=1

is_ip=0
[[ "$SERVER_NAME" =~ ^[0-9.]+$ || "$SERVER_NAME" == *:* ]] && is_ip=1

has_nginx=0
command -v nginx >/dev/null && [ -d /etc/nginx/sites-available ] && has_nginx=1

echo "== Checking the server (nothing changed yet)"
if [ "$has_nginx" = 1 ]; then
    say "nginx: installed, will add one site file for this app"
    if [ "$is_ip" = 1 ]; then
        say "mode: nginx on port $PUBLIC_PORT -> app on 127.0.0.1:$APP_PORT"
    else
        say "mode: nginx on port 80 for $SERVER_NAME only -> app on 127.0.0.1:$APP_PORT"
    fi
else
    say "nginx: not installed (will NOT be installed)"
    say "mode: app listens directly on port $PUBLIC_PORT"
fi

# Ports
check_port() {
    local p=$1 why=$2 owner
    owner=$(port_owner "$p")
    if [ -n "$owner" ]; then
        if [ "$ours" = 1 ] && systemctl is-active -q "$SERVICE" && echo "$owner" | grep -q python; then
            say "port $p: used by this app already (ok)"
        elif [ "$why" = nginx ] && echo "$owner" | grep -q nginx && [ -f "$SITE" ]; then
            say "port $p: nginx, from this app's site (ok)"
        else
            fail "port $p is already used: $owner  -> pick another, e.g. PUBLIC_PORT=8091 / APP_PORT=8766"
        fi
    else
        say "port $p: free"
    fi
}
if [ "$has_nginx" = 1 ]; then
    check_port "$APP_PORT" app
    if [ "$is_ip" = 1 ]; then check_port "$PUBLIC_PORT" nginx; fi
    owner80=$(port_owner 80)
    if [ "$is_ip" = 0 ] && [ -n "$owner80" ] && ! echo "$owner80" | grep -q nginx; then
        fail "port 80 belongs to something other than nginx: $owner80  -> use the IP instead of a domain"
    fi
    if [ "$is_ip" = 0 ] && grep -rqsw "server_name.*$SERVER_NAME" /etc/nginx/sites-enabled /etc/nginx/conf.d \
            && ! grep -qsw "server_name.*$SERVER_NAME" "$SITE"; then
        fail "another nginx site already uses the name $SERVER_NAME"
    fi
else
    check_port "$PUBLIC_PORT" app
fi

# Names this app uses
if [ -f "$UNIT" ] && [ "$ours" = 0 ]; then fail "a different service called $SERVICE already exists"; fi
if [ -e "$APP_DIR" ] && [ ! -d "$APP_DIR/.git" ]; then fail "$APP_DIR exists and is not this app"; fi
if [ -d "$APP_DIR/.git" ] && ! git -C "$APP_DIR" remote get-url origin 2>/dev/null | grep -q "epfo-search"; then
    fail "$APP_DIR holds a different git repo"
fi
if [ -f "$SITE" ] && ! grep -q "epfo-search" "$SITE"; then fail "$SITE exists and is not this app"; fi
if id "$APP_USER" >/dev/null 2>&1; then say "user $APP_USER: exists (reused)"; else say "user $APP_USER: will be created"; fi

# Packages: install only what is missing, never upgrade
missing=()
for pkg in python3 python3-venv git; do
    dpkg -s "$pkg" >/dev/null 2>&1 || missing+=("$pkg")
done
# Lines like "Inst libpython3.12 [3.12.3-1] (3.12.3-1ubuntu0.1 ...)" mean an
# installed package would be upgraded; other apps may depend on it.
upgrades_needed() {
    apt-get install -s --no-upgrade "${missing[@]}" 2>/dev/null | grep -E '^Inst \S+ \[' || true
}
if [ ${#missing[@]} -gt 0 ]; then
    say "packages to install: ${missing[*]}"
    up=$(upgrades_needed)
    if [ -n "$up" ]; then
        fail "installing ${missing[*]} would upgrade packages other apps may use:"
        echo "$up" | sed 's/^/        /'
    fi
else
    say "packages: all present"
fi

if [ "$problems" -gt 0 ]; then
    echo
    echo "Nothing was changed. Fix the STOP items above and run again."
    exit 1
fi
if [ "$CHECK_ONLY" = 1 ]; then
    echo
    echo "Check passed. Run again without --check to install."
    exit 0
fi

echo "== Installing"
if [ ${#missing[@]} -gt 0 ]; then
    apt-get update -y
    up=$(upgrades_needed)
    if [ -n "$up" ]; then
        echo "STOP: with fresh package lists, installing ${missing[*]} would upgrade:" >&2
        echo "$up" >&2
        echo "Nothing was changed." >&2
        exit 1
    fi
    apt-get install -y --no-upgrade "${missing[@]}"
fi

id "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /bin/bash "$APP_USER"

if [ -d "$APP_DIR/.git" ]; then
    sudo -u "$APP_USER" git -C "$APP_DIR" pull --ff-only
else
    mkdir -p "$APP_DIR"
    chown "$APP_USER:$APP_USER" "$APP_DIR"
    sudo -u "$APP_USER" git clone "$REPO_URL" "$APP_DIR"
fi
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

if [ "$has_nginx" = 1 ]; then
    listen_host=127.0.0.1; listen_port=$APP_PORT
else
    listen_host=0.0.0.0; listen_port=$PUBLIC_PORT
fi
cat > "$UNIT" <<EOF
[Unit]
Description=EPFO establishment search web app (/opt/epfo-search)
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
Environment=EPFO_HOST=$listen_host
Environment=EPFO_PORT=$listen_port
ExecStart=$APP_DIR/.venv/bin/python epfo_web.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"

# Lets GitHub Actions (logging in as $APP_USER) restart this app, nothing else.
echo "$APP_USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart $SERVICE" > /etc/sudoers.d/epfo-search
chmod 440 /etc/sudoers.d/epfo-search
visudo -cf /etc/sudoers.d/epfo-search >/dev/null

if [ "$has_nginx" = 1 ]; then
    if [ "$is_ip" = 1 ]; then nginx_listen=$PUBLIC_PORT; name=_; else nginx_listen=80; name=$SERVER_NAME; fi
    sed -e "s/LISTEN_PORT/$nginx_listen/" -e "s/SERVER_NAME/$name/" -e "s/APP_PORT/$APP_PORT/" \
        "$APP_DIR/deploy/nginx-epfo-search.conf" > "$SITE"
    ln -sf "$SITE" /etc/nginx/sites-enabled/epfo-search
    if ! nginx -t; then
        # Never leave nginx broken for the other sites
        rm -f /etc/nginx/sites-enabled/epfo-search "$SITE"
        echo "nginx rejected the config; removed it again. Other sites unaffected." >&2
        exit 1
    fi
    systemctl reload nginx
fi

if [ "$has_nginx" = 0 ] || [ "$is_ip" = 1 ]; then
    open_port=$PUBLIC_PORT
    if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
        ufw allow "$open_port/tcp" comment "epfo-search"
    fi
    url="http://$SERVER_NAME:$open_port"
else
    url="http://$SERVER_NAME"
fi

sleep 2
if systemctl is-active -q "$SERVICE"; then
    echo
    echo "Done. Open $url"
    [ "$url" != "http://$SERVER_NAME" ] && echo "If it does not load, allow TCP port $open_port in Hostinger hPanel > VPS > Firewall."
else
    echo "The app did not start. See: journalctl -u $SERVICE -n 50" >&2
    exit 1
fi
