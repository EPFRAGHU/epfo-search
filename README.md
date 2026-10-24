# EPFO Establishment Search

Search EPFO establishments by code or name, view their details and payment
history, and see them on a map. You type the EPFO captcha yourself; the app
talks to the EPFO public portal for you.

- `epfo_web.py` – the web app (search page, details, map)
- `epfo_lookup.py` – EPFO portal client; also a terminal version (`python epfo_lookup.py`, needs `pip install pillow` too)
- `epfo_geocode.py` – turns addresses into map points with OpenStreetMap Nominatim (free, cached)
- `deploy/` – files for running on a VPS
- `Dockerfile` – container image, used by Coolify

## Run on your PC

```
pip install -r requirements.txt
python epfo_web.py
```

Open http://localhost:8765. Stop with `Ctrl+C`. After changing the code,
stop and start it again.

## Run on Coolify (recommended if your VPS has Coolify)

1. DNS: add an `A` record for the subdomain (e.g. `epfo`) pointing to the VPS IP.
2. Coolify > Projects > your project > **+ New** > **Public Repository**:
   `https://github.com/EPFRAGHU/epfo-search`, branch `main`.
3. **Build Pack:** `Dockerfile`. **Ports Exposes:** `8765`.
4. **Domains:** `https://epfo.epf-dashboard.online` (your subdomain). Save, then **Deploy**.

5. **Persistent Storage** > Add > Volume, destination path `/data`. Exact
   locations people save are kept there; without it they are lost on redeploy.

Coolify's proxy adds the route and a Let's Encrypt certificate, and other
apps are not touched. Enable auto-deploy in the app's settings to redeploy
on every push to `main`.

## Put it on a Hostinger VPS (without Coolify)

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
bash setup.sh --check https://github.com/<you>/epfo-search.git <your-domain-or-vps-ip>
bash setup.sh https://github.com/<you>/epfo-search.git <your-domain-or-vps-ip>
```

`--check` only looks at the server and changes nothing. It stops if a port,
user, folder or service name is already taken, or if a missing package would
upgrade something other apps use. If it reports a port in use, pick another:
`PUBLIC_PORT=8095 bash setup.sh ...`

Safe on a VPS that already runs other apps:

- never installs nginx and never upgrades packages; installs only `python3-venv`/`git` if missing
- with an IP, the app gets its own port (default **8090**), so port 80/443 and
  existing sites are not touched; with a domain, nginx answers on port 80
  only for that exact name
- adds just one nginx site file, never edits other sites or the default site;
  removes its file again if `nginx -t` fails
- everything it creates is named `epfo-search` / `epfo` (`/opt/epfo-search`,
  service `epfo-search`, user `epfo`)

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

The script prints the address to open, e.g. `http://<vps-ip>:8090`. If it
doesn't load, allow that TCP port in Hostinger hPanel > VPS > Firewall. The
app runs under systemd, so it restarts on crash and after a reboot.

To remove it completely later (touches nothing else):

```
systemctl disable --now epfo-search
rm -f /etc/systemd/system/epfo-search.service /etc/sudoers.d/epfo-search
rm -f /etc/nginx/sites-enabled/epfo-search /etc/nginx/sites-available/epfo-search && nginx -t && systemctl reload nginx
rm -rf /opt/epfo-search && userdel -r epfo
```

### 3. HTTPS (needs a domain)

Point the domain's A record at the VPS IP (Hostinger panel > Domains > DNS), then on the VPS:

```
apt-get install -y certbot python3-certbot-nginx
certbot --nginx -d <your-domain>
```

### 4. Updating

Run the same `setup.sh` command again; it pulls the latest code from GitHub
and restarts the app. (On Coolify, use Redeploy or Auto Deploy instead.)

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
- **Google map (optional):** set the environment variable `GOOGLE_MAPS_API_KEY`
  (Coolify: app > Environment Variables) to show the real Google map with
  roads, landmarks, terrain and satellite. It loads with `region=IN`, so Google
  draws India's official boundary. In Google Cloud enable "Maps JavaScript API"
  and restrict the key to `https://search.epf-dashboard.online/*`. Without a
  key, or if Google rejects it, the map below is used.
- The map shows India only, drawn from India's official boundary and states
  (Survey of India map via [DataMeet](https://github.com/datameet/maps), CC BY 4.0:
  `data/india_states.geojson`, `data/india_outline.geojson`). Jammu & Kashmir
  includes PoK; Ladakh includes Gilgit-Baltistan and Aksai Chin. No world map
  tiles are loaded. Rebuilt from DataMeet `States/Admin2.shp` with
  `mapshaper Admin2.shp -rename-fields name=ST_NM -simplify 3% keep-shapes -clean`.
- Map pins come from a local list of Indian places (`data/in_places.tsv.gz`,
  from [GeoNames](https://www.geonames.org), CC BY 4.0), so a whole result
  list is placed in milliseconds. Pins are at the town/locality named in the
  address, or at the EPFO office city; "Open in Google Maps" in each pin
  searches the full address. Rebuild the list with `tools/build_places.py`.
- Only office cities missing from that list are looked up online (OpenStreetMap
  Nominatim, one request per second, cached in `geocode_cache.json`).
