import math
import time
import re
import random
import requests

from PIL import Image, ImageDraw, ImageFont
from rgbmatrix import RGBMatrix, RGBMatrixOptions


# ============================================================================
# CONFIGURATION
# ============================================================================

OBSERVER_LAT = 37.940256
OBSERVER_LON = 23.742944

BBOX_DELTA = 0.7

LAT_MIN = OBSERVER_LAT - BBOX_DELTA
LAT_MAX = OBSERVER_LAT + BBOX_DELTA

LON_MIN = OBSERVER_LON - BBOX_DELTA
LON_MAX = OBSERVER_LON + BBOX_DELTA

POLL_INTERVAL = 60

OPENSKY_URL = "https://opensky-network.org/api/states/all"

REQUEST_TIMEOUT = 10
ROUTE_REQUEST_TIMEOUT = 6


# ============================================================================
# AIRLINE LOOKUP
# ============================================================================

AIRLINE_LOOKUP = {

    # Greek / regional
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
    "CTN": "Croatia Airlines",
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

    # North America
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

    # Cargo
    "FDX": "FedEx",
    "UPS": "UPS Airlines",
    "BOX": "ASL Airlines",
    "BCS": "European Air Transport",
}

UNKNOWN_AIRLINE = "Unknown"


# ============================================================================
# OPENSKY FIELD INDICES
# ============================================================================

IDX_ICAO24 = 0
IDX_CALLSIGN = 1
IDX_ORIGIN_COUNTRY = 2
IDX_LONGITUDE = 5
IDX_LATITUDE = 6
IDX_BARO_ALTITUDE = 7
IDX_ON_GROUND = 8
IDX_VELOCITY = 9
IDX_TRUE_TRACK = 10


# ============================================================================
# ROUTE APIS
# ============================================================================

ADSBDB_URL = "https://api.adsbdb.com/v0/callsign/{callsign}"

HEXDB_URL = "https://hexdb.io/api/v1/route/icao/{callsign}"

ROUTE_CACHE = {}

ROUTE_CACHE_MAX_SIZE = 500

ROUTE_UNKNOWN_TTL = 600


# ============================================================================
# LED MATRIX
# ============================================================================

LED_ROWS = 32
LED_COLS = 64

options = RGBMatrixOptions()

options.rows = LED_ROWS
options.cols = LED_COLS

# This is the setting that worked with your panel.
options.gpio_slowdown = 2

# If your panel is directly connected to the Raspberry Pi,
# this should normally remain 0.
options.chain_length = 1
options.parallel = 1

matrix = RGBMatrix(options=options)


# ============================================================================
# FONT
# ============================================================================

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

FONT_SMALL = ImageFont.truetype(
    FONT_PATH,
    8
)

FONT_MEDIUM = ImageFont.truetype(
    FONT_PATH,
    9
)


# ============================================================================
# GEOMETRY
# ============================================================================

def haversine_km(lat1, lon1, lat2, lon2):

    R = 6371.0

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)

    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)

    a = (
        math.sin(dphi / 2) ** 2
        +
        math.cos(phi1)
        * math.cos(phi2)
        * math.sin(dlambda / 2) ** 2
    )

    return R * 2 * math.atan2(
        math.sqrt(a),
        math.sqrt(1 - a)
    )


def bearing_degrees(lat1, lon1, lat2, lon2):

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)

    dlambda = math.radians(lon2 - lon1)

    x = math.sin(dlambda) * math.cos(phi2)

    y = (
        math.cos(phi1) * math.sin(phi2)
        -
        math.sin(phi1)
        * math.cos(phi2)
        * math.cos(dlambda)
    )

    return (
        math.degrees(math.atan2(x, y))
        + 360
    ) % 360


def bearing_to_compass(degrees):

    labels = [
        "N",
        "NE",
        "E",
        "SE",
        "S",
        "SW",
        "W",
        "NW"
    ]

    return labels[
        round(degrees / 45) % 8
    ]


# ============================================================================
# CALLSIGN / AIRLINE
# ============================================================================

def extract_airline_prefix(callsign):

    match = re.match(
        r"^([A-Za-z]+)",
        callsign or ""
    )

    if match:
        return match.group(1).upper()

    return ""


def lookup_airline(callsign):

    prefix = extract_airline_prefix(
        callsign
    )

    for length in range(
        len(prefix),
        0,
        -1
    ):

        airline = AIRLINE_LOOKUP.get(
            prefix[:length]
        )

        if airline:
            return airline

    return UNKNOWN_AIRLINE


# ============================================================================
# ROUTE CACHE
# ============================================================================

def _evict_oldest_cache_entry():

    if ROUTE_CACHE:

        oldest = next(
            iter(ROUTE_CACHE)
        )

        del ROUTE_CACHE[oldest]


def _is_cached(callsign):

    entry = ROUTE_CACHE.get(
        callsign
    )

    if entry is None:
        return False

    # Successful route
    if entry[0] is not None:
        return True

    # Failed lookup
    _, timestamp = entry

    return (
        time.time() - timestamp
    ) < ROUTE_UNKNOWN_TTL


# ============================================================================
# ADSBDB
# ============================================================================

def _query_adsbdb(callsign):

    try:

        url = ADSBDB_URL.format(
            callsign=callsign
        )

        response = requests.get(
            url,
            timeout=ROUTE_REQUEST_TIMEOUT
        )

        if response.status_code != 200:
            return None

        data = response.json()

        route = (
            data
            .get("response", {})
            .get("flightroute", {})
        )

        origin_data = route.get(
            "origin",
            {}
        )

        destination_data = route.get(
            "destination",
            {}
        )

        origin = (
            origin_data.get("iata_code")
            or origin_data.get("icao_code")
            or ""
        ).strip()

        destination = (
            destination_data.get("iata_code")
            or destination_data.get("icao_code")
            or ""
        ).strip()

        if not origin or not destination:
            return None

        return (
            origin,
            destination
        )

    except Exception:

        return None


# ============================================================================
# HEXDB
# ============================================================================

def _query_hexdb(callsign):

    try:

        url = HEXDB_URL.format(
            callsign=callsign
        )

        response = requests.get(
            url,
            timeout=ROUTE_REQUEST_TIMEOUT
        )

        if response.status_code != 200:
            return None

        data = response.json()

        raw_route = (
            data.get("route")
            or data.get("flightroute")
            or ""
        )

        raw_route = raw_route.strip()

        if not raw_route:
            return None

        segments = [
            x.strip()
            for x in raw_route.split("-")
            if x.strip()
        ]

        if len(segments) < 2:
            return None

        return (
            segments[0],
            segments[-1]
        )

    except Exception:

        return None


# ============================================================================
# ROUTE LOOKUP
# ============================================================================

def lookup_route(callsign):

    if not callsign:
        return None

    callsign = callsign.strip().upper()

    if callsign == "N/A":
        return None

    # ---------------------------------------------------------------
    # Cache
    # ---------------------------------------------------------------

    entry = ROUTE_CACHE.get(
        callsign
    )

    if entry is not None:

        # Successful route
        if entry[0] is not None:
            return entry

        # Failed route still cached
        _, cached_at = entry

        if (
            time.time() - cached_at
        ) < ROUTE_UNKNOWN_TTL:

            return None

    # ---------------------------------------------------------------
    # ADSBDB
    # ---------------------------------------------------------------

    result = _query_adsbdb(
        callsign
    )

    # ---------------------------------------------------------------
    # HexDB fallback
    # ---------------------------------------------------------------

    if result is None:

        result = _query_hexdb(
            callsign
        )

    # ---------------------------------------------------------------
    # Cache
    # ---------------------------------------------------------------

    if len(ROUTE_CACHE) >= ROUTE_CACHE_MAX_SIZE:

        _evict_oldest_cache_entry()

    if result:

        ROUTE_CACHE[callsign] = result

        return result

    ROUTE_CACHE[callsign] = (
        None,
        time.time()
    )

    return None


# ============================================================================
# COMPLETENESS
# ============================================================================

def is_complete(aircraft):

    callsign = (
        aircraft
        .get("callsign", "")
        .strip()
    )

    if not callsign:
        return False

    if callsign.upper() == "N/A":
        return False

    # Must have recognised airline
    if (
        aircraft.get("airline")
        == UNKNOWN_AIRLINE
    ):
        return False

    # Must have valid route
    origin = aircraft.get(
        "origin"
    )

    destination = aircraft.get(
        "destination"
    )

    if not origin or not destination:
        return False

    if origin == "?" or destination == "?":
        return False

    # Must have altitude
    if (
        aircraft.get("baro_altitude")
        is None
    ):
        return False

    # Must have speed
    if (
        aircraft.get("velocity_kmh")
        is None
    ):
        return False

    # Must have heading
    if (
        aircraft.get("true_track")
        is None
    ):
        return False

    # Must be airborne
    if aircraft.get(
        "on_ground",
        True
    ):
        return False

    # Must have distance
    if (
        aircraft.get("distance_km")
        is None
    ):
        return False

    return True


# ============================================================================
# OPENSKY FETCH
# ============================================================================

def fetch_raw_aircraft():

    params = {

        "lamin": LAT_MIN,
        "lomin": LON_MIN,

        "lamax": LAT_MAX,
        "lomax": LON_MAX,
    }

    try:

        response = requests.get(
            OPENSKY_URL,
            params=params,
            timeout=REQUEST_TIMEOUT,
            verify="/etc/ssl/certs/ca-certificates.crt"
        )

        if response.status_code == 429:

            print(
                "OpenSky rate limited."
            )

            return None

        if response.status_code != 200:

            print(
                "OpenSky HTTP:",
                response.status_code
            )

            return None

        data = response.json()

    except requests.exceptions.Timeout:

        print(
            "OpenSky request timed out."
        )

        return None

    except requests.exceptions.ConnectionError:

        print(
            "Could not connect to OpenSky."
        )

        return None

    except Exception as exc:

        print(
            "OpenSky error:",
            exc
        )

        return None

    states = (
        data.get("states")
        or []
    )

    aircraft_list = []

    for state in states:

        # ---------------------------------------------------------------
        # Position
        # ---------------------------------------------------------------

        lat = state[
            IDX_LATITUDE
        ]

        lon = state[
            IDX_LONGITUDE
        ]

        if lat is None or lon is None:
            continue

        # ---------------------------------------------------------------
        # Callsign
        # ---------------------------------------------------------------

        raw_callsign = state[
            IDX_CALLSIGN
        ]

        callsign = (
            raw_callsign.strip()
            if raw_callsign
            else ""
        )

        if not callsign:
            continue

        # ---------------------------------------------------------------
        # Airline
        # ---------------------------------------------------------------

        airline = lookup_airline(
            callsign
        )

        # ---------------------------------------------------------------
        # Speed
        # ---------------------------------------------------------------

        velocity_ms = state[
            IDX_VELOCITY
        ]

        if velocity_ms is None:
            velocity_kmh = None
        else:
            velocity_kmh = round(
                velocity_ms * 3.6
            )

        # ---------------------------------------------------------------
        # Distance
        # ---------------------------------------------------------------

        distance = round(
            haversine_km(
                OBSERVER_LAT,
                OBSERVER_LON,
                lat,
                lon
            ),
            2
        )

        # ---------------------------------------------------------------
        # Bearing
        # ---------------------------------------------------------------

        bearing = round(
            bearing_degrees(
                OBSERVER_LAT,
                OBSERVER_LON,
                lat,
                lon
            ),
            1
        )

        aircraft_list.append({

            "icao24":
                state[IDX_ICAO24],

            "callsign":
                callsign,

            "origin_country":
                state[IDX_ORIGIN_COUNTRY],

            "latitude":
                lat,

            "longitude":
                lon,

            "baro_altitude":
                state[IDX_BARO_ALTITUDE],

            "velocity_kmh":
                velocity_kmh,

            "true_track":
                state[IDX_TRUE_TRACK],

            "on_ground":
                state[IDX_ON_GROUND],

            "distance_km":
                distance,

            "bearing":
                bearing,

            "compass":
                bearing_to_compass(
                    bearing
                ),

            "airline":
                airline,
        })

    return aircraft_list


# ============================================================================
# AIRCRAFT SELECTION
# ============================================================================

MAX_ROUTE_LOOKUPS_PER_CYCLE = 20


def select_one_aircraft(raw_list):

    """
    First perform all cheap filtering.

    Then resolve routes.

    Only aircraft with a real origin AND destination are allowed.

    Finally randomly select one of the valid aircraft.

    This prevents the problem where we randomly choose one aircraft
    whose route happens not to exist in ADSBDB/HexDB.
    """

    # ========================================================================
    # STAGE 1 — CHEAP FILTERING
    # ========================================================================

    candidates = []

    for aircraft in raw_list:

        callsign = (
            aircraft
            .get("callsign", "")
            .strip()
        )

        # No callsign
        if not callsign:
            continue

        # Unknown airline
        if (
            aircraft.get("airline")
            == UNKNOWN_AIRLINE
        ):
            continue

        # No altitude
        if (
            aircraft.get("baro_altitude")
            is None
        ):
            continue

        # No speed
        if (
            aircraft.get("velocity_kmh")
            is None
        ):
            continue

        # No heading
        if (
            aircraft.get("true_track")
            is None
        ):
            continue

        # On ground
        if aircraft.get(
            "on_ground",
            True
        ):
            continue

        candidates.append(
            aircraft
        )

    print(
        f"After basic filtering: "
        f"{len(candidates)} candidates"
    )

    if not candidates:
        return None

    # ========================================================================
    # IMPORTANT:
    #
    # Put cached routes FIRST.
    #
    # This means if we've already successfully resolved RYR8944,
    # we don't waste an API call.
    # ========================================================================

    cached_candidates = []
    uncached_candidates = []

    for aircraft in candidates:

        callsign = aircraft[
            "callsign"
        ]

        if _is_cached(callsign):

            cached_candidates.append(
                aircraft
            )

        else:

            uncached_candidates.append(
                aircraft
            )

    # Randomise both groups
    random.shuffle(
        cached_candidates
    )

    random.shuffle(
        uncached_candidates
    )

    # Cached routes first
    ordered_candidates = (
        cached_candidates
        +
        uncached_candidates
    )

    # ========================================================================
    # STAGE 2 — ROUTE LOOKUPS
    # ========================================================================

    network_calls = 0

    valid_aircraft = []

    for aircraft in ordered_candidates:

        callsign = aircraft[
            "callsign"
        ]

        cached = _is_cached(
            callsign
        )

        if not cached:

            if (
                network_calls
                >= MAX_ROUTE_LOOKUPS_PER_CYCLE
            ):

                print(
                    "Route lookup budget reached."
                )

                break

            network_calls += 1

        print(
            f"Checking route: {callsign}"
        )

        route = lookup_route(
            callsign
        )

        # ---------------------------------------------------------------
        # NO ROUTE = REJECT
        #
        # We NEVER put ? into the aircraft.
        # ---------------------------------------------------------------

        if route is None:

            print(
                f"  No route: {callsign}"
            )

            continue

        origin, destination = route

        # Extra safety
        if not origin or not destination:
            continue

        if origin == "?" or destination == "?":
            continue

        aircraft["origin"] = origin
        aircraft["destination"] = destination

        if is_complete(
            aircraft
        ):

            print(
                f"  VALID: "
                f"{callsign} "
                f"{origin} -> {destination}"
            )

            valid_aircraft.append(
                aircraft
            )

    # ========================================================================
    # NOTHING VALID
    # ========================================================================

    if not valid_aircraft:

        print(
            "No suitable route found."
        )

        return None

    # ========================================================================
    # RANDOM VALID FLIGHT
    # ========================================================================

    selected = random.choice(
        valid_aircraft
    )

    print(
        f"Selected: "
        f"{selected['callsign']} "
        f"{selected['origin']} -> "
        f"{selected['destination']}"
    )

    return selected


# ============================================================================
# LED DISPLAY HELPERS
# ============================================================================

def text_width(draw, text, font):

    bbox = draw.textbbox(
        (0, 0),
        text,
        font=font
    )

    return bbox[2] - bbox[0]


def draw_centered(
    draw,
    text,
    y,
    font
):

    width = text_width(
        draw,
        text,
        font
    )

    x = (
        LED_COLS - width
    ) // 2

    draw.text(
        (x, y),
        text,
        font=font,
        fill=(255, 255, 255)
    )


# ============================================================================
# LED DISPLAY
# ============================================================================

def display_flight(aircraft):

    image = Image.new(
        "RGB",
        (
            LED_COLS,
            LED_ROWS
        ),
        (0, 0, 0)
    )

    draw = ImageDraw.Draw(
        image
    )

    if aircraft is None:

        draw_centered(
            draw,
            "NO FLIGHT",
            10,
            FONT_MEDIUM
        )

    else:

        callsign = aircraft[
            "callsign"
        ]

        airline = aircraft[
            "airline"
        ]

        origin = aircraft[
            "origin"
        ]

        destination = aircraft[
            "destination"
        ]

        altitude = int(
            aircraft[
                "baro_altitude"
            ]
        )

        # ---------------------------------------------------------------
        # Line 1
        # ---------------------------------------------------------------

        line1 = (
            f"{callsign} - {airline}"
        )

        # ---------------------------------------------------------------
        # Line 2
        # ---------------------------------------------------------------

        line2 = (
            f"{origin} -> {destination}"
        )

        # ---------------------------------------------------------------
        # Line 3
        # ---------------------------------------------------------------

        line3 = (
            f"ALT {altitude:,}m"
        )

        # ---------------------------------------------------------------
        # Draw
        # ---------------------------------------------------------------

        draw_centered(
            draw,
            line1,
            2,
            FONT_SMALL
        )

        draw_centered(
            draw,
            line2,
            12,
            FONT_MEDIUM
        )

        draw_centered(
            draw,
            line3,
            23,
            FONT_MEDIUM
        )

    # Push image to matrix
    matrix.SetImage(
        image
    )


# ============================================================================
# MAIN
# ============================================================================

def main():

    print(
        "Aircraft Radar - Athens"
    )

    print(
        f"Bounding box:"
        f" lat [{LAT_MIN:.6f}, {LAT_MAX:.6f}]"
        f" lon [{LON_MIN:.6f}, {LON_MAX:.6f}]"
    )

    print(
        f"Refresh interval: "
        f"{POLL_INTERVAL}s"
    )

    # Clear display initially
    matrix.Clear()

    while True:

        timestamp = time.strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        print()
        print(
            f"[{timestamp}] "
            "Fetching aircraft..."
        )

        # ---------------------------------------------------------------
        # OpenSky
        # ---------------------------------------------------------------

        raw_list = fetch_raw_aircraft()

        if raw_list is None:

            print(
                "Could not retrieve aircraft."
            )

            display_flight(
                None
            )

        else:

            print(
                f"OpenSky returned "
                f"{len(raw_list)} aircraft."
            )

            # -----------------------------------------------------------
            # Select a genuinely complete flight
            # -----------------------------------------------------------

            aircraft = select_one_aircraft(
                raw_list
            )

            # -----------------------------------------------------------
            # LED
            # -----------------------------------------------------------

            display_flight(
                aircraft
            )

            if aircraft:

                print()
                print(
                    "DISPLAYING:"
                )

                print(
                    f"{aircraft['callsign']} - "
                    f"{aircraft['airline']}"
                )

                print(
                    f"{aircraft['origin']} -> "
                    f"{aircraft['destination']}"
                )

                print(
                    f"ALT "
                    f"{int(aircraft['baro_altitude']):,}m"
                )

        print(
            f"Next refresh in "
            f"{POLL_INTERVAL} seconds..."
        )

        time.sleep(
            POLL_INTERVAL
        )


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":

    try:

        main()

    except KeyboardInterrupt:

        print(
            "\nStopped by user."
        )

        matrix.Clear()
