"""Build data/in_places.tsv.gz from the GeoNames India dump.

GeoNames (https://www.geonames.org, CC BY 4.0) lists Indian towns,
villages, localities and districts with coordinates. epfo_geocode.py uses
the compact file this script writes to place establishments instantly,
without calling an online geocoder.

Usage:
    curl -o build/IN.zip https://download.geonames.org/export/dump/IN.zip
    python tools/build_places.py build/IN.zip

Output rows (tab separated): key, lat, lng, population, admin1 code
where key is the place name in capitals with only letters and digits.
Districts get population -1 so a town of the same name always wins.
Alternate English-alphabet names (Cochin for Kochi, Allahabad for
Prayagraj) get their own rows.
"""

import gzip
import io
import os
import re
import sys
import zipfile

# P = cities, towns, villages, localities; A = districts and sub-districts
KEEP = {"P"}
KEEP_A = {"ADM2", "ADM3"}
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "in_places.tsv.gz")


def key(name):
    return re.sub(r"[^A-Z0-9]", "", name.upper())


def main(zip_path):
    rows = set()
    with zipfile.ZipFile(zip_path) as z, z.open("IN.txt") as f:
        for line in io.TextIOWrapper(f, encoding="utf-8"):
            c = line.rstrip("\n").split("\t")
            # 1 name, 2 asciiname, 3 alternatenames, 4 lat, 5 lng,
            # 6 feature class, 7 feature code, 10 admin1, 14 population
            if c[6] not in KEEP and c[7] not in KEEP_A:
                continue
            lat, lng = round(float(c[4]), 4), round(float(c[5]), 4)
            # A district's centre is a worse pin than the town of the same
            # name, so districts rank below every town (-1).
            pop = int(c[14] or 0) if c[6] == "P" else -1
            names = {c[1], c[2]}
            names.update(n for n in c[3].split(",") if n and n.isascii())
            for n in names:
                k = key(n)
                if len(k) >= 3:
                    rows.add((k, lat, lng, pop, c[10]))
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8", compresslevel=9) as out:
        for r in sorted(rows):
            out.write("%s\t%s\t%s\t%d\t%s\n" % r)
    print(f"{len(rows)} rows -> {os.path.normpath(OUT)} ({os.path.getsize(OUT) // 1024} KB)")


if __name__ == "__main__":
    main(sys.argv[1])
