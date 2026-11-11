"""Checks one-way Delhi -> Paris fares (max 1 stop) for every travel date once.
GitHub runs this every hour; results go into the data/ folder."""
import csv
import json
import os
import random
import time
from datetime import date, datetime, timedelta, timezone

from fast_flights import FlightQuery, FlightsNotFound, Passengers, create_query, get_flights

# ---- settings you may change ----
ORIGIN = "DEL"
DESTINATIONS = ["CDG", "ORY"]          # Paris Charles de Gaulle, Paris Orly
START_DATE = date(2027, 3, 15)
END_DATE = date(2027, 4, 2)
MAX_STOPS = 1
SEAT = "economy"
ADULTS = 1
CURRENCY = "INR"
# ---------------------------------

HISTORY = "data/history.csv"
LATEST = "data/latest.json"
FIELDS = ["scraped_at", "travel_date", "min_all", "min_nonstop", "min_cdg", "min_ory",
          "best_airline", "best_route", "n_options"]


def _dt(sd):
    (y, m, d), (hh, mm) = sd.date, sd.time
    return datetime(y, m, d, hh, mm)


def fetch(travel_date, dest):
    q = create_query(
        flights=[FlightQuery(date=travel_date.isoformat(), from_airport=ORIGIN, to_airport=dest)],
        seat=SEAT, trip="one-way", passengers=Passengers(adults=ADULTS),
        currency=CURRENCY, language="en-GB", max_stops=MAX_STOPS,
        hide_separate_and_self_transfer=True,
    )
    try:
        results = get_flights(q)
    except FlightsNotFound:
        return []
    out = []
    for it in results:
        legs = it.flights
        if not legs or not it.price or len(legs) - 1 > MAX_STOPS:
            continue
        first, last = legs[0], legs[-1]
        via, layover = None, None
        if len(legs) == 2:
            via = first.to_airport.code
            layover = int((_dt(last.departure) - _dt(first.arrival)).total_seconds() // 60)
        arr = _dt(last.arrival)
        out.append({
            "price": int(it.price),
            "airline": " / ".join(it.airlines or []).replace(",", " "),
            "to": dest,
            "stops": len(legs) - 1,
            "via": via,
            "layover_min": layover,
            "departs": _dt(first.departure).strftime("%H:%M"),
            "arrives": arr.strftime("%H:%M"),
            "next_day": arr.date() != travel_date,
            "journey_min": sum(l.duration or 0 for l in legs) + (layover or 0),
        })
    return out


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
                    rows = fetch(td, dest)
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
            w.writerow({
                "scraped_at": stamp, "travel_date": td,
                "min_all": best["price"],
                "min_nonstop": mins(lambda r: r["stops"] == 0),
                "min_cdg": mins(lambda r: r["to"] == "CDG"),
                "min_ory": mins(lambda r: r["to"] == "ORY"),
                "best_airline": best["airline"],
                "best_route": "Non-stop" if best["stops"] == 0 else f"via {best['via']}",
                "n_options": len(rows),
            })

    # keep the 25 cheapest distinct options per date for the dashboard table
    trimmed = {}
    for td, rows in options.items():
        seen, keep = set(), []
        for r in sorted(rows, key=lambda r: r["price"]):
            k = (r["airline"], r["departs"], r["via"], r["to"])
            if k not in seen:
                seen.add(k)
                keep.append(r)
        trimmed[td] = keep[:25]

    with open(LATEST, "w") as fh:
        json.dump({"scraped_at": stamp, "searches_ok": ok, "searches_failed": failed,
                   "currency": CURRENCY, "options": trimmed}, fh, indent=1)
    print(f"Done: {ok} searches ok, {failed} failed")


if __name__ == "__main__":
    main()
