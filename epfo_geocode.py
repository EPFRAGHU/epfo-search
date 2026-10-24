"""Turn EPFO establishment addresses into map coordinates.

Uses the free OpenStreetMap Nominatim service. Its usage policy allows at
most one request per second, so calls are throttled and every answer is
cached in geocode_cache.json; the same address is never looked up twice.

EPFO addresses are often incomplete (no pincode, lines run together), so
each lookup tries, in order:
  1. address + EPFO office city, then address alone  -> precision "address"
  2. each part of the address (last part first, usually the town or
     locality), with the state when known         -> precision "locality"
  3. EPFO office city alone       -> precision "office"   (approximate)
  4. state from the establishment ID prefix -> precision "state" (rough)
"""

import json
import math
import os
import re
import threading
import time

import requests

NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "epfo-search-local/1.0 (personal EPFO establishment map)"
CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "geocode_cache.json")
MIN_INTERVAL = 1.1  # seconds between Nominatim requests
MAX_KM_FROM_OFFICE = 150  # address hits farther than this from the EPFO office are wrong matches

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

_lock = threading.Lock()
_last_call = 0.0
_cache = None


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


def _lookup(query):
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
        finally:
            _last_call = time.time()
        point = [float(hits[0]["lat"]), float(hits[0]["lon"])] if hits else None
        cache[query] = point
        _save_cache()
        return point


def _clean(text):
    """Drop bracketed notes and stray punctuation: 'KOCHI (COCHIN)' -> 'KOCHI'."""
    text = re.sub(r"\([^)]*\)", " ", text or "")
    # The portal glues address lines together, often repeating the town:
    # 'AMINGAONAMINGAON' -> 'AMINGAON'
    text = re.sub(r"\b([A-Za-z]{3,})\1\b", r"\1", text)
    text = re.sub(r"[\s,.;:-]+$", "", text)
    return " ".join(text.split())


def _localities(address):
    """Address parts likely to be a place name, last part first."""
    parts = [p.strip(" .") for p in address.split(",")]
    return [p for p in reversed(parts) if len(p) >= 3 and not re.search(r"\d", p)]


def _km(a, b):
    """Great-circle distance between two [lat, lng] points."""
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def geocode(address, office, est_id=""):
    """Return {"lat", "lng", "precision"} or None if nothing matched."""
    address, office = _clean(address), _clean(office)
    state = STATE_BY_PREFIX.get((est_id or "")[:2].upper())
    office_point = _lookup(f"{office}, India") if office else None
    tries = []
    if address:
        if office:
            tries.append((f"{address}, {office}, India", "address"))
        tries.append((f"{address}, India", "address"))
        for part in _localities(address)[:3]:
            if office:
                tries.append((f"{part}, {office}, India", "locality"))
            tries.append((f"{part}, {state}, India" if state else f"{part}, India", "locality"))
    for query, precision in tries:
        point = _lookup(query)
        # Short names match places all over India; an establishment is
        # registered with a nearby EPFO office, so drop far-away hits.
        if point and (not office_point or _km(point, office_point) <= MAX_KM_FROM_OFFICE):
            return {"lat": point[0], "lng": point[1], "precision": precision}
    if office_point:
        return {"lat": office_point[0], "lng": office_point[1], "precision": "office"}
    point = _lookup(f"{state}, India") if state else None
    return {"lat": point[0], "lng": point[1], "precision": "state"} if point else None
