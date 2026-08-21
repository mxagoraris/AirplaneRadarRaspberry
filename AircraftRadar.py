"""
Raspberry Pi flight radar script polling the OpenSky Network anonymous REST API
for aircraft above a fixed location near Athens, Greece.

Selects a SINGLE aircraft per refresh cycle at random from the whitelisted
airlines for which all display fields are known, and refreshes every 60 seconds,
because the target hardware is a small screen showing one flight at a time with
the airline logo.

Route data comes from adsbdb.com and hexdb.io (both free, no API key) and is
cached in memory per callsign.

Dependencies: pip install requests
Usage: python3 aircraft_radar.py
"""

import math
import time
import re
import random
import requests

# ---------------------------------------------------------------------------
# Observer location – fixed point near Athens, Greece
# ---------------------------------------------------------------------------
OBSERVER_LAT = 37.940256
OBSERVER_LON = 23.742944

# ---------------------------------------------------------------------------
# AIRLINE WHITELIST – edit this list to match the logo files you have on disk.
#
# These are the ICAO callsign prefixes the user has logo files for.  Logo files
# must be named <PREFIX>.png inside the LOGO_DIR directory.  Only aircraft
# whose callsign prefix appears in this list will ever be selected for display;
# add or remove entries here to match the logo files present, and nothing else
# in the script needs changing.
#
# Notes:
#   • easyJet operates under several prefixes (EZY / EZS / EJU).  If you hold
#     one easyJet logo file you may want all three listed here.
#   • Aegean (AEE) and Ryanair (RYR) will dominate the feed over Athens, so
#     expect them to appear most often.
# ---------------------------------------------------------------------------
LOGO_AIRLINES = ["AEE", "RYR", "EZY", "WZZ", "THY", "DLH", "BAW", "AFR", "KLM", "UAE"]

# Set built once at module level for O(1) membership checks – do not edit this.
WHITELIST_SET = set(LOGO_AIRLINES)

# ---------------------------------------------------------------------------
# Logo file locations – logo rendering is handled at the display stage.
# The filename for a given prefix is derived as f"{LOGO_DIR}/{prefix}{LOGO_EXTENSION}".
# ---------------------------------------------------------------------------
LOGO_DIR = "logos"
LOGO_EXTENSION = ".png"

# ---------------------------------------------------------------------------
# Bounding box – roughly ±77 km at this latitude
# ---------------------------------------------------------------------------
BBOX_DELTA = 0.7
LAT_MIN = OBSERVER_LAT - BBOX_DELTA
LAT_MAX = OBSERVER_LAT + BBOX_DELTA
LON_MIN = OBSERVER_LON - BBOX_DELTA
LON_MAX = OBSERVER_LON + BBOX_DELTA

# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------
POLL_INTERVAL = 60  # seconds between full refresh cycles

# ---------------------------------------------------------------------------
# OpenSky Network API
# ---------------------------------------------------------------------------
OPENSKY_URL = "https://opensky-network.org/api/states/all"
REQUEST_TIMEOUT = 10  # seconds for the main OpenSky HTTP request

# ---------------------------------------------------------------------------
# Airline lookup table
# ---------------------------------------------------------------------------

# Greek / regional carriers
AIRLINE_LOOKUP = {
    "AEE": "Aegean Airlines",
    "OAL": "Olympic Air",
    "SKY": "Sky Express",
    "SEH": "Sky Express",

    # European low-cost
    "RYR": "Ryanair",
    "EZY": "easyJet",
    "EZS": "easyJet",
    "EJU": "easyJet",
    "WZZ": "Wizz Air",
    "VLG": "Vueling",
    "IBE": "Iberia",
    "BEL": "Brussels Airlines",
    "SWR": "Swiss",
    "AUA": "Austrian Airlines",
    "EIN": "Aer Lingus",
    "OCN": "Eurowings",

    # European full-service
    "BAW": "British Airways",
    "DLH": "Lufthansa",
    "AFR": "Air France",
    "KLM": "KLM",
    "TAP": "TAP Air Portugal",
    "THY": "Turkish Airlines",
    "TOM": "TUI Airways",
    "FIN": "Finnair",
    "SAS": "Scandinavian Airlines",
    "CSA": "Czech Airlines",
    "LOT": "LOT Polish Airlines",
    "ALK": "SriLankan Airlines",
    "BMS": "Air Serbia",
    "CTN": "Croatia Airline",
    "ROT": "Tarom",

    # Middle East / Gulf
    "UAE": "Emirates",
    "QTR": "Qatar Airways",
    "ETH": "Ethiopian Airlines",
    "ETD": "Etihad Airways",
    "MSR": "EgyptAir",
    "MEA": "Middle East Airlines",
    "SVA": "Saudia",
    "FDB": "flydubai",
    "ABY": "Air Arabia",

    # North American
    "UAL": "United Airlines",
    "DAL": "Delta Air Lines",
    "AAL": "American Airlines",
    "WJA": "WestJet",
    "ACA": "Air Canada",

    # Asian / other
    "CPA": "Cathay Pacific",
    "SIA": "Singapore Airlines",
    "MAS": "Malaysia Airlines",
    "ANA": "All Nippon Airways",
    "JAL": "Japan Airlines",
    "KAL": "Korean Air",
    "CSN": "China Southern",
    "CCA": "Air China",
    "HFA": "Air Haifa",

    # Cargo / misc
    "FDX": "FedEx",
    "UPS": "UPS Airlines",
    "BOX": "ASL Airlines",
    "BCS": "European Air Transport",
}

UNKNOWN_AIRLINE = "Unknown"

# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def haversine_km(lat1, lon1, lat2, lon2):
    """Return the great-circle distance in kilometres between two points."""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing_degrees(lat1, lon1, lat2, lon2):
    """Return the initial bearing in degrees (0–360) from point 1 to point 2."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    x = math.sin(dlambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def bearing_to_compass(degrees):
    """Convert a bearing in degrees to an 8-point compass label."""
    labels = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    return labels[round(degrees / 45) % 8]


# ---------------------------------------------------------------------------
# Callsign / airline helpers
# ---------------------------------------------------------------------------

def extract_airline_prefix(callsign):
    """Return the leading alphabetic portion of a callsign, uppercased."""
    match = re.match(r"^([A-Za-z]+)", callsign or "")
    return match.group(1).upper() if match else ""


def lookup_airline(callsign):
    """
    Try progressively shorter prefix slices against AIRLINE_LOOKUP, falling
    back to UNKNOWN_AIRLINE if nothing matches.
    """
    prefix = extract_airline_prefix(callsign)
    for length in range(len(prefix), 0, -1):
        result = AIRLINE_LOOKUP.get(prefix[:length])
        if result:
            return result
    return UNKNOWN_AIRLINE


# ---------------------------------------------------------------------------
# Whitelist helpers
# ---------------------------------------------------------------------------

def is_whitelisted_airline(callsign):
    """
    Return True if the callsign's leading alphabetic prefix is present in
    WHITELIST_SET.  Uses the pre-built set for O(1) lookup.
    """
    return extract_airline_prefix(callsign) in WHITELIST_SET


def logo_path_for(callsign):
    """
    Return the expected logo file path for a whitelisted callsign, or None if
    the callsign's prefix is not in the whitelist.
    """
    prefix = extract_airline_prefix(callsign)
    if prefix in WHITELIST_SET:
        return f"{LOGO_DIR}/{prefix}{LOGO_EXTENSION}"
    return None


# ---------------------------------------------------------------------------
# OpenSky state-vector field indices
#
# Index  Field
#   0    icao24          – unique ICAO 24-bit address (hex string)
#   1    callsign        – 8-char callsign, may be null or blank
#   2    origin_country  – country of registration
#   3    time_position   – Unix timestamp of last position update
#   4    last_contact    – Unix timestamp of last signal
#   5    longitude       – WGS-84 longitude (degrees)
#   6    latitude        – WGS-84 latitude (degrees)
#   7    baro_altitude   – barometric altitude (metres)
#   8    on_ground       – boolean flag
#   9    velocity        – ground speed (m/s)
#  10    true_track      – track angle clockwise from north (degrees)
#  11    vertical_rate   – vertical rate (m/s)
#  12    sensors         – list of sensor IDs (may be null)
#  13    geo_altitude    – geometric altitude (metres)
#  14    squawk          – transponder code
#  15    spi             – special purpose indicator
#  16    position_source – 0=ADS-B, 1=ASTERIX, 2=MLAT, 3=FLARM
# ---------------------------------------------------------------------------
IDX_ICAO24         = 0
IDX_CALLSIGN       = 1
IDX_ORIGIN_COUNTRY = 2
IDX_LONGITUDE      = 5
IDX_LATITUDE       = 6
IDX_BARO_ALTITUDE  = 7
IDX_ON_GROUND      = 8
IDX_VELOCITY       = 9
IDX_TRUE_TRACK     = 10

# ---------------------------------------------------------------------------
# Route lookup – dual-source with in-memory cache
# ---------------------------------------------------------------------------

ADSBDB_URL         = "https://api.adsbdb.com/v0/callsign/{callsign}"
HEXDB_URL          = "https://hexdb.io/api/v1/route/icao/{callsign}"
ROUTE_REQUEST_TIMEOUT = 6        # seconds per route HTTP request
ROUTE_CACHE        = {}          # callsign -> (origin, destination) or (None, timestamp)
ROUTE_CACHE_MAX_SIZE = 500       # maximum number of entries before oldest-entry eviction
ROUTE_UNKNOWN_TTL  = 600         # seconds before retrying a failed route lookup


def _evict_oldest_cache_entry():
    """Remove the entry that was inserted first (dict insertion order, Python 3.7+)."""
    if ROUTE_CACHE:
        oldest_key = next(iter(ROUTE_CACHE))
        del ROUTE_CACHE[oldest_key]


def _query_adsbdb(callsign):
    """
    Query adsbdb.com for the route associated with *callsign*.

    Returns (origin, destination) as a tuple of airport code strings if both
    ends resolve, preferring IATA codes and falling back to ICAO codes.
    Returns None on 404, any non-200 status, or any exception.  Never raises.
    """
    try:
        url = ADSBDB_URL.format(callsign=callsign)
        response = requests.get(url, timeout=ROUTE_REQUEST_TIMEOUT)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            return None
        data = response.json()
        route = data.get("response", {}).get("flightroute", {})
        origin_data      = route.get("origin", {})
        destination_data = route.get("destination", {})
        origin = (origin_data.get("iata_code") or origin_data.get("icao_code") or "").strip()
        destination = (destination_data.get("iata_code") or destination_data.get("icao_code") or "").strip()
        if origin and destination:
            return (origin, destination)
        return None
    except Exception:
        return None


def _query_hexdb(callsign):
    """
    Query hexdb.io for the route associated with *callsign*.

    Parses a route-like string (e.g. "LHR-ATH" or "EGLL-LGAV-LFPG") by
    splitting on "-" and taking the first and last segments, so via-stops are
    handled correctly.  Returns (origin, destination) or None on any failure.
    Never raises.
    """
    try:
        url = HEXDB_URL.format(callsign=callsign)
        response = requests.get(url, timeout=ROUTE_REQUEST_TIMEOUT)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            return None
        data = response.json()
        # hexdb uses either "route" or "flightroute" as the key
        raw_route = data.get("route") or data.get("flightroute") or ""
        raw_route = raw_route.strip()
        if not raw_route:
            return None
        segments = [s.strip() for s in raw_route.split("-") if s.strip()]
        if len(segments) < 2:
            return None
        return (segments[0], segments[-1])
    except Exception:
        return None


def _is_cached(callsign):
    """
    Return True if *callsign* is resolvable from the cache right now WITHOUT
    making any network call.  A positive (known route) entry always qualifies;
    a negative entry qualifies only if it has NOT yet exceeded ROUTE_UNKNOWN_TTL.
    """
    entry = ROUTE_CACHE.get(callsign)
    if entry is None:
        return False
    # Positive cache hit: entry is a (origin, destination) tuple of strings
    if isinstance(entry, tuple) and len(entry) == 2 and not isinstance(entry[0], bool):
        # Distinguish from the miss sentinel (None, timestamp) by checking types
        if entry[0] is not None:
            return True
        # Miss sentinel: (None, timestamp)
        _, cached_at = entry
        return (time.time() - cached_at) < ROUTE_UNKNOWN_TTL
    return False


def lookup_route(callsign):
    """
    Return (origin, destination) for *callsign*, using the two-source lookup
    with in-memory caching.

    • Returns ("?", "?") immediately for blank / "N/A" callsigns.
    • Known routes are cached permanently.
    • Misses are cached with a timestamp and retried once ROUTE_UNKNOWN_TTL
      seconds have elapsed.
    • Evicts the oldest entry when the cache exceeds ROUTE_CACHE_MAX_SIZE.
    • Never raises.
    """
    if not callsign or callsign.strip().upper() == "N/A":
        return ("?", "?")

    entry = ROUTE_CACHE.get(callsign)

    if entry is not None:
        # Positive hit
        if entry[0] is not None:
            return entry  # type: (str, str)
        # Negative hit – check whether TTL has expired
        _, cached_at = entry
        if (time.time() - cached_at) < ROUTE_UNKNOWN_TTL:
            return ("?", "?")
        # TTL expired – fall through to re-query below

    # Network lookup: try adsbdb first, then hexdb as fallback
    result = _query_adsbdb(callsign) or _query_hexdb(callsign)

    # Evict if needed before inserting
    if len(ROUTE_CACHE) >= ROUTE_CACHE_MAX_SIZE:
        _evict_oldest_cache_entry()

    if result:
        ROUTE_CACHE[callsign] = result
        return result
    else:
        ROUTE_CACHE[callsign] = (None, time.time())
        return ("?", "?")


# ---------------------------------------------------------------------------
# Completeness check
# ---------------------------------------------------------------------------

def is_complete(aircraft):
    """
    Return True only when every field required for a well-formed display card
    is present and the aircraft is airborne.
    """
    cs = aircraft.get("callsign", "")
    if not cs or cs.strip().upper() == "N/A":
        return False
    if aircraft.get("airline") == UNKNOWN_AIRLINE:
        return False
    if aircraft.get("origin", "?") == "?" or aircraft.get("destination", "?") == "?":
        return False
    if aircraft.get("baro_altitude") is None:
        return False
    if aircraft.get("velocity_kmh") is None:
        return False
    if aircraft.get("true_track") is None:
        return False
    if aircraft.get("on_ground", True):
        return False
    if aircraft.get("distance_km") is None:
        return False
    return True


# ---------------------------------------------------------------------------
# OpenSky fetch
# ---------------------------------------------------------------------------

def fetch_raw_aircraft():
    """
    Poll the OpenSky Network for all state vectors inside the bounding box.

    Returns a list of aircraft dicts (no route data attached yet), or None if
    the request fails for any reason.  Route lookups are deliberately deferred
    to select_one_aircraft so that we never waste API calls on airlines we
    cannot display.
    """
    params = {
        "lamin": LAT_MIN,
        "lomin": LON_MIN,
        "lamax": LAT_MAX,
        "lomax": LON_MAX,
    }
    try:
        response = requests.get(OPENSKY_URL, params=params, timeout=REQUEST_TIMEOUT)
        if response.status_code == 429:
            print("⚠  Rate-limited by OpenSky API (HTTP 429). Waiting for next cycle.")
            return None
        if response.status_code != 200:
            print(f"⚠  Unexpected HTTP {response.status_code} from OpenSky API.")
            return None
        data = response.json()
    except requests.exceptions.Timeout:
        print("⚠  Request to OpenSky API timed out.")
        return None
    except requests.exceptions.ConnectionError:
        print("⚠  Could not connect to OpenSky API.")
        return None
    except Exception as exc:
        print(f"⚠  Unexpected error fetching from OpenSky API: {exc}")
        return None

    states = data.get("states") or []
    aircraft_list = []

    for state in states:
        lat = state[IDX_LATITUDE]
        lon = state[IDX_LONGITUDE]
        if lat is None or lon is None:
            continue  # skip records with no position fix

        raw_callsign = state[IDX_CALLSIGN]
        callsign = (raw_callsign.strip() if raw_callsign else "N/A") or "N/A"

        velocity_ms = state[IDX_VELOCITY]
        velocity_kmh = round(velocity_ms * 3.6) if velocity_ms is not None else None

        dist = round(haversine_km(OBSERVER_LAT, OBSERVER_LON, lat, lon), 2)
        brng = round(bearing_degrees(OBSERVER_LAT, OBSERVER_LON, lat, lon), 1)

        aircraft_list.append({
            "icao24":         state[IDX_ICAO24],
            "callsign":       callsign,
            "origin_country": state[IDX_ORIGIN_COUNTRY],
            "latitude":       lat,
            "longitude":      lon,
            "baro_altitude":  state[IDX_BARO_ALTITUDE],
            "velocity_kmh":   velocity_kmh,
            "true_track":     state[IDX_TRUE_TRACK],
            "on_ground":      state[IDX_ON_GROUND],
            "distance_km":    dist,
            "bearing":        brng,
            "compass":        bearing_to_compass(brng),
            "airline":        lookup_airline(callsign),
        })

    return aircraft_list


# ---------------------------------------------------------------------------
# Selection – whitelist-filtered, randomised, route-lazy
# ---------------------------------------------------------------------------

# The whitelist filter normally leaves only a handful of candidates, so this
# ceiling on route lookups per cycle is rarely reached in practice.
MAX_ROUTE_LOOKUPS_PER_CYCLE = 10


def select_one_aircraft(raw_list):
    """
    Choose one displayable aircraft at random from those belonging to a
    whitelisted airline, returning a tuple:

        (aircraft_dict_or_none, total_scanned, whitelisted_count)

    The three-stage pipeline is ordered deliberately to minimise network cost:

    Stage 1 – cheap, no network calls:
        Filter *raw_list* to candidates that (a) pass is_whitelisted_airline
        and (b) pass all non-route completeness checks (baro_altitude,
        velocity_kmh, true_track not None; on_ground False; callsign valid;
        airline known).  This whitelist prune happens BEFORE any network call –
        that is exactly why this version is cheaper than the previous
        closest-first design: we never spend a route lookup on an airline we
        cannot display.

    Stage 2 – free, in-process only:
        Shuffle the surviving candidate list.  Shuffling AFTER filtering (never
        before) is deliberate: shuffling first would scatter route lookups
        across airlines we cannot show.  The shuffle gives variety on the shelf
        display instead of always showing the same nearby aircraft.

    Stage 3 – potentially costly (network):
        Walk the shuffled list calling lookup_route on each candidate.  Return
        the FIRST one for which is_complete() passes (early exit – no point
        resolving the rest).  Cached callsigns cost nothing and do NOT count
        toward MAX_ROUTE_LOOKUPS_PER_CYCLE; only genuine network calls count.
        Once the budget is exhausted we continue only with candidates already
        resolvable via _is_cached.
    """
    total_scanned = len(raw_list)

    # ------------------------------------------------------------------
    # Stage 1: whitelist + cheap completeness filter (zero network calls)
    # ------------------------------------------------------------------
    candidates = []
    for ac in raw_list:
        cs = ac.get("callsign", "")
        if not cs or cs.strip().upper() == "N/A":
            continue
        if not is_whitelisted_airline(cs):
            continue
        if ac.get("airline") == UNKNOWN_AIRLINE:
            continue
        if ac.get("baro_altitude") is None:
            continue
        if ac.get("velocity_kmh") is None:
            continue
        if ac.get("true_track") is None:
            continue
        if ac.get("on_ground", True):
            continue
        candidates.append(ac)

    whitelisted_count = len(candidates)

    if not candidates:
        return (None, total_scanned, whitelisted_count)

    # ------------------------------------------------------------------
    # Stage 2: shuffle for variety (after filtering, never before)
    # ------------------------------------------------------------------
    random.shuffle(candidates)

    # ------------------------------------------------------------------
    # Stage 3: route lookup with budget – return the first complete match
    # ------------------------------------------------------------------
    network_calls_used = 0

    for ac in candidates:
        cs = ac["callsign"]
        cached = _is_cached(cs)

        if not cached:
            # This call will hit the network; check the budget first
            if network_calls_used >= MAX_ROUTE_LOOKUPS_PER_CYCLE:
                # Budget exhausted – skip candidates that need a network call
                continue
            network_calls_used += 1

        origin, destination = lookup_route(cs)
        ac["origin"]      = origin
        ac["destination"] = destination

        if is_complete(ac):
            return (ac, total_scanned, whitelisted_count)

    # Nothing qualified within budget
    return (None, total_scanned, whitelisted_count)


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def print_flight_card(aircraft, timestamp, total_scanned, whitelisted_count):
    """
    Print a compact boxed flight card (~46 characters wide) using box-drawing
    characters.  Stable layout between refreshes so the shelf display does not
    flicker.

    When *aircraft* is None, prints a placeholder card showing counts so the
    screen never looks broken.
    """
    width = 46  # inner content width (excluding the two border characters)
    h_line = "─" * width

    def row(text):
        """Print a single padded card row."""
        print(f"│ {text:<{width - 2}} │")

    def blank():
        row("")

    print(f"┌{h_line}┐")

    if aircraft is None:
        blank()
        row("✈  Searching for a whitelisted flight...")
        blank()
        row(f"   Aircraft in bbox : {total_scanned}")
        row(f"   Whitelisted match : {whitelisted_count}")
        blank()
        row(f"   {timestamp}")
        blank()
    else:
        cs        = aircraft.get("callsign", "N/A")
        airline   = aircraft.get("airline", UNKNOWN_AIRLINE)
        origin    = aircraft.get("origin", "?")
        dest      = aircraft.get("destination", "?")
        alt       = aircraft.get("baro_altitude")
        spd       = aircraft.get("velocity_kmh")
        dist      = aircraft.get("distance_km")
        compass   = aircraft.get("compass", "?")
        logo      = logo_path_for(cs) or "—"

        alt_str  = f"{int(alt):,} m"   if alt  is not None else "? m"
        spd_str  = f"{spd} km/h"       if spd  is not None else "? km/h"
        dist_str = f"{dist} km {compass}" if dist is not None else "? km"

        blank()
        row(f"✈  {cs}  –  {airline}")
        blank()
        row(f"   Route   : {origin}  →  {dest}")
        row(f"   Altitude: {alt_str}")
        row(f"   Speed   : {spd_str}")
        row(f"   From us : {dist_str}")
        blank()
        # Development aid – shows which logo file would be loaded at the
        # display stage.  Replace this line with the actual rendered logo
        # image once the screen is wired up.
        row(f"   Logo    : {logo}")
        blank()
        row(f"   Scanned {total_scanned} aircraft")
        row(f"   {timestamp}")
        blank()

    print(f"└{h_line}┘")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    print("Aircraft Radar – Athens, Greece")
    print(
        f"Bounding box: lat [{LAT_MIN:.6f}, {LAT_MAX:.6f}]  "
        f"lon [{LON_MIN:.6f}, {LON_MAX:.6f}]"
    )
    print(f"Refresh interval: {POLL_INTERVAL} seconds")
    print("Press Ctrl+C to quit.")
    print()

    while True:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        raw_list  = fetch_raw_aircraft()

        if raw_list is None:
            # Fetch failed – show an empty card so the screen stays alive
            print_flight_card(None, timestamp, 0, 0)
        else:
            aircraft, total_scanned, whitelisted_count = select_one_aircraft(raw_list)
            print_flight_card(aircraft, timestamp, total_scanned, whitelisted_count)

        print()
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped by user. Goodbye! ✈")
