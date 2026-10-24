"""Build data/in_pincodes.tsv.gz from the GeoNames India postal code dump.

GeoNames (https://www.geonames.org, CC BY 4.0) lists every Indian pincode's
post offices with coordinates. A pincode has several post offices and a few
of their points are off, so each pincode gets the median of its offices.

Usage:
    curl -o build/IN_postal.zip https://download.geonames.org/export/zip/IN.zip
    python tools/build_pincodes.py build/IN_postal.zip

Output rows (tab separated): pincode, lat, lng, place name, district
"""

import gzip
import io
import os
import statistics
import sys
import zipfile
from collections import defaultdict

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "in_pincodes.tsv.gz")


def main(zip_path):
    offices = defaultdict(list)
    with zipfile.ZipFile(zip_path) as z, z.open("IN.txt") as f:
        for line in io.TextIOWrapper(f, encoding="utf-8"):
            # 1 pincode, 2 post office, 5 district, 9 lat, 10 lng
            c = line.rstrip("\n").split("\t")
            if c[9] and c[10]:
                offices[c[1]].append((float(c[9]), float(c[10]), c[2], c[5]))
    with gzip.open(OUT, "wt", encoding="utf-8", compresslevel=9) as out:
        for pin in sorted(offices):
            rows = offices[pin]
            lat = statistics.median(r[0] for r in rows)
            lng = statistics.median(r[1] for r in rows)
            # Name the pincode after its head/sub post office when it has one
            main_office = next((r for r in rows if r[2].endswith((" H.O", " S.O"))), rows[0])
            name = main_office[2].removesuffix(" H.O").removesuffix(" S.O")
            out.write(f"{pin}\t{lat:.4f}\t{lng:.4f}\t{name}\t{main_office[3]}\n")
    print(f"{len(offices)} pincodes -> {os.path.normpath(OUT)} ({os.path.getsize(OUT) // 1024} KB)")


if __name__ == "__main__":
    main(sys.argv[1])
