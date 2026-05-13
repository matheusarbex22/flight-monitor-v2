#!/usr/bin/env python3
"""
Paris to Sao Paulo Flight Price Monitor
Checks Google Flights via SerpAPI, sends alerts via SendGrid HTTP API
(works behind corporate firewalls -- pure HTTPS port 443, no SMTP).

SETUP
=====
1. Install dependency (run once):
       C:\\...\\python.exe -m pip install requests

2. SendGrid free account (replaces Gmail SMTP -- works on corporate networks):
       a. Sign up at https://sendgrid.com  (free, 100 emails/day, no credit card)
       b. Settings > API Keys > Create API Key > Full Access > copy the key (starts with SG.)
       c. Settings > Sender Authentication > Single Sender Verification
          > verify your email address (e.g. matheus.arbex@gmail.com)

3. Update your .env file to look like this:
       SERPAPI_KEY=your_serpapi_key
       ALERT_EMAIL=matheusfa@al.insper.edu.br
       SENDGRID_API_KEY=SG.xxxxxxxxxxxxxxxx
       SENDGRID_FROM=matheus.arbex@gmail.com

4. Run:   python flight_monitor.py

5. Schedule every 6 hours (Windows Task Scheduler):
       Action:  C:\\...\\python.exe C:\\Users\\arbexma\\Downloads\\flight_monitor.py
       Trigger: Daily, repeat every 6 hours indefinitely
"""

import os
import sys
import json
import logging
import requests
import urllib3
from datetime import datetime
from pathlib import Path

# Fix Windows cp1252 encoding issue
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# Suppress SSL warnings from corporate proxy
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# ── Load .env from same folder as this script ────────────────────────────────
def load_dotenv():
    candidates = [Path(__file__).parent / ".env", Path(".env")]
    for env_path in candidates:
        if env_path.exists():
            with open(env_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, value = line.partition("=")
                        os.environ.setdefault(key.strip(), value.strip())
            print(f"Loaded .env from: {env_path.resolve()}")
            return
    print("WARNING: No .env file found. Checked:", [str(c) for c in candidates])

load_dotenv()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONFIGURE YOUR SEARCH HERE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONFIG = {
    "origin":          "CDG",
    "destination":     "GRU",
    "depart_date":     "2026-12-18",
    "return_dates":    ["2027-01-07", "2027-01-08", "2027-01-09", "2027-01-10"],

    "adults":          1,

    # Alert fires when cheapest price drops BELOW this (BRL)
    "price_threshold": 8500,

    # Filters
    "max_stops":       1,       # max number of stopovers (1 = max 1 scale)
    "max_duration_h":  18,      # max total flight duration in hours

    # Top N results shown per return date in the alert email
    "results_to_show": 3,

    # Local file that tracks price history over time
    "history_file":    "flight_price_history.json",

    # Loaded from .env
    "serpapi_key":     os.getenv("SERPAPI_KEY", ""),
    "sendgrid_key":    os.getenv("SENDGRID_API_KEY", ""),
    "sendgrid_from":   os.getenv("SENDGRID_FROM", ""),
    "alert_email":     os.getenv("ALERT_EMAIL", ""),
}
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("flight_monitor.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# Airline name fragments → their booking search URL templates
# {dep}, {dest}, {out}, {ret} are filled in per flight
# Dates format: out/ret = YYYY-MM-DD
AIRLINE_LINKS = {
    "air france":       "https://www.airfrance.com/search/offers?pax=1ADT&cabinClass=ECONOMY&lang=en&tripType=ROUND_TRIP&outboundDate={out}&inboundDate={ret}&originLocationCode={dep}&destinationLocationCode={dest}",
    "tap":              "https://www.flytap.com/en-us/flight-search?origin={dep}&destination={dest}&departureDate={out}&returnDate={ret}&adults=1&cabin=ECONOMY",
    "iberia":           "https://www.iberia.com/us/flights/results/?lang=en_GB&from={dep}&to={dest}&departure={out_slash}&return={ret_slash}&adults=1&cabin=N",
    "lufthansa":        "https://www.lufthansa.com/us/en/flight-search?origin={dep}&destination={dest}&outboundDate={out}&inboundDate={ret}&adults=1&cabin=N&tripType=ROUND_TRIP",
    "lufthansa city":   "https://www.lufthansa.com/us/en/flight-search?origin={dep}&destination={dest}&outboundDate={out}&inboundDate={ret}&adults=1&cabin=N&tripType=ROUND_TRIP",
    "latam":            "https://www.latamairlines.com/us/en/flight-offers?origin={dep}&inbound={ret}&outbound={out}&destination={dest}&adt=1&chd=0&inf=0&trip=RT&cabin=Y&redemption=false",
    "air europa":       "https://www.aireuropa.com/en/flights/flight-results?origin={dep}&destination={dest}&departure={out}&return={ret}&adults=1&children=0&infants=0&cabin=T",
    "royal air maroc":  "https://www.royalairmaroc.com/us-en/Booking/Search?tripType=RT&origin={dep}&destination={dest}&departureDate={out}&returnDate={ret}&adults=1&children=0&infants=0",
    "ita airways":      "https://www.ita-airways.com/en_us/fly-ita/book-a-flight.html",
    "british airways":  "https://www.britishairways.com/travel/fx/public/en_us?from={dep}&to={dest}&depart={out_ddmmyyyy}&return={ret_ddmmyyyy}&adult=1&cabin=M",
    "swiss":            "https://www.swiss.com/us/en/book/flights#outbound={dep}-{dest}-{out}|inbound={dest}-{dep}-{ret}|1-0-0-0|N",
    "austrian":         "https://www.austrian.com/us/en/flight-search?origin={dep}&destination={dest}&outboundDate={out}&inboundDate={ret}&adults=1&cabin=ECO&tripType=RT",
    "klm":              "https://www.klm.com/search/us/en#outbound={dep}-{dest}-{out}|inbound={dest}-{dep}-{ret}|1-0-0-0|Y",
}


def get_booking_link(flight: dict) -> str:
    """
    Build a direct airline booking URL pre-filled with flight dates.
    Falls back to KAYAK (with exact dates) if airline not mapped.
    """
    airline_raw = flight.get("airline", "").lower()
    dep  = CONFIG["origin"]
    dest = CONFIG["destination"]
    out  = flight.get("return_date", CONFIG["return_dates"][-1])
    # out should be depart_date, ret is the return
    out  = CONFIG["depart_date"]
    ret  = flight.get("return_date", CONFIG["return_dates"][-1])

    # Alternative date formats some airlines need
    out_slash    = out.replace("-", "/")          # YYYY/MM/DD
    ret_slash    = ret.replace("-", "/")
    out_ddmmyyyy = out[8:10] + out[5:7] + out[0:4]   # DDMMYYYY
    ret_ddmmyyyy = ret[8:10] + ret[5:7] + ret[0:4]

    fmt = dict(dep=dep, dest=dest, out=out, ret=ret,
               out_slash=out_slash, ret_slash=ret_slash,
               out_ddmmyyyy=out_ddmmyyyy, ret_ddmmyyyy=ret_ddmmyyyy)

    # Match against known airlines (partial name match)
    for key, template in AIRLINE_LINKS.items():
        if key in airline_raw:
            return template.format(**fmt)

    # Fallback: KAYAK with exact dates pre-filled (better than Google Flights)
    return (f"https://www.kayak.com/flights/{dep}-{dest}/{out}/{ret}"
            f"?sort=price_a")


# ─────────────────────────────────────────────────
# 1.  FETCH PRICES via SerpAPI Google Flights
#     Queries all 4 return dates, returns dict keyed by return date
# ─────────────────────────────────────────────────
def fetch_flights_for_date(return_date: str) -> list:
    """Fetch and filter flights for a single return date."""
    params = {
        "engine":        "google_flights",
        "departure_id":  CONFIG["origin"],
        "arrival_id":    CONFIG["destination"],
        "outbound_date": CONFIG["depart_date"],
        "return_date":   return_date,
        "adults":        CONFIG["adults"],
        "currency":      "BRL",
        "hl":            "en",
        "type":          "1",
        "api_key":       CONFIG["serpapi_key"],
    }

    resp = requests.get(
        "https://serpapi.com/search",
        params=params, timeout=30, verify=False
    )
    resp.raise_for_status()
    data = resp.json()

    max_mins = CONFIG["max_duration_h"] * 60
    flights  = []

    for section in ("best_flights", "other_flights"):
        for item in data.get(section, []):
            price = item.get("price")
            if price is None:
                continue
            legs  = item.get("flights", [])
            stops = len(legs) - 1
            mins  = item.get("total_duration", 0)

            if stops > CONFIG["max_stops"]:
                continue
            if mins > max_mins:
                continue

            airlines      = list({f.get("airline", "Unknown") for f in legs})
            h, m          = divmod(mins, 60)
            layovers      = [lv.get("name", "") for lv in item.get("layovers", [])]
            dep           = legs[0].get("departure_airport", {}) if legs else {}
            arr           = legs[-1].get("arrival_airport", {}) if legs else {}
            booking_token = item.get("booking_token", "")

            flights.append({
                "airline":       ", ".join(sorted(airlines)),
                "price_brl":     price,
                "stops":         stops,
                "duration":      f"{h}h {m}m",
                "layovers":      layovers,
                "departs":       dep.get("time", ""),
                "arrives":       arr.get("time", ""),
                "return_date":   return_date,
                "booking_token": booking_token,
                "booking_link":  "",   # resolved later for top results only
            })

    flights.sort(key=lambda x: x["price_brl"])
    return flights


def fetch_all_dates() -> dict:
    """Query all return dates. Returns {date: [flights]}."""
    if not CONFIG["serpapi_key"]:
        raise ValueError("SERPAPI_KEY not set in .env")

    results = {}
    for return_date in CONFIG["return_dates"]:
        log.info(f"Querying return {return_date} ...")
        try:
            flights = fetch_flights_for_date(return_date)
            results[return_date] = flights
            cheapest = f"BRL {flights[0]['price_brl']:,.0f}" if flights else "no results after filters"
            log.info(f"  -> {len(flights)} flights. Cheapest: {cheapest}")
        except Exception as e:
            log.error(f"  -> fetch failed for {return_date}: {e}")
            results[return_date] = []

    return results


def resolve_booking_links_for_email(results_by_date: dict):
    """Fill in direct airline booking links for top N flights per date."""
    n = CONFIG["results_to_show"]
    for flights in results_by_date.values():
        for f in flights[:n]:
            f["booking_link"] = get_booking_link(f)



# ─────────────────────────────────────────────────
# 2.  PRICE HISTORY
# ─────────────────────────────────────────────────
def load_history() -> list:
    p = Path(CONFIG["history_file"])
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return []

def save_history(history: list, price: float):
    history.append({
        "timestamp":    datetime.now().isoformat(),
        "cheapest_brl": price,
    })
    Path(CONFIG["history_file"]).write_text(
        json.dumps(history, indent=2), encoding="utf-8"
    )

def price_trend(history: list) -> str:
    if len(history) < 2:
        return "Not enough data yet for a price trend."
    prev  = history[-2]["cheapest_brl"]
    curr  = history[-1]["cheapest_brl"]
    delta = curr - prev
    direction = "UP" if delta > 0 else "DOWN" if delta < 0 else "UNCHANGED"
    return (f"Price trend: {direction} &nbsp; "
            f"Previous BRL {prev:.0f} &rarr; Now BRL {curr:.0f} ({delta:+.0f})")


# ─────────────────────────────────────────────────
# 3.  BOOKING LINKS
# ─────────────────────────────────────────────────
def google_flights_url():
    return (
        "https://www.google.com/travel/flights/search?"
        "tfs=CBwQAhooEgoyMDI2LTEyLTE4agcIARIDQ0RHcgcIARIDR1JV"
        "GgoyMDI3LTAxLTEwagcIARIDR1JVcgcIARIDQ0RHQAFIAXABggELCP___________wE"
    )

def kayak_url():
    return (f"https://www.kayak.com/flights/"
            f"{CONFIG['origin']}-{CONFIG['destination']}/"
            f"{CONFIG['depart_date']}/{CONFIG['return_dates'][-1]}?sort=price_a")

def skyscanner_url():
    dep = CONFIG["depart_date"].replace("-", "")
    ret = CONFIG["return_dates"][-1].replace("-", "")
    o   = CONFIG["origin"].lower()
    d   = CONFIG["destination"].lower()
    return f"https://www.skyscanner.net/transport/flights/{o}/{d}/{dep}/{ret}/"

def momondo_url():
    return (f"https://www.momondo.com/flight-search/"
            f"{CONFIG['origin']}-{CONFIG['destination']}/"
            f"{CONFIG['depart_date']}/{CONFIG['return_dates'][-1]}/1adults/Economy?sort=price_a")


# ─────────────────────────────────────────────────
# 4.  SEND EMAIL via SendGrid HTTP API
#     Uses HTTPS port 443 -- never blocked by corporate firewalls
# ─────────────────────────────────────────────────
def popup(title: str, message: str, error: bool = False):
    """Show a native Windows message box. Works even without a display."""
    if sys.platform == "win32":
        import ctypes
        icon = 0x10 if error else 0x40  # 0x10 = error icon, 0x40 = info icon
        ctypes.windll.user32.MessageBoxW(0, message, title, icon)
    else:
        # Fallback for non-Windows: just print
        print(f"\n[{title}] {message}\n")


def send_email(results_by_date: dict, history: list) -> bool:
    """results_by_date: {return_date: [flight_dicts]}. Returns True on success."""
    if not CONFIG["sendgrid_key"]:
        log.error("Email not sent -- SENDGRID_API_KEY missing from .env")
        return False
    if not CONFIG["sendgrid_from"] or not CONFIG["alert_email"]:
        log.error("Email not sent -- SENDGRID_FROM or ALERT_EMAIL missing from .env")
        return False

    trend = price_trend(history)

    # Find overall cheapest across all dates
    all_flights = [f for flights in results_by_date.values() for f in flights]
    overall_cheapest = min(all_flights, key=lambda x: x["price_brl"]) if all_flights else None

    # Build one section per return date
    date_sections = ""
    for return_date, flights in results_by_date.items():
        top = flights[:CONFIG["results_to_show"]]
        label = return_date  # e.g. "2027-01-07"
        # Format nicely: "Thu Jan 07"
        from datetime import datetime as dt
        try:
            d = dt.strptime(return_date, "%Y-%m-%d")
            label = d.strftime("%a %d %b %Y")
        except Exception:
            pass

        if not top:
            date_sections += f"""
            <h3 style="color:#555;margin-top:25px">Return: {label}</h3>
            <p style="color:#999;font-style:italic">No flights found matching filters for this date.</p>"""
            continue

        rows = ""
        for i, f in enumerate(top, 1):
            stop_label = ("Direct" if f["stops"] == 0
                          else f"{f['stops']} stop(s) via {', '.join(f['layovers'])}")
            bg = "#f9f9f9" if i % 2 else "#ffffff"
            rows += f"""
            <tr style="background:{bg}">
              <td style="padding:8px;font-weight:bold;color:#1a73e8;text-align:center">{i}</td>
              <td style="padding:8px">{f["airline"]}</td>
              <td style="padding:8px;font-size:15px;font-weight:bold;color:#1b5e20;white-space:nowrap">R$ {f["price_brl"]:,.0f}</td>
              <td style="padding:8px">{stop_label}</td>
              <td style="padding:8px;white-space:nowrap">{f["duration"]}</td>
              <td style="padding:8px;white-space:nowrap">{f["departs"]}<br>to {f["arrives"]}</td>
              <td style="padding:8px;text-align:center">
                <a href="{f["booking_link"]}" style="background:#1b5e20;color:white;padding:6px 12px;border-radius:4px;text-decoration:none;font-size:12px;white-space:nowrap">Book now</a>
              </td>
            </tr>"""

        date_sections += f"""
        <h3 style="color:#1a73e8;margin-top:28px;border-bottom:2px solid #1a73e8;padding-bottom:6px">
          Return: {label} &mdash; from R$ {top[0]["price_brl"]:,.0f}
        </h3>
        <table style="width:100%;border-collapse:collapse;font-size:13px">
          <thead><tr style="background:#1a73e8;color:white">
            <th style="padding:8px">#</th>
            <th style="padding:8px">Airline</th>
            <th style="padding:8px">Price (BRL)</th>
            <th style="padding:8px">Stops</th>
            <th style="padding:8px">Duration</th>
            <th style="padding:8px">Times</th>
            <th style="padding:8px">Book</th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>"""

    cheapest_summary = (
        f"R$ {overall_cheapest['price_brl']:,.0f} with {overall_cheapest['airline']} "
        f"(return {overall_cheapest['return_date']})"
        if overall_cheapest else "N/A"
    )

    html_body = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:860px;margin:auto;padding:20px">
      <div style="background:#1a73e8;color:white;padding:20px;border-radius:8px 8px 0 0">
        <h1 style="margin:0">Flight Price Alert - Paris to Sao Paulo</h1>
        <p style="margin:5px 0 0">CDG -> GRU | Depart Dec 18 | 4 return options | max {CONFIG["max_stops"]} stop | max {CONFIG["max_duration_h"]}h</p>
      </div>
      <div style="background:#e8f5e9;border-left:5px solid #2e7d32;padding:15px;margin:20px 0;border-radius:4px">
        <strong>Price below your threshold of R$ {CONFIG["price_threshold"]:,}!</strong><br>
        Overall cheapest: <strong style="font-size:22px;color:#1b5e20">{cheapest_summary}</strong>
      </div>
      <p>{trend}</p>
      <p style="color:#555">Checked: {datetime.now().strftime("%A %d %B %Y at %H:%M")}</p>

      {date_sections}

      <h2 style="margin-top:30px">Search All Options</h2>
      <p>
        <a href="{google_flights_url()}" style="background:#1a73e8;color:white;padding:10px 18px;border-radius:5px;text-decoration:none;margin:4px;display:inline-block">Google Flights</a>
        <a href="{kayak_url()}"          style="background:#ff690f;color:white;padding:10px 18px;border-radius:5px;text-decoration:none;margin:4px;display:inline-block">KAYAK</a>
        <a href="{skyscanner_url()}"     style="background:#0770e3;color:white;padding:10px 18px;border-radius:5px;text-decoration:none;margin:4px;display:inline-block">Skyscanner</a>
        <a href="{momondo_url()}"        style="background:#721c78;color:white;padding:10px 18px;border-radius:5px;text-decoration:none;margin:4px;display:inline-block">Momondo</a>
      </p>
      <p style="color:#999;font-size:11px;margin-top:30px">
        Alert fires when any option is below R$ {CONFIG["price_threshold"]:,} | max {CONFIG["max_stops"]} stop | max {CONFIG["max_duration_h"]}h
      </p>
    </body></html>"""

    payload = {
        "personalizations": [{"to": [{"email": CONFIG["alert_email"]}]}],
        "from":    {"email": CONFIG["sendgrid_from"]},
        "subject": (f"Flight Alert: from R$ {overall_cheapest['price_brl']:,.0f} CDG-GRU "
                    f"(below R$ {CONFIG['price_threshold']:,})")
                   if overall_cheapest else "Flight Monitor Alert",
        "content": [{"type": "text/html", "value": html_body}],
    }
    headers = {
        "Authorization": f"Bearer {CONFIG['sendgrid_key']}",
        "Content-Type":  "application/json",
    }

    log.info(f"Sending alert via SendGrid to {CONFIG['alert_email']} ...")
    try:
        resp = requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            json=payload, headers=headers,
            timeout=15, verify=False,
        )
        if resp.status_code == 202:
            log.info(f"Email sent successfully to {CONFIG['alert_email']}")
            return True
        else:
            log.error(f"SendGrid error {resp.status_code}: {resp.text}")
            return False
    except Exception as e:
        log.error(f"SendGrid request failed: {e}")
        return False


# ─────────────────────────────────────────────────
# 5.  MAIN
# ─────────────────────────────────────────────────
def main():
    return_dates_str = ", ".join(CONFIG["return_dates"])
    log.info("=" * 55)
    log.info("Flight Monitor -- CDG to GRU")
    log.info(f"Depart: {CONFIG['depart_date']}  |  Returns: {return_dates_str}")
    log.info(f"Threshold: BRL {CONFIG['price_threshold']:,}  |  max {CONFIG['max_stops']} stop  |  max {CONFIG['max_duration_h']}h")
    log.info("=" * 55)

    if not CONFIG["serpapi_key"]:
        log.error("SERPAPI_KEY not set in .env")
        return

    try:
        results_by_date = fetch_all_dates()
    except Exception as e:
        log.error(f"Failed to fetch flights: {e}")
        return

    # Flatten all flights to find overall cheapest
    all_flights = [f for flights in results_by_date.values() for f in flights]

    if not all_flights:
        log.warning("No flights found across all dates after filters.")
        popup("Flight Monitor - No Results",
              "No flights found matching filters (max 1 stop, max 18h) for any return date.",
              error=True)
        return

    cheapest_price = min(f["price_brl"] for f in all_flights)
    cheapest_flight = min(all_flights, key=lambda x: x["price_brl"])

    history = load_history()
    save_history(history, cheapest_price)

    log.info("-" * 50)
    log.info(f"  Overall cheapest: BRL {cheapest_price:,.0f} ({cheapest_flight['airline']}) return {cheapest_flight['return_date']}")
    log.info(f"  Threshold:        BRL {CONFIG['price_threshold']:,}")
    log.info(f"  Trigger alert:    {'YES' if cheapest_price < CONFIG['price_threshold'] else 'No'}")
    log.info("-" * 50)

    # Log summary per date
    for return_date, flights in results_by_date.items():
        if flights:
            log.info(f"  {return_date}: cheapest BRL {flights[0]['price_brl']:,.0f} ({flights[0]['airline']})")
        else:
            log.info(f"  {return_date}: no results after filters")

    if cheapest_price < CONFIG["price_threshold"]:
        log.info(f"ALERT: BRL {cheapest_price:,.0f} is below threshold -- sending email!")
        resolve_booking_links_for_email(results_by_date)
        updated = history + [{"timestamp": datetime.now().isoformat(), "cheapest_brl": cheapest_price}]
        email_ok = send_email(results_by_date, updated)
        if email_ok:
            popup(
                "Flight Alert Sent!",
                f"Cheapest: R$ {cheapest_price:,.0f} ({cheapest_flight['airline']})"
                f"\nReturn: {cheapest_flight['return_date']}"
                f"\n\nEmail sent to: {CONFIG['alert_email']}"
                f"\nCheck your inbox!"
            )
        else:
            popup("Flight Alert - Email Failed",
                  f"Price found R$ {cheapest_price:,.0f} but email failed.\nCheck the log.",
                  error=True)
    else:
        log.info(f"BRL {cheapest_price:,.0f} is above threshold R$ {CONFIG['price_threshold']:,}. No email sent.")
        popup(
            "Flight Monitor - Check Complete",
            f"Cheapest: R$ {cheapest_price:,.0f} ({cheapest_flight['airline']})"
            f"\nReturn: {cheapest_flight['return_date']}"
            f"\n\nAbove threshold of R$ {CONFIG['price_threshold']:,}. No email sent."
        )

    log.info("Booking links:")
    log.info(f"  Google Flights: {google_flights_url()}")
    log.info(f"  KAYAK:          {kayak_url()}")


if __name__ == "__main__":
    main()
