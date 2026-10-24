"""EPFO establishment lookup.

Search by establishment code (7 digits) or part of the name on the EPFO
public portal. The captcha image is shown to you and you type it in; the
script then fetches the result list and the details of the establishment
you pick.

Usage:  python epfo_lookup.py
Needs:  pip install requests beautifulsoup4 pillow
"""

import io
import json
import os
import re
import sys

import requests
from bs4 import BeautifulSoup
from PIL import Image

BASE = "https://unifiedportal-emp.epfindia.gov.in"
HOME = BASE + "/publicPortal/no-auth/misReport/home/loadEstSearchHome"


class EpfoClient:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36",
            "Referer": HOME,
            "Origin": BASE,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self.search_url = None
        self.captcha_url = None
        self.section_titles = {}

    def load_home(self):
        """Load the search page: sets the session cookie and gives fresh
        _HDIV_STATE_ tokens for the search and captcha URLs."""
        html = self.s.get(HOME, timeout=30).text
        self._pin_backend()
        m = re.search(r"fnSearchEstb\('([^']+)'", html)
        if not m:
            raise RuntimeError("Search URL not found on page; site layout may have changed.")
        self.search_url = BASE + m.group(1)

        soup = BeautifulSoup(html, "html.parser")
        img = soup.find("img", id="capImg")
        if not img:
            raise RuntimeError("Captcha image not found on page.")
        self.captcha_url = BASE + img["src"]

        # Section titles for the detail panels: div id="dvXxx_N" -> heading text
        for div in soup.find_all("div", id=re.compile(r"^dv\w+_\d+$")):
            n = int(div["id"].rsplit("_", 1)[1])
            h = div.find(class_="panel-title")
            if h:
                self.section_titles[n] = " ".join(h.get_text(" ", strip=True).split())

    def _pin_backend(self):
        """The portal's load balancer picks a backend from the EMP cookie, but
        the server sets it only for the folder of the page that was loaded.
        Requests to other folders (like the search URL) would then land on a
        random backend that doesn't know our session and answer "Unexpected
        error occurred". Re-set EMP for the whole site so every request goes
        to the backend that holds our session."""
        emp = None
        for c in self.s.cookies:
            if c.name == "EMP" and c.path.startswith("/publicPortal/no-auth/misReport"):
                emp = c.value
        if not emp:
            return
        for c in [c for c in self.s.cookies if c.name == "EMP"]:
            self.s.cookies.clear(c.domain, c.path, c.name)
        self.s.cookies.set("EMP", emp, domain="unifiedportal-emp.epfindia.gov.in", path="/")

    def show_captcha(self):
        img = self.s.get(self.captcha_url, timeout=30).content
        Image.open(io.BytesIO(img)).show()

    def _post(self, url, obj):
        """POST JSON exactly as the site's jQuery code does."""
        return self.s.post(
            url,
            data=json.dumps(obj, separators=(",", ":")).encode("utf-8"),
            timeout=60,
            headers={
                "Content-Type": "application/json; charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
            },
        )

    def search(self, est_name="", est_code="", captcha=""):
        if est_code:
            est_code = est_code.strip().zfill(7)
        payload = {"EstName": est_name.strip(), "EstCode": est_code, "captcha": captcha.strip()}
        r = self._post(self.search_url, payload)
        # Keep the last raw reply for troubleshooting
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_search_response.html")
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(f"<!-- status {r.status_code} -->\n{r.text}")
        r.raise_for_status()
        return parse_results(r.text)

    def details(self, est_id, urls):
        """Fetch every detail panel. urls[i] belongs to panel number i + 3."""
        out = []
        for i, url in enumerate(urls):
            if not url:
                continue
            n = i + 3
            title = self.section_titles.get(n, f"Section {n}")
            try:
                r = self._post(BASE + url, {"EstId": est_id})
                r.raise_for_status()
                out.append((title, parse_tables(r.text)))
                # The Validity Status panel carries the "View Payment Details" link
                m = re.search(r"fnViewPaymentDtls\('([^']+)'", r.text)
                if m:
                    out.append(("Payment Details", self.payment_details(m.group(1))))
            except requests.RequestException as e:
                out.append((title, [[f"Error: {e}"]]))
        return out

    def payment_details(self, url):
        """Monthly ECR payments: TRRN, date of credit, amount, wage month,
        number of employees, ECR filed. The site opens this as a pop-up page."""
        try:
            r = self.s.get(BASE + url, timeout=60)
            r.raise_for_status()
        except requests.RequestException as e:
            return [[f"Error: {e}"]]
        soup = BeautifulSoup(r.text, "html.parser")
        table = soup.find("table")
        if table:
            return parse_tables(str(table))
        m = re.search(r"No Payment details found[^<.]*", r.text)
        return [[m.group(0) + "." if m else "No payment details returned."]]


def parse_tables(html):
    """Return all table rows in html as lists of cell text."""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for tr in soup.find_all("tr"):
        cells = [" ".join(c.get_text(" ", strip=True).split()) for c in tr.find_all(["th", "td"])]
        if any(cells):
            rows.append(cells)
    if not rows:
        text = " ".join(soup.get_text(" ", strip=True).split())
        if text:
            rows.append([text])
    return rows


def parse_results(html):
    """Return (header, results). Each result is a dict with cells, est_id and
    detail URLs taken from the row's fnViewDetails(...) link."""
    soup = BeautifulSoup(html, "html.parser")
    header, results = [], []
    for tr in soup.find_all("tr"):
        ths = tr.find_all("th")
        if ths and not header:
            header = [" ".join(th.get_text(" ", strip=True).split()) for th in ths]
            continue
        tds = tr.find_all("td")
        if not tds:
            continue
        cells = [" ".join(td.get_text(" ", strip=True).split()) for td in tds]
        # Link looks like: <a name="ORBBS0005059000"
        #   onclick="fnViewDetails(this.name,'<url3>','<url4>',...,'<url12>')">
        est_id, urls = None, []
        for a in tr.find_all("a"):
            m = re.search(r"fnViewDetails\(([^)]*)\)", a.get("onclick") or "")
            if m:
                est_id = a.get("name")
                urls = re.findall(r"'([^']*)'", m.group(1))
                break
        results.append({"cells": cells, "est_id": est_id, "urls": urls})
    if not results:
        # No table: probably a message such as "invalid captcha" or "no record found"
        msg = " ".join(soup.get_text(" ", strip=True).split())
        return header, msg or "Empty response."
    return header, results


def print_rows(rows):
    for row in rows:
        print("  " + " | ".join(row))


def main():
    client = EpfoClient()
    while True:
        print("\n=== EPFO Establishment Search ===")
        q = input("Establishment code (7 digits) or name (blank to quit): ").strip()
        if not q:
            return
        is_code = q.isdigit()
        if not is_code and len(q) < 3:
            print("Enter at least 3 characters of the name.")
            continue

        try:
            client.load_home()
            client.show_captcha()
        except (requests.RequestException, RuntimeError) as e:
            print("Could not load the EPFO page:", e)
            continue

        cap = input("Type the captcha shown in the image: ").strip()
        try:
            header, results = client.search(est_code=q if is_code else "",
                                            est_name="" if is_code else q,
                                            captcha=cap)
        except requests.RequestException as e:
            print("Search failed:", e)
            continue

        if isinstance(results, str):
            print("Site says:", results[:500])
            continue

        print(f"\nFound {len(results)} result(s):")
        if header:
            print("     " + " | ".join(header))
        for i, r in enumerate(results, 1):
            print(f"  {i:>2}. " + " | ".join(r["cells"]))

        viewable = [r for r in results if r["est_id"]]
        if not viewable:
            continue
        pick = input("\nNumber to view details (blank to skip): ").strip()
        if not pick.isdigit() or not 1 <= int(pick) <= len(results):
            continue
        chosen = results[int(pick) - 1]
        if not chosen["est_id"]:
            print("No details link for that row.")
            continue

        print(f"\nDetails for {chosen['est_id']}:")
        for title, rows in client.details(chosen["est_id"], chosen["urls"]):
            print(f"\n--- {title} ---")
            print_rows(rows)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
