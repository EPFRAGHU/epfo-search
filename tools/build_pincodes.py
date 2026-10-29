"""Build data/in_pincodes.tsv.gz from the GeoNames India postal code dump.

GeoNames (https://www.geonames.org, CC BY 4.0) lists every Indian pincode's
post offices with coordinates. A pincode has several post offices and a few
of their points are off, so each pincode gets the median of its offices.

Usage:
    curl -o build/IN_postal.zip https://download.geonames.org/export/zip/IN.zip
    python tools/build_pincodes.py build/IN_postal.zip

Output rows (tab separated): pincode, lat, lng, place name, district, state,
point reliable (1/0)
"""

import gzip
import io
import os
import statistics
import sys
import zipfile
from collections import defaultdict

PLACEHOLDER_PINS = 4  # a point shared by this many pincodes is a placeholder
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "in_pincodes.tsv.gz")


def main(zip_path):
    offices = defaultdict(list)
    with zipfile.ZipFile(zip_path) as z, z.open("IN.txt") as f:
        for line in io.TextIOWrapper(f, encoding="utf-8"):
            # 1 pincode, 2 post office, 3 state, 5 district, 9 lat, 10 lng
            c = line.rstrip("\n").split("\t")
            if c[9] and c[10]:
                offices[c[1]].append((float(c[9]), float(c[10]), c[2], c[5], c[3]))
    # Many offices have only an "estimated" point that is really a district
    # placeholder: the very same coordinates for many different pincodes
    # (e.g. 20.09, 85.5114 for much of Bhubaneswar). Drop those points; a
    # pincode left with none gets no entry rather than a wrong one.
    pins_at = defaultdict(set)
    for pin, rows in offices.items():
        for r in rows:
            pins_at[(r[0], r[1])].add(pin)
    placeholder = {pt for pt, pins in pins_at.items() if len(pins) >= PLACEHOLDER_PINS}
    unreliable = 0

    with gzip.open(OUT, "wt", encoding="utf-8", compresslevel=9) as out:
        for pin in sorted(offices):
            # Names (place, district, state) are kept for every pincode; the
            # point only from offices with a real one, flagged 1, else 0.
            good = [r for r in offices[pin] if (r[0], r[1]) not in placeholder]
            rows = good or offices[pin]
            unreliable += not good
            lat = statistics.median(r[0] for r in rows)
            lng = statistics.median(r[1] for r in rows)
            # Name the pincode after its head/sub post office when it has one
            # (any office: a reliable point does not make a better name)
            main_office = next((r for r in offices[pin] if r[2].endswith((" H.O", " S.O"))), offices[pin][0])
            name = main_office[2].removesuffix(" H.O").removesuffix(" S.O")
            out.write(f"{pin}\t{lat:.4f}\t{lng:.4f}\t{name}\t{main_office[3]}\t{main_office[4]}\t{int(bool(good))}\n")
    print(f"{len(placeholder)} placeholder points; {unreliable} pincodes without a reliable point (flag 0)")
    print(f"{len(offices)} pincodes -> {os.path.normpath(OUT)} ({os.path.getsize(OUT) // 1024} KB)")


if __name__ == "__main__":
    main(sys.argv[1])
