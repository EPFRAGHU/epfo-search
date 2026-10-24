"""Web page for EPFO establishment search.

Run:  python epfo_web.py   then open http://localhost:8765
You type the captcha shown on the page; the server talks to the EPFO portal.

Settings (environment variables):
  EPFO_HOST  address to listen on (default 127.0.0.1; a VPS behind nginx keeps this)
  EPFO_PORT  port to listen on (default 8765)
"""

import json
import os
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from epfo_geocode import geocode
from epfo_lookup import EpfoClient

HOST = os.environ.get("EPFO_HOST", "127.0.0.1")
PORT = int(os.environ.get("EPFO_PORT", "8765"))
SESSION_TTL = 30 * 60  # seconds a visitor's search stays usable while idle
MAX_SESSIONS = 1000

# One entry per browser, keyed by the "sid" cookie, so visitors don't see
# or overwrite each other's captcha and results.
_sessions = {}
_sessions_lock = threading.Lock()


def new_state():
    return {"client": None, "results": [], "results_client": None, "header": [], "seen": time.time()}


def get_state(sid):
    """Return this visitor's state, dropping expired ones."""
    now = time.time()
    with _sessions_lock:
        for k in [k for k, v in _sessions.items() if now - v["seen"] > SESSION_TTL]:
            del _sessions[k]
        st = _sessions.get(sid)
        if st is None:
            if len(_sessions) >= MAX_SESSIONS:
                del _sessions[min(_sessions, key=lambda k: _sessions[k]["seen"])]
            st = _sessions[sid] = new_state()
        st["seen"] = now
        return st

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EPFO Establishment Search</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --text:#1c2430; --muted:#5b6675; --line:#dde2e8; --accent:#1f5fbf; --err:#b42318;
          --paid-bg:#dcfce7; --paid-fg:#14532d; --paid-line:#86efac; --due-bg:#fee2e2; --due-fg:#7f1d1d; --due-line:#fca5a5; }
  @media (prefers-color-scheme: dark) { :root { --bg:#14171c; --card:#1d2128; --text:#e6e9ee; --muted:#9aa4b2; --line:#2f3540; --accent:#6aa0ff; --err:#ff7b72;
          --paid-bg:#123d24; --paid-fg:#bbf7d0; --paid-line:#1f6b3d; --due-bg:#45191b; --due-fg:#fecaca; --due-line:#7f2a2d; } }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font:15px/1.5 system-ui, Segoe UI, sans-serif; }
  main { max-width:1100px; margin:0 auto; padding:24px 16px 60px; }
  h1 { font-size:22px; margin:0 0 4px; }
  .sub { color:var(--muted); margin:0 0 20px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:18px; margin-bottom:18px; }
  form { display:grid; gap:12px; grid-template-columns: 1fr; max-width:460px; }
  label { font-weight:600; font-size:13px; color:var(--muted); display:block; margin-bottom:4px; }
  input { width:100%; padding:9px 11px; border:1px solid var(--line); border-radius:7px; background:var(--bg); color:var(--text); font-size:15px; }
  .cap { display:flex; gap:10px; align-items:center; }
  .cap img { height:50px; border:1px solid var(--line); border-radius:6px; background:#fff; }
  button { padding:9px 16px; border:0; border-radius:7px; background:var(--accent); color:#fff; font-weight:600; cursor:pointer; font-size:14px; }
  button.link { background:none; color:var(--accent); padding:4px 6px; }
  button:disabled { opacity:.6; cursor:wait; }
  .msg { color:var(--err); font-weight:600; }
  .wrap { overflow-x:auto; }
  table { border-collapse:collapse; width:100%; font-size:13.5px; }
  th, td { border:1px solid var(--line); padding:6px 8px; text-align:left; vertical-align:top; }
  th { background:var(--bg); }
  h2 { font-size:16px; margin:0 0 10px; }
  #map { height:460px; border-radius:8px; border:1px solid var(--line); }
  #mapStatus { color:var(--muted); font-size:13px; margin:0 0 8px; }
  .leaflet-popup-content { font-size:13px; line-height:1.45; color:#1c2430; }
  /* Payment calendar: financial years as rows, March..February as columns */
  .cal { border-collapse:separate; border-spacing:4px; width:auto; min-width:100%; }
  .cal th, .cal td { border:0; padding:0; text-align:center; vertical-align:middle; background:none; }
  .cal thead th { font-size:12px; color:var(--muted); font-weight:600; padding:2px 0; }
  .cal th.fy { text-align:left; white-space:nowrap; padding-right:6px; font-size:13px; color:var(--text); }
  .cal td.tot { white-space:nowrap; font-size:12px; color:var(--muted); padding-left:6px; text-align:right; }
  .cal button { width:100%; min-width:62px; height:46px; border-radius:7px; padding:2px 3px; font:inherit; font-size:11.5px;
                line-height:1.25; cursor:pointer; border:1px solid var(--line); background:var(--bg); color:var(--muted); }
  .cal button.paid { background:var(--paid-bg); color:var(--paid-fg); border-color:var(--paid-line); font-weight:600; }
  .cal button.due { background:var(--due-bg); color:var(--due-fg); border-color:var(--due-line); }
  .cal button.sel { outline:2px solid var(--accent); outline-offset:1px; }
  .cal button small { display:block; font-weight:400; opacity:.85; }
  .legend { display:flex; flex-wrap:wrap; gap:14px; font-size:12.5px; color:var(--muted); margin:6px 0 10px; }
  .legend i { display:inline-block; width:12px; height:12px; border-radius:3px; vertical-align:-1px; margin-right:5px; border:1px solid var(--line); }
  .legend .p { background:var(--paid-bg); border-color:var(--paid-line); } .legend .d { background:var(--due-bg); border-color:var(--due-line); }
  .paydetail { margin-top:12px; }
  .paydetail h3 { font-size:14px; margin:0 0 8px; }
  .tabs { display:flex; gap:6px; margin-bottom:10px; }
  .tabs button { background:var(--bg); color:var(--text); border:1px solid var(--line); padding:5px 12px; font-weight:500; }
  .tabs button.on { background:var(--accent); color:#fff; border-color:var(--accent); }
</style></head>
<body><main>
  <h1>EPFO Establishment Search</h1>
  <p class="sub">Search by 7-digit code or part of the name. Type the captcha shown, then Search.</p>
  <div class="card">
    <form id="f">
      <div><label for="q">Establishment code or name</label>
        <input id="q" required placeholder="e.g. 5059 (becomes 0005059) or TATA"></div>
      <div><label for="cap">Captcha</label>
        <div class="cap"><img id="capImg" alt="captcha">
          <button type="button" class="link" id="refresh">New captcha</button></div>
        <input id="cap" required autocomplete="off" style="margin-top:8px" placeholder="Type the letters above"></div>
      <div><button id="go">Search</button></div>
      <div id="msg" class="msg"></div>
    </form>
  </div>
  <div id="out"></div>
  <div class="card" id="mapCard" hidden>
    <h2>Map</h2>
    <p id="mapStatus"></p>
    <div id="map"></div>
  </div>
  <div id="details"></div>
<script>
const $ = id => document.getElementById(id);
function esc(s){ return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
function newCaptcha(){ $('capImg').src = '/captcha?t=' + Date.now(); $('cap').value=''; }
function table(rows, header){
  let h = '<div class="wrap"><table>';
  if (header && header.length) h += '<tr>' + header.map(x => '<th>'+esc(x)+'</th>').join('') + '</tr>';
  for (const r of rows) h += '<tr>' + r.map(x => '<td>'+esc(x)+'</td>').join('') + '</tr>';
  return h + '</table></div>';
}
// Codes shorter than 7 digits get leading zeros: 5059 -> 0005059
function padCode(){ const v = $('q').value.trim(); if (/^\d{1,6}$/.test(v)) $('q').value = v.padStart(7, '0'); }
$('q').addEventListener('blur', padCode);
$('refresh').onclick = newCaptcha;
$('f').onsubmit = async ev => {
  ev.preventDefault();
  $('msg').textContent = ''; $('out').innerHTML = ''; $('details').innerHTML = '';
  padCode();
  const q = $('q').value.trim();
  if (/^\d+$/.test(q) && q.length > 7) { $('msg').textContent = 'Establishment code is at most 7 digits.'; return; }
  if (!/^\d+$/.test(q) && q.length < 3) { $('msg').textContent = 'Enter at least 3 letters of the name.'; return; }
  $('go').disabled = true; $('go').textContent = 'Searching…';
  try {
    const r = await fetch('/search', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({q, captcha: $('cap').value.trim()})});
    const d = await r.json();
    if (d.error) { $('msg').textContent = d.error; }
    else {
      let h = '<div class="card"><h2>' + d.results.length + ' result(s)</h2>';
      h += '<div class="wrap"><table>';
      if (d.header.length) h += '<tr>' + d.header.map(x => '<th>'+esc(x)+'</th>').join('') + '<th></th></tr>';
      d.results.forEach((x, i) => {
        h += '<tr>' + x.cells.map(c => '<td>'+esc(c)+'</td>').join('')
          + '<td>' + (x.has_details ? '<button class="link" onclick="details(' + i + ')">View details</button>' : '') + '</td></tr>';
      });
      $('out').innerHTML = h + '</table></div></div>';
      plotResults(d.results);
    }
  } catch (e) { $('msg').textContent = 'Request failed: ' + e; }
  $('go').disabled = false; $('go').textContent = 'Search';
  newCaptcha();
};
async function details(i){
  $('details').innerHTML = '<div class="card">Loading details…</div>';
  let d;
  try {
    const r = await fetch('/details', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({index:i})});
    d = await r.json();
  } catch (e) { d = {error: 'Request failed: ' + e}; }
  if (d.error) { $('details').innerHTML = '<div class="card msg">' + esc(d.error) + '</div>'; return; }
  $('details').innerHTML = d.sections.map(s => {
    const pay = s.title === 'Payment Details' && s.rows.length > 1;
    if (pay) return paymentCard(s.rows);
    return '<div class="card"><h2>' + esc(s.title) + '</h2>' + table(s.rows) + '</div>';
  }).join('');
  $('details').scrollIntoView({behavior:'smooth'});
}

// ---- Payment calendar ----------------------------------------------------
// One row per financial year (current year on top), one box per wage month
// from March to February. Green = payment credited, red = no payment.
const MON = ['JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'];
const FY_MONTHS = [3,4,5,6,7,8,9,10,11,12,1,2];
let payData = null;

// "MAR-2024", "March 2024", "MAR-24", "15-MAR-2024 10:20", "03/2024", "032024", "2024-03", "15/03/2024"
function parseMonth(v){
  const s = String(v || '').toUpperCase().trim();
  let m = s.match(/\b(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)[A-Z]*[\s\-\/.,']*(\d{4}|\d{2})\b/);
  if (m) return {y: m[2].length === 2 ? 2000 + +m[2] : +m[2], m: MON.indexOf(m[1]) + 1};
  if ((m = s.match(/^(\d{1,2})[\-\/.](\d{1,2})[\-\/.](\d{4})/))) return ok(+m[3], +m[2]);
  if ((m = s.match(/^(\d{4})[\-\/.](\d{1,2})/))) return ok(+m[1], +m[2]);
  if ((m = s.match(/^(\d{1,2})[\-\/.](\d{4})$/))) return ok(+m[2], +m[1]);
  if ((m = s.match(/^(\d{2})(\d{4})$/))) return ok(+m[2], +m[1]);
  if ((m = s.match(/^(\d{4})(\d{2})$/))) return ok(+m[1], +m[2]);
  return null;
  function ok(y, mo){ return mo >= 1 && mo <= 12 && y > 1950 ? {y, m: mo} : null; }
}
const fyOf = p => p.m >= 3 ? p.y : p.y - 1;   // March starts the year
const fyLabel = fy => fy + '-' + String((fy + 1) % 100).padStart(2, '0');
const num = v => { const n = parseFloat(String(v || '').replace(/[^\d.\-]/g, '')); return isNaN(n) ? 0 : n; };
function money(n){
  if (n >= 1e7) return '₹' + (n / 1e7).toFixed(n >= 1e8 ? 0 : 1) + 'Cr';
  if (n >= 1e5) return '₹' + (n / 1e5).toFixed(n >= 1e6 ? 0 : 1) + 'L';
  if (n >= 1e3) return '₹' + (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + 'K';
  return '₹' + Math.round(n);
}
const fullMoney = n => '₹' + n.toLocaleString('en-IN', {maximumFractionDigits: 2});

function paymentCard(rows){
  const header = rows[0], body = rows.slice(1);
  const col = (...words) => header.findIndex(h => words.some(w => h.toLowerCase().includes(w)));
  const c = {wage: col('wage'), credit: col('credit', 'date'), amount: col('amount', 'amt'),
             emp: col('employee', 'member', 'emp'), trrn: col('trrn'), ecr: col('ecr')};
  const items = [];
  for (const r of body) {
    const when = parseMonth(r[c.wage]) || parseMonth(r[c.credit]);
    if (when) items.push({when, row: r});
  }
  const tableView = table(body, header);
  const title = 'Payment Details (' + body.length + ' payments)';
  if (!items.length) return '<div class="card"><h2>' + esc(title) + '</h2>' + tableView + '</div>';

  const byMonth = {};
  for (const it of items) (byMonth[it.when.y * 100 + it.when.m] ||= []).push(it.row);
  payData = {header, c, byMonth};

  // Red only between the first paid month and last month: earlier months
  // are before the establishment started paying; this month is not due yet.
  const first = Math.min(...items.map(it => it.when.y * 100 + it.when.m));
  const now = new Date(), lastDue = now.getMonth() === 0 ? (now.getFullYear() - 1) * 100 + 12 : now.getFullYear() * 100 + now.getMonth();
  const curFy = fyOf({y: now.getFullYear(), m: now.getMonth() + 1});
  const firstFy = fyOf({y: Math.floor(first / 100), m: first % 100});

  let h = '<div class="wrap"><table class="cal"><thead><tr><th></th>'
    + FY_MONTHS.map(m => '<th>' + MON[m - 1] + '</th>').join('') + '<th>Year total</th></tr></thead><tbody>';
  for (let fy = curFy; fy >= firstFy; fy--) {
    let total = 0, paidMonths = 0, dueMonths = 0;
    h += '<tr><th class="fy">' + fyLabel(fy) + '</th>';
    for (const m of FY_MONTHS) {
      const y = m >= 3 ? fy : fy + 1, k = y * 100 + m, list = byMonth[k];
      if (list) {
        const amt = list.reduce((s, r) => s + num(r[c.amount]), 0);
        const emp = c.emp >= 0 ? Math.max(...list.map(r => num(r[c.emp]))) : 0;
        total += amt; paidMonths++; dueMonths++;
        h += '<td><button class="paid" title="' + MON[m - 1] + ' ' + y + ': ' + fullMoney(amt) + '" onclick="showPay(' + k + ', this)">'
          + money(amt) + (emp ? '<small>' + emp + ' emp</small>' : '') + (list.length > 1 ? '<small>' + list.length + ' credits</small>' : '') + '</button></td>';
      } else if (k >= first && k <= lastDue) {
        dueMonths++;
        h += '<td><button class="due" title="' + MON[m - 1] + ' ' + y + ': no payment" onclick="showPay(' + k + ', this)">No<small>payment</small></button></td>';
      } else {
        h += '<td><button disabled title="' + MON[m - 1] + ' ' + y + '">–</button></td>';
      }
    }
    h += '<td class="tot"><b>' + (total ? fullMoney(total) : '–') + '</b><br>' + paidMonths + '/' + dueMonths + ' months</td></tr>';
  }
  h += '</tbody></table></div>';

  const legend = '<div class="legend"><span><i class="p"></i>Payment received</span><span><i class="d"></i>No payment</span>'
    + '<span>Click a month for date of credit, amount, employees and TRRN. Months are wage months.</span></div>';
  return '<div class="card"><h2>' + esc(title) + '</h2>'
    + '<div class="tabs"><button class="on" onclick="payView(this, 0)">Year / month</button><button onclick="payView(this, 1)">Table</button></div>'
    + '<div class="payview">' + legend + h + '<div class="paydetail" id="payDetail"></div></div>'
    + '<div class="payview" hidden>' + tableView + '</div></div>';
}

function payView(btn, i){
  const card = btn.closest('.card');
  card.querySelectorAll('.tabs button').forEach((b, j) => b.classList.toggle('on', j === i));
  card.querySelectorAll('.payview').forEach((v, j) => v.hidden = j !== i);
}

function showPay(k, btn){
  document.querySelectorAll('.cal button.sel').forEach(b => b.classList.remove('sel'));
  btn.classList.add('sel');
  const y = Math.floor(k / 100), m = k % 100, label = MON[m - 1] + ' ' + y;
  const list = payData.byMonth[k];
  if (!list) { $('payDetail').innerHTML = '<h3>' + label + '</h3><p class="msg">No payment found for wage month ' + label + '.</p>'; return; }
  const {header, c} = payData;
  const want = [['Date of credit', c.credit], ['Amount', c.amount], ['No. of employees', c.emp], ['TRRN', c.trrn], ['ECR filed', c.ecr], ['Wage month', c.wage]]
    .filter(([, i]) => i >= 0);
  const total = list.reduce((s, r) => s + num(r[c.amount]), 0);
  $('payDetail').innerHTML = '<h3>' + label + ' · ' + list.length + ' credit' + (list.length > 1 ? 's' : '') + ' · ' + fullMoney(total) + '</h3>'
    + table(list.map(r => want.map(([n, i]) => n === 'Amount' ? fullMoney(num(r[i])) : r[i])), want.map(([n]) => n));
}
// Map: the server places every result in one request from its local list
// of Indian places, so all pins appear together.
let map, pins, plotRun = 0;
const PRECISION = {locality: 'Town/locality from the address', office: 'Approximate: EPFO office city', state: 'Rough: state only'};
async function plotResults(results){
  const run = ++plotRun;
  $('mapCard').hidden = false;
  $('mapStatus').textContent = 'Placing ' + results.length + ' on the map…';
  if (!map) {
    map = L.map('map').setView([22.5, 79], 5);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
      {maxZoom: 19, attribution: '&copy; OpenStreetMap contributors'}).addTo(map);
    pins = L.featureGroup().addTo(map);
  }
  pins.clearLayers();
  map.invalidateSize();
  let d;
  try {
    const r = await fetch('/geocode', {method:'POST', headers:{'Content-Type':'application/json'}, body: '{}'});
    d = await r.json();
  } catch (e) { d = {error: 'Request failed: ' + e}; }
  if (run !== plotRun) return;  // a newer search started
  if (d.error) { $('mapStatus').textContent = d.error; return; }
  let town = 0, approx = 0, missed = 0;
  d.points.forEach((g, i) => {
    const p = g.point, x = results[i];
    if (!p) { missed++; return; }
    const good = p.precision === 'locality';
    good ? town++ : approx++;
    const gmaps = 'https://www.google.com/maps/search/?api=1&query=' + encodeURIComponent(g.query);
    L.circleMarker([p.lat, p.lng], {radius: 8, weight: 2, color: good ? '#1f5fbf' : '#c2410c', fillOpacity: .55})
      .bindPopup('<b>' + esc(g.name) + '</b><br>' + esc(g.address) + '<br>' + esc(g.est_id)
        + '<br><i>' + esc(PRECISION[p.precision]) + '</i><br><a href="' + gmaps + '" target="_blank" rel="noopener">Open in Google Maps</a>'
        + (x && x.has_details ? ' · <a href="#" onclick="details(' + i + ');return false">View details</a>' : ''))
      .addTo(pins);
  });
  if (pins.getLayers().length) map.fitBounds(pins.getBounds(), {maxZoom: 13, padding: [30, 30]});
  $('mapStatus').textContent = town + ' at town/locality (blue), ' + approx + ' at EPFO office city (orange)'
    + (missed ? ', ' + missed + ' not found' : '') + '. For the exact spot use "Open in Google Maps". '
    + 'Places © GeoNames, map © OpenStreetMap.';
}
newCaptcha();
</script>
</main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _session(self):
        """Visitor state from the sid cookie; a new cookie is issued if missing."""
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        sid = cookie["sid"].value if "sid" in cookie else ""
        self._new_sid = None
        if len(sid) != 43:  # token_urlsafe(32) length
            sid = self._new_sid = secrets.token_urlsafe(32)
        return get_state(sid)

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        if getattr(self, "_new_sid", None):
            self.send_header("Set-Cookie", f"sid={self._new_sid}; Path=/; HttpOnly; SameSite=Lax")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        state = self._session()
        if self.path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif self.path.startswith("/captcha"):
            # Fresh EPFO session for every captcha: new cookie + new HDIV tokens
            try:
                c = EpfoClient()
                c.load_home()
                img = c.s.get(c.captcha_url, timeout=30).content
                state["client"] = c
                self._send(200, img, "image/png")
            except (requests.RequestException, RuntimeError) as e:
                self._send(502, str(e).encode(), "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        state = self._session()
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError:
            return self._json({"error": "Bad request."}, 400)
        c = state["client"]
        if c is None:
            return self._json({"error": "Load a captcha first."})
        try:
            if self.path == "/search":
                q = body.get("q", "").strip()
                is_code = q.isdigit()
                if is_code and len(q) > 7:
                    return self._json({"error": "Establishment code is at most 7 digits."})
                header, results = c.search(est_code=q if is_code else "",
                                           est_name="" if is_code else q,
                                           captcha=body.get("captcha", ""))
                if isinstance(results, str):
                    return self._json({"error": results.replace("JSP Page", "").strip()[:500]})
                # Details must use the session that ran the search, even after
                # the page loads a new captcha (which starts a new session).
                state["results"], state["results_client"] = results, c
                if header and header[-1].lower() == "action":
                    header = header[:-1]
                    for r in results:
                        r["cells"] = r["cells"][:len(header)]
                state["header"] = header
                return self._json({"header": header, "results": [
                    {"cells": r["cells"], "has_details": bool(r["est_id"])} for r in results]})
            if self.path == "/details":
                r = state["results"][int(body["index"])]
                sections = state["results_client"].details(r["est_id"], r["urls"])
                return self._json({"sections": [{"title": t, "rows": rows} for t, rows in sections]})
            if self.path == "/geocode":
                # Every result at once. Columns: Establishment ID | Establishment Name | Address | Office Name
                cols = {h.lower(): i for i, h in enumerate(state["header"])}
                idx = [cols.get(h, d) for h, d in (("establishment id", 0), ("establishment name", 1),
                                                    ("address", 2), ("office name", 3))]
                points = []
                for r in state["results"]:
                    cells = r["cells"]
                    est_id, name, address, office = (cells[i] if i < len(cells) else "" for i in idx)
                    points.append({"point": geocode(address, office, est_id), "est_id": est_id,
                                   "name": name, "address": address,
                                   "query": ", ".join(x for x in (name, address, office) if x)})
                return self._json({"points": points})
        except (requests.RequestException, IndexError, KeyError, ValueError) as e:
            return self._json({"error": f"Failed: {e}"})
        self._json({"error": "unknown path"}, 404)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"EPFO search running at http://{'localhost' if HOST == '127.0.0.1' else HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
