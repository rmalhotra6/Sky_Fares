"""Checks one-way Delhi -> Paris fares (max 1 stop) for every travel date once,
then opens Google Flights' booking page for the cheapest flights on each date to
read the prices that booking sites (MakeMyTrip, Goibibo, EaseMyTrip, ixigo,
Cleartrip, airline websites, ...) charge. GitHub runs this every hour."""
import base64
import csv
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

from fast_flights import FlightQuery, Passengers, create_query
from primp import Client
from selectolax.lexbor import LexborHTMLParser

# ---- settings you may change ----
ORIGIN = "DEL"
DESTINATIONS = ["CDG", "ORY"]          # Paris Charles de Gaulle, Paris Orly
START_DATE = date(2027, 3, 15)
END_DATE = date(2027, 4, 2)
MAX_STOPS = 1
SEAT = "economy"
ADULTS = 1
CURRENCY = "INR"
BOOKING_CHECKS_PER_DATE = 3            # cheapest N flights per date get booking-site prices
# ---------------------------------

WATCHED_SITES = ["MakeMyTrip", "Goibibo", "EaseMyTrip", "ixigo", "Cleartrip", "Agoda", "Skyscanner"]
OTHER_SITES = ["Yatra", "Trip.com", "Gotogate", "Mytrip", "Kiwi.com", "Booking.com", "Expedia", "eDreams",
               "Opodo", "Travelgenio", "Wego", "FlightsMojo", "Travomint", "Akbar Travels", "Musafir", "Via.com", "Happyfares", "Paytm", "Budgetair"]
HISTORY = "data/history.csv"
LATEST = "data/latest.json"
DEBUG_DIR = "data/debug"
FIELDS = ["scraped_at", "travel_date", "min_all", "min_nonstop", "min_cdg", "min_ory",
          "best_airline", "best_route", "n_options", "best_site_price", "best_site"]
SEAT_CODE = {"economy": 1, "premium-economy": 2, "business": 3, "first": 4}[SEAT]


# ---------------- Google Flights search (plain web request) ----------------

def _get(lst, *idx):
    for i in idx:
        if not isinstance(lst, list) or i >= len(lst):
            return None
        lst = lst[i]
    return lst


def google_search(travel_date, dest):
    """Return itineraries from BOTH 'Top flights' (section 2) and 'Other flights' (section 3)."""
    q = create_query(
        flights=[FlightQuery(date=travel_date.isoformat(), from_airport=ORIGIN, to_airport=dest)],
        seat=SEAT, trip="one-way", passengers=Passengers(adults=ADULTS),
        currency=CURRENCY, language="en-GB", max_stops=MAX_STOPS,
        hide_separate_and_self_transfer=True,
    )
    params = q.params()
    params["gl"] = "IN"  # prices as sold in India
    client = Client(impersonate="chrome_145", impersonate_os="macos", referer=True, cookie_store=True)
    html = client.get("https://www.google.com/travel/flights", params=params).text
    script = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if script is None:
        raise RuntimeError("Google returned a page without flight data (possibly blocked)")
    data = script.text().split("data:", 1)[1].rsplit(",", 1)[0]
    if data.endswith("errorHasStatus: true"):
        return []
    payload = json.loads(data)

    itins = []
    for section in (2, 3):
        for k in _get(payload, section, 0) or []:
            flight, price = _get(k, 0), _get(k, 1, 0, 1)
            if not flight or not price:
                continue
            legs = []
            for L in _get(flight, 2) or []:
                dep_t, arr_t = (_get(L, 8) or []) + [None, None], (_get(L, 10) or []) + [None, None]
                fn = _get(L, 22) or []
                legs.append({
                    "from": _get(L, 3), "to": _get(L, 6),
                    "dep": datetime(*_get(L, 20), dep_t[0] or 0, dep_t[1] or 0),
                    "arr": datetime(*_get(L, 21), arr_t[0] or 0, arr_t[1] or 0),
                    "dur": _get(L, 11) or 0,
                    "carrier": fn[0] if len(fn) > 0 else None,
                    "number": fn[1] if len(fn) > 1 else None,
                })
            if legs:
                itins.append({"price": int(price), "airlines": _get(flight, 1) or [], "legs": legs})
    return itins


def summarise(travel_date, dest, itins):
    out, seen = [], set()
    for it in itins:
        legs = it["legs"]
        key = tuple((l["carrier"], l["number"], l["dep"]) for l in legs) + (it["price"],)
        if key in seen or len(legs) - 1 > MAX_STOPS:
            continue
        seen.add(key)
        first, last = legs[0], legs[-1]
        via = layover = None
        if len(legs) == 2:
            via = first["to"]
            layover = int((last["dep"] - first["arr"]).total_seconds() // 60)
        out.append({
            "price": it["price"],
            "airline": " / ".join(it["airlines"]).replace(",", " "),
            "to": dest, "stops": len(legs) - 1, "via": via, "layover_min": layover,
            "departs": first["dep"].strftime("%H:%M"), "arrives": last["arr"].strftime("%H:%M"),
            "next_day": last["arr"].date() != travel_date,
            "journey_min": sum(l["dur"] for l in legs) + (layover or 0),
            "flight_nos": " + ".join(f"{l['carrier']} {l['number']}" for l in legs if l["carrier"]),
            "_legs": [(l["from"], l["dep"].date().isoformat(), l["to"], l["carrier"], l["number"]) for l in legs],
        })
    return out


# ---------------- Booking-site prices (real headless browser) ----------------

def _varint(v):
    out = bytearray()
    while True:
        b, v = v & 0x7F, v >> 7
        out.append(b | 0x80 if v else b)
        if not v:
            return bytes(out)


def _ld(field, payload):
    return _varint(field << 3 | 2) + _varint(len(payload)) + payload


def _vi(field, value):
    return _varint(field << 3) + _varint(value)


def booking_url(legs):
    """Google Flights booking deep link for one exact itinerary (format from the open-source 'fli' project)."""
    body = _ld(2, legs[0][1].encode())
    for frm, d, to, carrier, number in legs:
        body += _ld(4, _ld(1, frm.encode()) + _ld(2, d.encode()) + _ld(3, to.encode())
                    + _ld(5, carrier.encode()) + _ld(6, str(number).encode()))
    body += _ld(13, _vi(1, 1) + _ld(2, legs[0][0].encode()))
    body += _ld(14, _vi(1, 1) + _ld(2, legs[-1][2].encode()))
    payload = _vi(1, 28) + _vi(2, 2) + _ld(3, body)
    for _ in range(ADULTS):
        payload += _vi(8, 1)
    payload += _vi(9, SEAT_CODE) + _vi(14, 1) + _ld(16, _vi(1, (1 << 64) - 1)) + _vi(19, 2)
    tfs = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return "https://www.google.com/travel/flights/booking?" + urlencode(
        {"tfs": tfs, "hl": "en-GB", "curr": CURRENCY, "gl": "IN"})


PRICE_RE = re.compile(r"(?:₹|INR|Rs\.?)\s?([\d,]{4,})")


def parse_booking_text(text, airline_names):
    """Pull (site, price) pairs out of the booking page's visible text."""
    known = WATCHED_SITES + OTHER_SITES + [a for a in airline_names if a]
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    found = {}
    for i, line in enumerate(lines):
        name = None
        m = re.match(r"(?i)book with (.+)", line)
        if m:
            name = m.group(1).strip()
        else:
            for k in known:
                if line.lower() == k.lower() or line.lower().startswith(k.lower() + " "):
                    name = k
                    break
        if not name or len(name) > 60:
            continue
        is_airline = bool(re.search(r"\s*Airline$", name)) or (i + 1 < len(lines) and lines[i + 1] == "Airline")
        name = re.sub(r"\s*Airline$", "", name).strip()
        name = next((k for k in known if k.lower() == name.lower()), name[:1].upper() + name[1:])
        if is_airline:
            name += " (airline)"
        for nxt in lines[i:i + 7]:
            if nxt is not line and re.match(r"(?i)book with ", nxt):
                break
            pm = PRICE_RE.search(nxt)
            if pm:
                price = int(pm.group(1).replace(",", ""))
                if 3000 <= price <= 1000000:
                    found[name] = min(price, found.get(name, price))
                break
    return sorted(({"name": n, "price": p} for n, p in found.items()), key=lambda s: s["price"])


def ensure_browser():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        subprocess.run([sys.executable, "-m", "pip", "install", "playwright==1.48.0"], check=True)
    subprocess.run([sys.executable, "-m", "playwright", "install", "--with-deps", "chromium"], check=True)


def check_booking_sites(targets):
    """targets: list of option dicts (mutated in place with 'sites')."""
    if not targets:
        return "skipped"
    try:
        ensure_browser()
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"Browser setup failed: {e}")
        return f"browser setup failed: {e}"

    os.makedirs(DEBUG_DIR, exist_ok=True)
    ok, pages_log, sample_saved = 0, [], False
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            locale="en-GB", timezone_id="Asia/Kolkata", viewport={"width": 1280, "height": 2000},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36")
        page = ctx.new_page()
        for n, opt in enumerate(targets):
            url = booking_url(opt["_legs"])
            try:
                for attempt in range(3):
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                    try:
                        page.get_by_text(re.compile(r"Book with|something went wrong", re.I)).first.wait_for(timeout=25000)
                    except Exception:
                        pass
                    if "something went wrong" not in page.inner_text("body").lower():
                        break
                    page.wait_for_timeout(4000 * (attempt + 1))
                for label in ("Accept all", "Reject all"):           # cookie consent, if shown
                    btn = page.get_by_role("button", name=label)
                    if btn.count():
                        btn.first.click()
                        page.wait_for_load_state("domcontentloaded")
                        break
                try:
                    page.get_by_text(re.compile(r"Book with", re.I)).first.wait_for(timeout=25000)
                except Exception:
                    pass
                more = page.get_by_role("button", name=re.compile(r"more booking options", re.I))
                if more.count():
                    more.first.click()
                    page.wait_for_timeout(1500)
                text = page.inner_text("body")
                opt["sites"] = parse_booking_text(text, opt["airline"].split(" / "))
                ok += bool(opt["sites"])
                print(f"  booking {n + 1}/{len(targets)} {opt['flight_nos']}: "
                      + (", ".join(f"{s['name']} {s['price']}" for s in opt["sites"][:5]) or "no site prices found"))
                pages_log.append(f"===== {opt['flight_nos']} | {url}\n{text[:4000]}\n")
                if not opt["sites"] and not sample_saved:  # first page without prices: keep a screenshot
                    sample_saved = True
                    page.screenshot(path=f"{DEBUG_DIR}/booking_problem.png", full_page=False)
                if n == 0:  # keep one sample so problems can be diagnosed from the repository
                    with open(f"{DEBUG_DIR}/booking_sample.txt", "w") as fh:
                        fh.write(url + "\n\n" + text[:15000])
                    page.screenshot(path=f"{DEBUG_DIR}/booking_sample.png", full_page=False)
            except Exception as e:
                print(f"  booking {n + 1}/{len(targets)} failed: {type(e).__name__}: {e}")
            time.sleep(random.uniform(2, 5))
        browser.close()
    with open(f"{DEBUG_DIR}/booking_pages.txt", "w") as fh:
        fh.write("\n".join(pages_log))
    return f"{ok} of {len(targets)} flights had booking-site prices"


# ---------------- main ----------------

def migrate_history():
    if not os.path.exists(HISTORY):
        return
    with open(HISTORY) as fh:
        rows = list(csv.DictReader(fh))
        fh.seek(0)
        header = fh.readline().strip().split(",")
    if header == FIELDS:
        return
    with open(HISTORY, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def main():
    os.makedirs("data", exist_ok=True)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    stamp = now.isoformat().replace("+00:00", "Z")
    if now.date() > END_DATE:
        print("Travel window has passed; nothing to do.")
        return

    dates, d = [], max(START_DATE, now.date())
    while d <= END_DATE:
        dates.append(d)
        d += timedelta(days=1)

    options, ok, failed = {}, 0, 0
    for td in dates:
        for dest in DESTINATIONS:
            for attempt in range(3):
                try:
                    rows = summarise(td, dest, google_search(td, dest))
                    ok += 1
                    options.setdefault(td.isoformat(), []).extend(rows)
                    print(f"{td} {dest}: {len(rows)} options")
                    break
                except Exception as e:
                    print(f"{td} {dest}: attempt {attempt + 1} failed: {type(e).__name__}: {e}")
                    time.sleep(15 * (attempt + 1))
            else:
                failed += 1
            time.sleep(random.uniform(3, 7))

    # booking-site prices for the cheapest flights on each date
    targets = []
    for td, rows in options.items():
        rows.sort(key=lambda r: r["price"])
        targets += [r for r in rows if all(l[3] and l[4] for l in r["_legs"])][:BOOKING_CHECKS_PER_DATE]
    print(f"Checking booking sites for {len(targets)} flights")
    booking_status = check_booking_sites(targets)

    migrate_history()
    new_file = not os.path.exists(HISTORY)
    with open(HISTORY, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        if new_file:
            w.writeheader()
        for td, rows in sorted(options.items()):
            if not rows:
                continue
            best = min(rows, key=lambda r: r["price"])
            mins = lambda pred: min((r["price"] for r in rows if pred(r)), default="")
            site_best = min((s for r in rows for s in r.get("sites", [])), key=lambda s: s["price"], default=None)
            w.writerow({
                "scraped_at": stamp, "travel_date": td,
                "min_all": best["price"],
                "min_nonstop": mins(lambda r: r["stops"] == 0),
                "min_cdg": mins(lambda r: r["to"] == "CDG"),
                "min_ory": mins(lambda r: r["to"] == "ORY"),
                "best_airline": best["airline"],
                "best_route": "Non-stop" if best["stops"] == 0 else f"via {best['via']}",
                "n_options": len(rows),
                "best_site_price": site_best["price"] if site_best else "",
                "best_site": site_best["name"].replace(",", " ") if site_best else "",
            })

    # cheapest fare per booking site across all dates
    by_site = {}
    for td, rows in options.items():
        for r in rows:
            for s in r.get("sites", []):
                cur = by_site.get(s["name"])
                if cur is None or s["price"] < cur["price"]:
                    by_site[s["name"]] = {"price": s["price"], "date": td, "airline": r["airline"],
                                          "route": "Non-stop" if r["stops"] == 0 else f"via {r['via']}",
                                          "departs": r["departs"]}

    trimmed = {}
    for td, rows in options.items():
        seen, keep = set(), []
        for r in sorted(rows, key=lambda r: r["price"]):
            k = (r["airline"], r["departs"], r["via"], r["to"])
            if k not in seen:
                seen.add(k)
                keep.append({k2: v for k2, v in r.items() if not k2.startswith("_")})
        trimmed[td] = keep[:25]

    with open(LATEST, "w") as fh:
        json.dump({"scraped_at": stamp, "searches_ok": ok, "searches_failed": failed,
                   "booking_status": booking_status, "watched_sites": WATCHED_SITES,
                   "currency": CURRENCY, "by_site": by_site, "options": trimmed}, fh, indent=1)
    print(f"Done: {ok} searches ok, {failed} failed; booking sites: {booking_status}")


if __name__ == "__main__":
    main()
