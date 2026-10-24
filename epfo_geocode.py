"""Turn EPFO establishment addresses into map coordinates.

Lookups use a local list of Indian places from GeoNames (data/in_places.tsv.gz,
CC BY 4.0), so placing a whole result list takes milliseconds and needs no
online service. On first use the list is turned into a small SQLite index
(data/in_places.sqlite) so it does not sit in memory.

EPFO addresses are often incomplete (no pincode, lines glued together such
as 'M.G.ROADERNAKULAM'), so each lookup tries, in order:
  1. a town or locality named in the address, near the EPFO office
                                                  -> precision "locality"
  2. the EPFO office city                         -> precision "office"
  3. state from the establishment ID prefix       -> precision "state"
Only when the office city is not in the local list is the free OpenStreetMap
Nominatim service asked (one request per second, answers cached in
geocode_cache.json).
"""

import gzip
import json
import math
import os
import re
import sqlite3
import threading
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
PLACES_GZ = os.path.join(HERE, "data", "in_places.tsv.gz")
PLACES_DB = os.path.join(HERE, "data", "in_places.sqlite")
CACHE_PATH = os.path.join(HERE, "geocode_cache.json")
NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "epfo-search-local/1.0 (personal EPFO establishment map)"
MIN_INTERVAL = 1.1  # seconds between Nominatim requests
MAX_KM_FROM_OFFICE = 150  # places farther than this from the EPFO office are wrong matches
MIN_SUFFIX = 5  # shortest tail of a glued word tried as a place name

# Words that are parts of addresses, not place names.
NOT_PLACES = {
    "ROAD", "STREET", "NAGAR", "MARG", "AREA", "INDUSTRIAL", "ESTATE", "COLONY", "BAZAR",
    "BAZAAR", "MARKET", "PLOT", "PHASE", "SECTOR", "NEAR", "OPPOSITE", "DISTT", "DIST",
    "DISTRICT", "TALUKA", "TEHSIL", "MAIN", "CROSS", "LAYOUT", "FLOOR", "BUILDING", "HOUSE",
    "POST", "OFFICE", "VILLAGE", "CITY", "TOWN", "WEST", "EAST", "NORTH", "SOUTH", "CENTRAL",
    "COMPLEX", "TOWER", "CAMPUS", "COLLEGE", "SCHOOL", "HOSPITAL", "LIMITED", "PRIVATE",
    "STATION", "JUNCTION", "CHOWK", "GALI", "LANE", "BLOCK", "WARD", "NAGARAM", "PURAM",
    "PALACE", "TEMPLE", "MILLS", "WORKS", "FACTORY", "COMPANY", "SOCIETY", "BRANCH",
}

# First two letters of an establishment ID name the EPFO zone, which is
# mostly one state. Only used as the last fallback.
STATE_BY_PREFIX = {
    "AP": "Andhra Pradesh", "BR": "Bihar", "CG": "Chhattisgarh", "DL": "Delhi",
    "GJ": "Gujarat", "GA": "Goa", "GN": "Haryana", "HR": "Haryana", "HP": "Himachal Pradesh",
    "JH": "Jharkhand", "JK": "Jammu and Kashmir", "KN": "Karnataka", "KR": "Kerala",
    "MP": "Madhya Pradesh", "MH": "Maharashtra", "NE": "Assam", "OR": "Odisha",
    "PB": "Punjab", "PY": "Puducherry", "RJ": "Rajasthan", "TN": "Tamil Nadu",
    "TS": "Telangana", "UK": "Uttarakhand", "UP": "Uttar Pradesh", "WB": "West Bengal",
}

_db_lock = threading.Lock()
_local = threading.local()
_lock = threading.Lock()
_last_call = 0.0
_cache = None


# ---- local place list ---------------------------------------------------

def _build_db():
    tmp = PLACES_DB + ".tmp"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.execute("CREATE TABLE place (key TEXT, lat REAL, lng REAL, pop INTEGER)")
    with gzip.open(PLACES_GZ, "rt", encoding="utf-8") as f:
        con.executemany("INSERT INTO place VALUES (?, ?, ?, ?)",
                        (line.split("\t")[:4] for line in f))
    con.execute("CREATE INDEX place_key ON place (key)")
    con.commit()
    con.close()
    os.replace(tmp, PLACES_DB)


def _db():
    """SQLite connection for this thread; builds the index on first use."""
    con = getattr(_local, "con", None)
    if con is None:
        with _db_lock:
            if not os.path.exists(PLACES_DB) or os.path.getmtime(PLACES_DB) < os.path.getmtime(PLACES_GZ):
                _build_db()
        con = _local.con = sqlite3.connect(f"file:{PLACES_DB}?mode=ro", uri=True)
    return con


def _places(key):
    """All places with this key: [(lat, lng, population)]."""
    if not os.path.exists(PLACES_GZ):
        return []
    return _db().execute("SELECT lat, lng, pop FROM place WHERE key = ?", (key,)).fetchall()


def _key(text):
    return re.sub(r"[^A-Z0-9]", "", text.upper())


# ---- Nominatim (only for office cities missing from the local list) ------

def _load_cache():
    global _cache
    if _cache is None:
        try:
            with open(CACHE_PATH, encoding="utf-8") as f:
                _cache = json.load(f)
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save_cache():
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_cache, f, ensure_ascii=False, indent=0)
    os.replace(tmp, CACHE_PATH)


def _nominatim(query):
    """One Nominatim query, cached. Returns [lat, lng] or None."""
    global _last_call
    with _lock:
        cache = _load_cache()
        if query in cache:
            return cache[query]
        wait = MIN_INTERVAL - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            r = requests.get(NOMINATIM, timeout=20, headers={"User-Agent": USER_AGENT},
                             params={"q": query, "format": "json", "limit": 1, "countrycodes": "in"})
            r.raise_for_status()
            hits = r.json()
        except (requests.RequestException, ValueError):
            return None  # not cached: try again next time
        finally:
            _last_call = time.time()
        point = [float(hits[0]["lat"]), float(hits[0]["lon"])] if hits else None
        cache[query] = point
        _save_cache()
        return point


# ---- matching -----------------------------------------------------------

def _clean(text):
    """Drop bracketed notes and undo repeated words: 'PATNAPATNA' -> 'PATNA'."""
    text = re.sub(r"\([^)]*\)", " ", text or "")
    text = re.sub(r"\b([A-Za-z]{3,})\1\b", r"\1", text)
    return " ".join(text.split())


def _km(a, b):
    """Great-circle distance between two (lat, lng) points."""
    la1, lo1, la2, lo2 = map(math.radians, (*a[:2], *b[:2]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def _office_point(office):
    """Biggest place with the office's name ('THANE (MUMBAI-II)' -> Thane)."""
    for part in [office] + re.split(r"[-/,]", office):
        hits = _places(_key(part))
        if hits:
            lat, lng, _ = max(hits, key=lambda h: h[2])
            return lat, lng
    point = _nominatim(f"{office}, India") if office else None
    return tuple(point) if point else None


def _candidates(address):
    """Keys that may name a place, most likely first: whole address parts
    from the last one back (the town is usually last), then single words,
    then the tails of glued words ('ROADERNAKULAM' -> 'ERNAKULAM')."""
    parts = [p for p in re.split(r"[,;]", address) if p.strip()]
    out = []
    for part in reversed(parts):
        words = [w for w in re.split(r"[^A-Za-z]+", part.upper()) if w]
        if len(words) > 1:
            out.append("".join(words))
        for w in reversed(words):
            if w in NOT_PLACES or len(w) < 3:
                continue
            out.append(w)
            out += [w[i:] for i in range(1, len(w) - MIN_SUFFIX + 1) if w[i:] not in NOT_PLACES]
    seen = set()
    return [k for k in out if not (k in seen or seen.add(k))]


def _locality(address, near):
    """First address candidate that is a real place close to the office."""
    for k in _candidates(address):
        hits = [h for h in _places(k) if near is None or _km(h, near) <= MAX_KM_FROM_OFFICE]
        if hits:
            # Several places share a name: prefer the bigger, then the nearer one.
            lat, lng, _ = max(hits, key=lambda h: (h[2], -_km(h, near) if near else 0))
            return lat, lng
    return None


def geocode(address, office, est_id=""):
    """Return {"lat", "lng", "precision"} or None if nothing matched."""
    address, office = _clean(address), _clean(office)
    near = _office_point(office) if office else None
    point = _locality(address, near) if address else None
    if point:
        return {"lat": point[0], "lng": point[1], "precision": "locality"}
    if near:
        return {"lat": near[0], "lng": near[1], "precision": "office"}
    state = STATE_BY_PREFIX.get((est_id or "")[:2].upper())
    point = _office_point(state) if state else None
    return {"lat": point[0], "lng": point[1], "precision": "state"} if point else None
