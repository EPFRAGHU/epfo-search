# EPFO Establishment Search

Search EPFO establishments by code or name, view their details and payment
history, and see them on a map. You type the EPFO captcha yourself; the app
talks to the EPFO public portal for you.

- `epfo_web.py` – the web app (search page, details, map)
- `epfo_lookup.py` – EPFO portal client; also a terminal version (`python epfo_lookup.py`)
- `epfo_geocode.py` – turns addresses into map points with OpenStreetMap Nominatim (free, cached)
- `deploy/` – files for running on a VPS
- `.github/workflows/deploy.yml` – auto-deploy to the VPS on every push to `main`

## Run on your PC

```
pip install -r requirements.txt
python epfo_web.py
```

Open http://localhost:8765. Stop with `Ctrl+C`. After changing the code,
stop and start it again.

## Put it on a Hostinger VPS

Tested target: Ubuntu 22.04 / 24.04 (Hostinger's default VPS images).

### 1. Push the code to GitHub

Create an empty repository on GitHub (for example `epfo-search`), then in this folder:

```
git remote add origin https://github.com/<you>/epfo-search.git
git push -u origin main
```

### 2. First-time setup on the VPS

Log in as root (Hostinger panel > VPS > SSH access shows the IP and root password):

```
ssh root@<vps-ip>
```

**Public repository:**

```
curl -fsSL https://raw.githubusercontent.com/<you>/epfo-search/main/deploy/setup.sh -o setup.sh
bash setup.sh https://github.com/<you>/epfo-search.git <your-domain-or-vps-ip>
```

**Private repository:** the VPS needs read access. On the VPS:

```
useradd --system --create-home --shell /bin/bash epfo
sudo -u epfo ssh-keygen -t ed25519 -N "" -f /home/epfo/.ssh/id_ed25519
cat /home/epfo/.ssh/id_ed25519.pub
```

Add that public key on GitHub: repository > Settings > Deploy keys > Add (read-only). Then:

```
sudo -u epfo ssh -o StrictHostKeyChecking=accept-new -T git@github.com   # says "successfully authenticated"
scp deploy/setup.sh root@<vps-ip>:setup.sh                             # run this one on your PC
bash setup.sh git@github.com:<you>/epfo-search.git <your-domain-or-vps-ip>
```

Open `http://<your-domain-or-vps-ip>`. The app runs under systemd, so it
restarts on crash and after a reboot.

### 3. HTTPS (needs a domain)

Point the domain's A record at the VPS IP (Hostinger panel > Domains > DNS), then on the VPS:

```
apt-get install -y certbot python3-certbot-nginx
certbot --nginx -d <your-domain>
```

### 4. Auto-deploy from GitHub

Every push to `main` then updates the VPS by itself.

On your PC, make a key used only for deploying:

```
ssh-keygen -t ed25519 -N "" -f epfo_deploy_key
```

Put the **public** key on the VPS:

```
mkdir -p /home/epfo/.ssh
echo "<contents of epfo_deploy_key.pub>" >> /home/epfo/.ssh/authorized_keys
chown -R epfo:epfo /home/epfo/.ssh && chmod 700 /home/epfo/.ssh && chmod 600 /home/epfo/.ssh/authorized_keys
```

On GitHub: repository > Settings > Secrets and variables > Actions > New secret:

| Name | Value |
|---|---|
| `VPS_HOST` | VPS IP or domain |
| `VPS_SSH_KEY` | full contents of `epfo_deploy_key` (the private key) |
| `VPS_PORT` | only if SSH is not on port 22 |

Delete both key files from your PC afterwards (or keep them somewhere safe,
never in the repo). Test it: GitHub > Actions > Deploy to VPS > Run workflow.

## Useful commands on the VPS

```
systemctl status epfo-search        # is it running?
journalctl -u epfo-search -n 50     # recent log
systemctl restart epfo-search       # restart
```

## Notes

- Each visitor gets their own EPFO session (cookie), so several people can search at once.
- The EPFO portal may refuse requests from some data-centre IPs. If searches
  fail only on the VPS, check `journalctl` and `last_search_response.html` in `/opt/epfo-search`.
- Nominatim's free service allows one lookup per second and no heavy use.
  Results are cached in `geocode_cache.json`, which is fine for light use;
  for many users switch to a paid geocoder or your own Nominatim.
