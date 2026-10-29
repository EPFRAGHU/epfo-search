"""Web page for EPFO establishment search.

Run:  python epfo_web.py   then open http://localhost:8765
You type the captcha shown on the page; the server talks to the EPFO portal.

Settings (environment variables):
  EPFO_HOST  address to listen on (default 127.0.0.1; a VPS behind nginx keeps this)
  EPFO_PORT  port to listen on (default 8765)
  GOOGLE_MAPS_API_KEY  optional; shows the real Google map (region India).
             Restrict the key to your site's address in Google Cloud.
"""

import json
import os
import secrets
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from epfo_geocode import address_from_details, geocode, known_address, remember_address
from epfo_lookup import BASE, EpfoClient, parse_tables

HOST = os.environ.get("EPFO_HOST", "127.0.0.1")
GMAPS_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
PORT = int(os.environ.get("EPFO_PORT", "8765"))
# India's official boundary and states (Survey of India via DataMeet, CC BY 4.0)
MAP_FILES = {f"/map/{n}": os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", n)
             for n in ("india_states.geojson", "india_outline.geojson")}
SESSION_TTL = 30 * 60  # seconds a visitor's search stays usable while idle
MAX_SESSIONS = 1000
MAX_SEARCHES = 5  # recent searches kept per visitor (one per open tab, roughly)

# One entry per browser, keyed by the "sid" cookie, so visitors don't see
# or overwrite each other's captcha and results.
_sessions = {}
_sessions_lock = threading.Lock()


def row_fields(search, row):
    """(establishment id, name, address, office) of a result row, found by
    column name: Establishment ID | Establishment Name | Address | Office Name."""
    cols = {h.lower(): i for i, h in enumerate(search["header"])}
    cells = row["cells"]
    return tuple(cells[i] if i < len(cells) else "" for i in
                 (cols.get(h, d) for h, d in (("establishment id", 0), ("establishment name", 1),
                                               ("address", 2), ("office name", 3))))


def details_info(known):
    """What the page shows from an establishment's EPFO details; "read"
    tells it whether they were read yet."""
    known = known or {}
    return ({k: known.get(k) or "" for k in ("pin", "city", "district", "state", "zone", "region")}
            | {"read": bool(known.get("read"))})


def point_info(search, row):
    """Map data of one result row: its pin, name, office, and what its EPFO
    details said (pincode, city, district, state, zone, region)."""
    est_id, name, address, office = row_fields(search, row)
    known = known_address(est_id) or {}
    full = known.get("address") or ", ".join(x for x in (address, office) if x)
    return {"point": geocode(address, office, est_id), "est_id": est_id, "name": name,
            "address": address, "office": office, **details_info(known),
            "query": ", ".join(x for x in (name, full) if x)}


def new_state():
    # client: EPFO session of the latest captcha; searches: search id ->
    # {"results", "client", "header"}, oldest first
    return {"client": None, "searches": {}, "seen": time.time()}


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
          --paid-bg:#dcfce7; --paid-fg:#14532d; --paid-line:#86efac; --due-bg:#fee2e2; --due-fg:#7f1d1d; --due-line:#fca5a5;
          --map-sea:#eef3f8; --map-land:#fdfcf8; --map-state:#b9c2cc; --map-border:#4b5563; }
  @media (prefers-color-scheme: dark) { :root { --bg:#14171c; --card:#1d2128; --text:#e6e9ee; --muted:#9aa4b2; --line:#2f3540; --accent:#6aa0ff; --err:#ff7b72;
          --paid-bg:#123d24; --paid-fg:#bbf7d0; --paid-line:#1f6b3d; --due-bg:#45191b; --due-fg:#fecaca; --due-line:#7f2a2d;
          --map-sea:#111820; --map-land:#232a33; --map-state:#46505c; --map-border:#c5ccd6; } }
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
  #map { height:520px; border-radius:8px; border:1px solid var(--line); background:var(--map-sea); }
  .st-label { background:none; border:0; box-shadow:none; color:var(--muted); font-size:11px; font-weight:600; padding:0; }
  .st-label::before { display:none; }
  #map.far .st-label { display:none; }
  .est-label { font-size:11px; line-height:1.25; padding:2px 5px; text-align:center; font-weight:600; color:#1c2430;
               background:rgba(255,255,255,.88); border:1px solid #b9c2cc; box-shadow:none;
               width:max-content; max-width:240px; white-space:normal; }
  #mapStatus { color:var(--muted); font-size:13px; margin:0 0 8px; }
  .leaflet-popup-content, .pop { font-size:13px; line-height:1.45; color:#1c2430; }
  tr.sel td { background:color-mix(in srgb, var(--accent) 14%, transparent); }
  .nowrap { white-space:nowrap; }
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
      if (d.header.length) h += '<tr>' + d.header.map(x => '<th>'+esc(x)+'</th>').join('') + '<th></th><th>Map</th></tr>';
      d.results.forEach((x, i) => {
        h += '<tr id="row' + i + '">' + x.cells.map(c => '<td>'+esc(c)+'</td>').join('')
          + '<td>' + (x.has_details ? '<button class="link" onclick="details(' + i + ')">View details</button>' : '') + '</td>'
          + '<td><button class="link nowrap" onclick="showOnMap(' + i + ')" title="Show this establishment on the map">📍 Show on map</button></td></tr>';
      });
      $('out').innerHTML = h + '</table></div></div>';
      lastResults = d.results; lastHeader = d.header; searchId = d.search; pinTried.clear();
      plotResults(d.results);
    }
  } catch (e) { $('msg').textContent = 'Request failed: ' + e; }
  $('go').disabled = false; $('go').textContent = 'Search';
  newCaptcha();
};
// The server no longer has this search (it restarted, or the search was
// idle too long): back to the form with a fresh captcha, search text kept.
function searchAgain(){
  newCaptcha();
  $('msg').textContent = 'This search has expired (the app was updated or idle). Type the new captcha and press Search again.';
  $('f').scrollIntoView({behavior: 'smooth', block: 'start'});
  $('cap').focus({preventScroll: true});
}
async function details(i){
  $('details').innerHTML = '<div class="card">Loading details…</div>';
  let d;
  try {
    const r = await fetch('/details', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({index: i, search: searchId})});
    d = await r.json();
  } catch (e) { d = {error: 'Request failed: ' + e}; }
  if (d.expired) { $('details').innerHTML = ''; return searchAgain(); }
  if (d.error) {
    $('details').innerHTML = '<div class="card msg">' + esc(d.error) + '</div>';
    $('details').scrollIntoView({behavior: 'smooth', block: 'center'});
    return;
  }
  $('details').innerHTML = d.sections.map(s => {
    const pay = s.title === 'Payment Details' && s.rows.length > 1;
    if (pay) return paymentCard(s.rows);
    return '<div class="card"><h2>' + esc(s.title) + '</h2>' + table(s.rows) + '</div>';
  }).join('');
  // The details just gave this establishment's pincode: redraw the pins
  // when that moves its pin
  pinTried.add(i);
  const p = lastPoints[i] && lastPoints[i].point;
  if (lastPoints[i] && !lastPoints[i].read) await plotResults(lastPlot);  // shows the new state, zone, region
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
// Map. With a Google Maps API key (GOOGLE_MAPS_API_KEY on the server) this is
// the real Google map (roads, landmarks, terrain, satellite) with region=IN,
// so Google draws India's official boundary. Without a key, or if Google
// rejects the key, it falls back to an India-only map drawn from India's
// official boundary (Survey of India map via DataMeet: Jammu & Kashmir with
// PoK, Ladakh with Gilgit-Baltistan and Aksai Chin).
// The server places every result in one request from its local list of
// Indian places, so all pins appear together.
const GMAPS_KEY = __GMAPS_KEY__;
let view = null, plotRun = 0, lastPlot = null;

function loadGoogle(){
  return new Promise((resolve, reject) => {
    window.__gmapsReady = resolve;
    // Google calls this when the key is invalid or not allowed for this site
    window.gm_authFailure = () => { googleFailed = true; freshMapBox(); view = null; if (lastPlot) plotResults(lastPlot); };
    const s = document.createElement('script');
    s.src = 'https://maps.googleapis.com/maps/api/js?key=' + encodeURIComponent(GMAPS_KEY)
      + '&region=IN&language=en&loading=async&callback=__gmapsReady';
    s.async = true; s.onerror = () => reject(new Error('Google Maps did not load'));
    document.head.appendChild(s);
  });
}
let googleFailed = false;
// Empty #map element, so the fallback map does not inherit Google's leftovers
function freshMapBox(){ const old = $('map'), box = document.createElement('div'); box.id = 'map'; old.replaceWith(box); }

async function googleView(){
  await loadGoogle();
  const g = google.maps;
  const map = new g.Map($('map'), {center: {lat: 22.5, lng: 80}, zoom: 5, mapTypeId: 'roadmap',
    mapTypeControl: true, mapTypeControlOptions: {mapTypeIds: ['roadmap', 'terrain', 'satellite', 'hybrid']},
    streetViewControl: true, fullscreenControl: true, gestureHandling: 'greedy'});
  const info = new g.InfoWindow();
  let markers = [], byIndex = {};
  return {
    clear(){ markers.forEach(m => m.setMap(null)); markers = []; byIndex = {}; info.close(); },
    remove(i){
      const e = byIndex[i]; if (!e) return;
      e[0].setMap(null); markers = markers.filter(m => m !== e[0]); delete byIndex[i];
    },
    add(lat, lng, color, html, i, tip){
      const m = new g.Marker({position: {lat, lng}, map, title: tip.join('\n'), icon: {path: g.SymbolPath.CIRCLE, scale: 8,
        fillColor: color, fillOpacity: .85, strokeColor: '#fff', strokeWeight: 2}});
      m.addListener('click', () => { info.setContent(html); info.open({map, anchor: m}); onPinOpen(i); });
      markers.push(m); byIndex[i] = [m, html];
    },
    focus(i){
      const e = byIndex[i]; if (!e) return;
      map.setCenter(e[0].getPosition()); map.setZoom(Math.max(map.getZoom(), 8));
      info.setContent(e[1]); info.open({map, anchor: e[0]});
    },
    fit(){
      if (!markers.length) return;
      const b = new g.LatLngBounds(); markers.forEach(m => b.extend(m.getPosition()));
      map.fitBounds(b, 40);
      g.event.addListenerOnce(map, 'idle', () => { if (map.getZoom() > 13) map.setZoom(13); });
    },
  };
}

async function indiaView(){
  const css = getComputedStyle(document.documentElement), v = n => css.getPropertyValue(n).trim();
  const map = L.map('map', {zoomSnap: 0.25, minZoom: 4, maxZoom: 12, maxBounds: [[2, 60], [42, 104]], maxBoundsViscosity: 0.8,
                      attributionControl: true}).fitBounds([[6.5, 68], [37.2, 97.5]]);
  map.attributionControl.setPrefix('').addAttribution(
    'India boundary: Survey of India via <a href="https://github.com/datameet/maps" target="_blank" rel="noopener">DataMeet</a> (CC BY 4.0) · Places © GeoNames');
  const [states, outline] = await Promise.all(['/map/india_states.geojson', '/map/india_outline.geojson']
    .map(u => fetch(u).then(r => r.json())));
  L.geoJSON(states, {
    style: {color: v('--map-state'), weight: 0.8, fillColor: v('--map-land'), fillOpacity: 1},
    onEachFeature: (f, layer) => layer.bindTooltip(f.properties.name, {permanent: true, direction: 'center', className: 'st-label'}),
  }).addTo(map);
  L.geoJSON(outline, {style: {color: v('--map-border'), weight: 1.6, fill: false}, interactive: false}).addTo(map);
  const far = () => $('map').classList.toggle('far', map.getZoom() < 5.5);
  map.on('zoomend', far); far();
  const pins = L.featureGroup().addTo(map);
  let byIndex = {};
  return {
    clear(){ pins.clearLayers(); byIndex = {}; map.invalidateSize(); },
    remove(i){ if (byIndex[i]) { pins.removeLayer(byIndex[i]); delete byIndex[i]; } },
    add(lat, lng, color, html, i, tip){
      // Always-on label under the dot, one line each (text nodes: no escaping needed)
      const label = document.createElement('div');
      tip.forEach((line, k) => { if (k) label.append(document.createElement('br')); label.append(line); });
      byIndex[i] = L.circleMarker([lat, lng], {radius: 8, weight: 2, color, fillOpacity: .55})
        .bindPopup(html).bindTooltip(label, {permanent: true, direction: 'bottom', offset: [0, 6], className: 'est-label'})
        .on('popupopen', () => onPinOpen(i)).addTo(pins);
    },
    focus(i){
      const m = byIndex[i]; if (!m) return;
      map.setView(m.getLatLng(), Math.max(map.getZoom(), 7), {animate: false});
      m.openPopup();
    },
    // extra room at the bottom for the labels under the dots
    fit(){ if (pins.getLayers().length) map.fitBounds(pins.getBounds(), {maxZoom: 13, paddingTopLeft: [40, 30], paddingBottomRight: [40, 90]}); },
  };
}

async function makeView(){
  if (GMAPS_KEY && !googleFailed) {
    try { return await googleView(); } catch (e) { googleFailed = true; freshMapBox(); }
  }
  return indiaView();
}

// searchId: the server's ID for the search these rows came from
let lastPoints = [], lastResults = [], lastHeader = [], searchId = null;
const pinTried = new Set();
const COLOR = {saved: '#15803d', pincode: '#0e7490', locality: '#1f5fbf'};
const PRECISION = {saved: 'Exact location (saved)', pincode: 'Pincode area, from EPFO details',
                   locality: 'Approximate: town/locality from the address',
                   office: 'Approximate: EPFO office city', state: 'Rough: state only'};

// Details not read yet for this row (and it has them)?
const needsDetails = i => lastPoints[i] && !lastPoints[i].read && lastResults[i] && lastResults[i].has_details && !pinTried.has(i);
// Clicking a dot whose details are not read yet reads them now
function onPinOpen(i){ if (needsDetails(i)) showOnMap(i); }
// "Ernakulam, Ernakulam, Kerala" -> "Ernakulam, Kerala"
const uniq = list => [...new Map(list.filter(Boolean).map(s => [s.toLowerCase(), s])).values()];

// Draw (or redraw) the dot of row i: colour, popup, and the label under it
// with name, code, city/district/state and EPFO office.
function drawPoint(i){
  view.remove(i);
  const g = lastPoints[i], p = g && g.point, x = lastResults[i];
  if (!p) return;
  const gmaps = 'https://www.google.com/maps/search/?api=1&query=' + encodeURIComponent(g.query);
  const facts = [['City', g.city], ['District', g.district], ['State', g.state], ['Zone', g.zone], ['Region', g.region]]
    .filter(f => f[1]).map(f => f[0] + ': <b>' + esc(f[1]) + '</b>').join('<br>');
  const html = '<div class="pop"><b>' + esc(g.name) + '</b>' + (g.office ? ' (' + esc(g.office) + ')' : '')
    + '<br>' + esc(g.address) + '<br>' + esc(g.est_id)
    + (facts ? '<br>' + facts : (!g.read && x && x.has_details ? '<br><i>Reading city, state, zone, region…</i>' : ''))
    + '<br><i>' + esc(p.precision === 'pincode' ? 'Pincode ' + p.pin + ' area, from EPFO details' : PRECISION[p.precision]) + '</i>'
    + '<br><a href="' + gmaps + '" target="_blank" rel="noopener">Open in Google Maps</a>'
    + (x && x.has_details ? ' · <a href="#" onclick="details(' + i + ');return false">View details</a>' : '') + '</div>';
  view.add(p.lat, p.lng, COLOR[p.precision] || '#c2410c', html, i,
           [g.name, g.est_id, uniq([g.city, g.district, g.state]).join(', '), g.office ? 'EPFO: ' + g.office : ''].filter(Boolean));
}

function mapSummary(){
  const n = {saved: 0, pincode: 0, locality: 0, approx: 0, missed: 0};
  lastPoints.forEach(g => { const p = g.point; n[!p ? 'missed' : p.precision in COLOR ? p.precision : 'approx']++; });
  return (n.saved ? n.saved + ' exact (green), ' : '') + (n.pincode ? n.pincode + ' by pincode (teal), ' : '')
    + n.locality + ' at town/locality (blue), ' + n.approx + ' at EPFO office city (orange)'
    + (n.missed ? ', ' + n.missed + ' not found' : '') + '. Click "📍 Show on map" in a row to find it on the map.';
}

// Read one row's EPFO details (pincode, city, district, state, zone, region;
// the server keeps them for everyone) and redraw its dot.
async function readDetails(i){
  pinTried.add(i);
  let d; try {
    const res = await fetch('/pincode', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({index: i, search: searchId})});
    d = await res.json();
  } catch (e) { d = {error: String(e)}; }
  if (d.expired) { searchAgain(); return false; }
  if (d.error) return true;          // skip this row, go on with the others
  lastPoints[i] = d;
  drawPoint(i);
  return true;
}

// After a search: read the details of every row not read before, one at a
// time, so every label fills in; stops when a newer search starts.
async function readAll(run){
  const todo = lastPoints.map((_, i) => i).filter(needsDetails);
  for (let k = 0; k < todo.length; k++) {
    if (run !== plotRun) return;
    if (!needsDetails(todo[k])) continue;
    $('mapStatus').textContent = 'Reading city, district and state from EPFO details: ' + (k + 1) + ' of ' + todo.length + '…';
    if (!await readDetails(todo[k])) return;
  }
  if (run === plotRun) $('mapStatus').textContent = mapSummary();
}

// Show one establishment on the map: zoom to its pin and open its popup.
async function showOnMap(i){
  document.querySelectorAll('tr.sel').forEach(r => r.classList.remove('sel'));
  if ($('row' + i)) $('row' + i).classList.add('sel');
  $('mapCard').hidden = false;
  $('map').scrollIntoView({behavior: 'smooth', block: 'center'});
  if (needsDetails(i)) await readDetails(i);
  if (view && view.focus) view.focus(i);
}

async function plotResults(results){
  const run = ++plotRun;
  lastPlot = results;
  $('mapCard').hidden = false;
  $('mapStatus').textContent = 'Placing ' + results.length + ' on the map…';
  if (!view) view = await makeView();
  view.clear();
  let d;
  try {
    const r = await fetch('/geocode', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({search: searchId})});
    d = await r.json();
  } catch (e) { d = {error: 'Request failed: ' + e}; }
  if (run !== plotRun) return;  // a newer search started
  if (d.error) { $('mapStatus').textContent = d.error; if (d.expired) searchAgain(); return; }
  lastPoints = d.points;
  lastPoints.forEach((_, i) => drawPoint(i));
  view.fit();
  $('mapStatus').textContent = mapSummary();
  readAll(run);
}
newCaptcha();
</script>
</main></body></html>""".replace("__GMAPS_KEY__", json.dumps(GMAPS_KEY))


class Handler(BaseHTTPRequestHandler):
    def _session(self):
        """Visitor state from the sid cookie; a new cookie is issued if missing."""
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        sid = cookie["sid"].value if "sid" in cookie else ""
        self._new_sid = None
        if len(sid) != 43:  # token_urlsafe(32) length
            sid = self._new_sid = secrets.token_urlsafe(32)
        return get_state(sid)

    def _send(self, code, body, ctype, cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", cache)
        if getattr(self, "_new_sid", None):
            self.send_header("Set-Cookie", f"sid={self._new_sid}; Path=/; HttpOnly; SameSite=Lax")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        state = self._session()
        # Links shared through chat apps often get "?..." appended
        if self.path.split("?", 1)[0] in ("/", "/index.html"):
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif self.path in MAP_FILES:
            with open(MAP_FILES[self.path], "rb") as f:
                self._send(200, f.read(), "application/geo+json", cache="public, max-age=86400")
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
        try:
            if self.path == "/search":
                c = state["client"]
                if c is None:
                    return self._json({"error": "Load a captcha first."})
                q = body.get("q", "").strip()
                is_code = q.isdigit()
                if is_code and len(q) > 7:
                    return self._json({"error": "Establishment code is at most 7 digits."})
                header, results = c.search(est_code=q if is_code else "",
                                           est_name="" if is_code else q,
                                           captcha=body.get("captcha", ""))
                if isinstance(results, str):
                    return self._json({"error": results.replace("JSP Page", "").strip()[:500]})
                if header and header[-1].lower() == "action":
                    header = header[:-1]
                    for r in results:
                        r["cells"] = r["cells"][:len(header)]
                # Each search gets an ID that the page sends back with every
                # row click, so a row always refers to its own search even
                # when another tab searched since. Details must use the EPFO
                # session that ran the search (the page loads a new captcha,
                # which starts a new one), so it is kept with the results.
                search_id = secrets.token_hex(6)
                searches = state["searches"]
                searches[search_id] = {"results": results, "client": c, "header": header}
                while len(searches) > MAX_SEARCHES:
                    del searches[next(iter(searches))]
                return self._json({"search": search_id, "header": header, "results": [
                    {"cells": r["cells"], "has_details": bool(r["est_id"])} for r in results]})
            s = state["searches"].get(body.get("search"))
            if s is None:
                return self._json({"error": "This search has expired (the app was updated, or it was idle "
                                            "for a while). Please search again.", "expired": True})
            if self.path == "/details":
                r = s["results"][int(body["index"])]
                sections = s["client"].details(r["est_id"], r["urls"])
                # Keep pincode, address, state, zone, region for the map, for everyone
                info = address_from_details(sections)
                remember_address(row_fields(s, r)[0], {**info, "read": True})
                return self._json({"sections": [{"title": t, "rows": rows} for t, rows in sections], "pin": info["pin"]})
            if self.path == "/pincode":
                # Pincode, state, zone and region of one row without opening
                # all its details: fetch its detail panels one at a time
                # until they are found (once per establishment, for everyone).
                r = s["results"][int(body["index"])]
                est_id = row_fields(s, r)[0]
                known = known_address(est_id)
                if not (known and known.get("read")) and r.get("est_id") and r.get("urls"):
                    c, panels, info = s["client"], [], {}
                    for url in r["urls"]:
                        if not url:
                            continue
                        resp = c._post(BASE + url, {"EstId": r["est_id"]})
                        resp.raise_for_status()
                        panels.append(("", parse_tables(resp.text)))
                        info = address_from_details(panels)
                        if info["pin"] and info["zone"] and info["region"]:
                            break
                    remember_address(est_id, {**info, "read": True})
                # The row's map data again, now with its pincode pin and details
                return self._json(point_info(s, r))
            if self.path == "/geocode":
                # Every result at once
                return self._json({"points": [point_info(s, r) for r in s["results"]]})
        except (requests.RequestException, IndexError, KeyError, ValueError, TypeError) as e:
            return self._json({"error": f"Failed: {e}"})
        self._json({"error": "unknown path"}, 404)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    print(f"EPFO search running at http://{'localhost' if HOST == '127.0.0.1' else HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
