"""
Raspberry Pi 64x32 HUB75 Flight Radar

Polls the OpenSky Network for aircraft around Athens, Greece,
selects one aircraft at random, looks up its route, and displays
the result on a 64x32 RGB HUB75 LED matrix.

Run from the rpi-rgb-led-matrix virtual environment:

    sudo ./venv/bin/python aircraft_radar.py

Dependencies:
    requests
    pillow
    rgbmatrix
"""

import math
import time
import re
import random
import requests

from PIL import Image, ImageDraw, ImageFont
from rgbmatrix import RGBMatrix, RGBMatrixOptions


# ============================================================================
# MATRIX CONFIGURATION
# ============================================================================

MATRIX_WIDTH = 64
MATRIX_HEIGHT = 32


def create_matrix():
    """Create and configure the 64x32 RGB matrix."""

    options = RGBMatrixOptions()

    options.rows = 32
    options.cols = 64
    options.chain_length = 1
    options.parallel = 1

    # This matches your working demo:
    # --led-slowdown-gpio=2
    options.gpio_slowdown = 2

    options.hardware_mapping = "regular"

    return RGBMatrix(options=options)


# ============================================================================
# DISPLAY FONTS
# ============================================================================

def load_font(size):
    """
    Load a font.

    We try several common locations used by the RGB matrix project,
    then fall back to PIL's built-in font.
    """

    candidates = [
        f"/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        f"/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
    ]

    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass

    return ImageFont.load_default()


FONT_SMALL = load_font(7)
FONT_MEDIUM = load_font(9)
FONT_LARGE = load_font(11)


# ============================================================================
# COLOURS
# ============================================================================

WHITE = (255, 255, 255)
CYAN = (0, 220, 255)
GREEN = (0, 255, 80)
YELLOW = (255, 220, 0)
RED = (255, 60, 60)
GREY = (130, 130, 130)


# ============================================================================
# OBSERVER LOCATION – ATHENS
# ============================================================================

OBSERVER_LAT = 37.940256
OBSERVER_LON = 23.742944


# ============================================================================
# BOUNDING BOX
# ============================================================================

BBOX_DELTA = 0.7

LAT_MIN = OBSERVER_LAT - BBOX_DELTA
LAT_MAX = OBSERVER_LAT + BBOX_DELTA

LON_MIN = OBSERVER_LON - BBOX_DELTA
LON_MAX = OBSERVER_LON + BBOX_DELTA


# ============================================================================
# POLLING
# ============================================================================

POLL_INTERVAL = 60


# ============================================================================
# OPENSKY
# ============================================================================

OPENSKY_URL = "https://opensky-network.org/api/states/all"

REQUEST_TIMEOUT = 10


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

    # Cargo
    "FDX": "FedEx",
    "UPS": "UPS Airlines",
    "BOX": "ASL Airlines",
    "BCS": "European Air Transport",
}

UNKNOWN_AIRLINE = "Unknown"


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
        + math.cos(phi1)
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
        - math.sin(phi1)
        * math.cos(phi2)
        * math.cos(dlambda)
    )

    return (
        math.degrees(math.atan2(x, y)) + 360
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
        "NW",
    ]

    return labels[round(degrees / 45) % 8]


# ============================================================================
# CALLSIGN / AIRLINE
# ============================================================================

def extract_airline_prefix(callsign):

    match = re.match(
        r"^([A-Za-z]+)",
        callsign or ""
    )

    return (
        match.group(1).upper()
        if match
        else ""
    )


def lookup_airline(callsign):

    prefix = extract_airline_prefix(callsign)

    for length in range(
        len(prefix),
        0,
        -1
    ):

        result = AIRLINE_LOOKUP.get(
            prefix[:length]
        )

        if result:
            return result

    return UNKNOWN_AIRLINE


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
# ROUTE LOOKUP
# ============================================================================

ADSBDB_URL = (
    "https://api.adsbdb.com/v0/callsign/{callsign}"
)

HEXDB_URL = (
    "https://hexdb.io/api/v1/route/icao/{callsign}"
)

ROUTE_REQUEST_TIMEOUT = 6

ROUTE_CACHE = {}

ROUTE_CACHE_MAX_SIZE = 500

ROUTE_UNKNOWN_TTL = 600


def _evict_oldest_cache_entry():

    if ROUTE_CACHE:

        oldest_key = next(
            iter(ROUTE_CACHE)
        )

        del ROUTE_CACHE[oldest_key]


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
            "origin", {}
        )

        destination_data = route.get(
            "destination", {}
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

        if origin and destination:

            return (
                origin,
                destination
            )

        return None

    except Exception:

        return None


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
        ).strip()

        if not raw_route:
            return None

        segments = [
            s.strip()
            for s in raw_route.split("-")
            if s.strip()
        ]

        if len(segments) < 2:
            return None

        return (
            segments[0],
            segments[-1]
        )

    except Exception:

        return None


def _is_cached(callsign):

    entry = ROUTE_CACHE.get(callsign)

    if entry is None:
        return False

    if entry[0] is not None:
        return True

    _, cached_at = entry

    return (
        time.time() - cached_at
    ) < ROUTE_UNKNOWN_TTL


def lookup_route(callsign):

    if (
        not callsign
        or callsign.strip().upper() == "N/A"
    ):

        return ("?", "?")

    entry = ROUTE_CACHE.get(callsign)

    if entry is not None:

        if entry[0] is not None:
            return entry

        _, cached_at = entry

        if (
            time.time() - cached_at
        ) < ROUTE_UNKNOWN_TTL:

            return ("?", "?")

    result = (
        _query_adsbdb(callsign)
        or _query_hexdb(callsign)
    )

    if len(ROUTE_CACHE) >= ROUTE_CACHE_MAX_SIZE:

        _evict_oldest_cache_entry()

    if result:

        ROUTE_CACHE[callsign] = result

        return result

    ROUTE_CACHE[callsign] = (
        None,
        time.time()
    )

    return ("?", "?")


# ============================================================================
# COMPLETENESS
# ============================================================================

def is_complete(aircraft):

    cs = aircraft.get(
        "callsign",
        ""
    )

    if (
        not cs
        or cs.strip().upper() == "N/A"
    ):
        return False

    if aircraft.get(
        "baro_altitude"
    ) is None:
        return False

    if aircraft.get(
        "velocity_kmh"
    ) is None:
        return False

    if aircraft.get(
        "true_track"
    ) is None:
        return False

    if aircraft.get(
        "on_ground",
        True
    ):
        return False

    if aircraft.get(
        "distance_km"
    ) is None:
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
            timeout=REQUEST_TIMEOUT
        )

        if response.status_code == 429:

            print(
                "OpenSky rate limit."
            )

            return None

        if response.status_code != 200:

            print(
                f"OpenSky HTTP "
                f"{response.status_code}"
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
            f"OpenSky error: {exc}"
        )

        return None

    states = data.get(
        "states"
    ) or []

    aircraft_list = []

    for state in states:

        lat = state[
            IDX_LATITUDE
        ]

        lon = state[
            IDX_LONGITUDE
        ]

        if (
            lat is None
            or lon is None
        ):
            continue

        raw_callsign = state[
            IDX_CALLSIGN
        ]

        callsign = (
            raw_callsign.strip()
            if raw_callsign
            else "N/A"
        ) or "N/A"

        velocity_ms = state[
            IDX_VELOCITY
        ]

        velocity_kmh = (
            round(
                velocity_ms * 3.6
            )
            if velocity_ms is not None
            else None
        )

        dist = round(
            haversine_km(
                OBSERVER_LAT,
                OBSERVER_LON,
                lat,
                lon
            ),
            2
        )

        brng = round(
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
                dist,

            "bearing":
                brng,

            "compass":
                bearing_to_compass(brng),

            "airline":
                lookup_airline(callsign),

        })

    return aircraft_list


# ============================================================================
# AIRCRAFT SELECTION
# ============================================================================

MAX_ROUTE_LOOKUPS_PER_CYCLE = 10


def select_one_aircraft(raw_list):

    total_scanned = len(
        raw_list
    )

    candidates = []

    for ac in raw_list:

        cs = ac.get(
            "callsign",
            ""
        )

        if (
            not cs
            or cs.strip().upper() == "N/A"
        ):
            continue

        if ac.get(
            "baro_altitude"
        ) is None:
            continue

        if ac.get(
            "velocity_kmh"
        ) is None:
            continue

        if ac.get(
            "true_track"
        ) is None:
            continue

        if ac.get(
            "on_ground",
            True
        ):
            continue

        candidates.append(ac)

    displayable_count = len(
        candidates
    )

    if not candidates:

        return (
            None,
            total_scanned,
            displayable_count
        )

    random.shuffle(candidates)

    network_calls_used = 0

    for ac in candidates:

        cs = ac["callsign"]

        cached = _is_cached(cs)

        if not cached:

            if (
                network_calls_used
                >= MAX_ROUTE_LOOKUPS_PER_CYCLE
            ):
                continue

            network_calls_used += 1

        origin, destination = lookup_route(
            cs
        )

        ac["origin"] = origin
        ac["destination"] = destination

        if is_complete(ac):

            return (
                ac,
                total_scanned,
                displayable_count
            )

    return (
        None,
        total_scanned,
        displayable_count
    )


# ============================================================================
# TEXT HELPERS
# ============================================================================

def fit_text(text, max_chars):

    text = str(text)

    if len(text) <= max_chars:
        return text

    if max_chars <= 3:
        return text[:max_chars]

    return text[:max_chars - 3] + "..."


# ============================================================================
# LED DISPLAY
# ============================================================================

def draw_flight_card(
    matrix,
    aircraft,
    total_scanned,
    displayable_count
):

    image = Image.new(
        "RGB",
        (
            MATRIX_WIDTH,
            MATRIX_HEIGHT
        ),
        (0, 0, 0)
    )

    draw = ImageDraw.Draw(image)

    # ------------------------------------------------------------------
    # No aircraft
    # ------------------------------------------------------------------

    if aircraft is None:

        draw.text(
            (2, 2),
            "FLIGHT RADAR",
            font=FONT_MEDIUM,
            fill=WHITE
        )

        draw.text(
            (2, 13),
            "SEARCHING...",
            font=FONT_SMALL,
            fill=YELLOW
        )

        draw.text(
            (2, 23),
            f"{total_scanned} ACFT",
            font=FONT_SMALL,
            fill=CYAN
        )

        matrix.SetImage(image)

        return

    # ------------------------------------------------------------------
    # Aircraft information
    # ------------------------------------------------------------------

    callsign = aircraft.get(
        "callsign",
        "N/A"
    )

    airline = aircraft.get(
        "airline",
        UNKNOWN_AIRLINE
    )

    origin = aircraft.get(
        "origin",
        "?"
    )

    destination = aircraft.get(
        "destination",
        "?"
    )

    altitude = aircraft.get(
        "baro_altitude"
    )

    speed = aircraft.get(
        "velocity_kmh"
    )

    distance = aircraft.get(
        "distance_km"
    )

    compass = aircraft.get(
        "compass",
        "?"
    )

    # ------------------------------------------------------------------
    # Line 1 – callsign
    # ------------------------------------------------------------------

    draw.text(
        (1, 0),
        fit_text(
            "✈ " + callsign,
            10
        ),
        font=FONT_LARGE,
        fill=WHITE
    )

    # ------------------------------------------------------------------
    # Line 2 – airline
    # ------------------------------------------------------------------

    draw.text(
        (1, 10),
        fit_text(
            airline.upper(),
            17
        ),
        font=FONT_SMALL,
        fill=CYAN
    )

    # ------------------------------------------------------------------
    # Line 3 – route
    # ------------------------------------------------------------------

    route = (
        f"{origin} > {destination}"
    )

    draw.text(
        (1, 17),
        fit_text(
            route,
            18
        ),
        font=FONT_MEDIUM,
        fill=GREEN
    )

    # ------------------------------------------------------------------
    # Line 4 – altitude / speed
    # ------------------------------------------------------------------

    if altitude is not None:

        altitude_km = altitude / 1000

        alt_text = (
            f"ALT {altitude_km:.1f}km"
        )

    else:

        alt_text = "ALT ?"

    if speed is not None:

        speed_text = (
            f"SPD {speed}"
        )

    else:

        speed_text = "SPD ?"

    draw.text(
        (1, 24),
        alt_text,
        font=FONT_SMALL,
        fill=YELLOW
    )

    draw.text(
        (34, 24),
        speed_text,
        font=FONT_SMALL,
        fill=YELLOW
    )

    # ------------------------------------------------------------------
    # Distance/direction
    #
    # This is drawn at the bottom-right corner.
    # ------------------------------------------------------------------

    if distance is not None:

        distance_text = (
            f"{distance:g}km {compass}"
        )

        bbox = draw.textbbox(
            (0, 0),
            distance_text,
            font=FONT_SMALL
        )

        text_width = (
            bbox[2] - bbox[0]
        )

        draw.text(
            (
                max(
                    1,
                    MATRIX_WIDTH
                    - text_width
                    - 1
                ),
                17
            ),
            distance_text,
            font=FONT_SMALL,
            fill=RED
        )

    matrix.SetImage(image)


# ============================================================================
# MAIN
# ============================================================================

def main():

    print(
        "Starting 64x32 Athens Flight Radar..."
    )

    print(
        "Press Ctrl+C to quit."
    )

    matrix = create_matrix()

    try:

        while True:

            timestamp = time.strftime(
                "%H:%M:%S"
            )

            print(
                f"[{timestamp}] "
                "Fetching aircraft..."
            )

            raw_list = fetch_raw_aircraft()

            if raw_list is None:

                draw_flight_card(
                    matrix,
                    None,
                    0,
                    0
                )

            else:

                aircraft, total_scanned, displayable_count = (
                    select_one_aircraft(
                        raw_list
                    )
                )

                if aircraft:

                    print(
                        f"Selected: "
                        f"{aircraft['callsign']} "
                        f"({aircraft['airline']}) "
                        f"{aircraft['origin']} -> "
                        f"{aircraft['destination']}"
                    )

                else:

                    print(
                        "No suitable aircraft found."
                    )

                draw_flight_card(
                    matrix,
                    aircraft,
                    total_scanned,
                    displayable_count
                )

            print(
                f"Next refresh in "
                f"{POLL_INTERVAL} seconds."
            )

            time.sleep(
                POLL_INTERVAL
            )

    except KeyboardInterrupt:

        print(
            "\nStopping radar..."
        )

    finally:

        matrix.Clear()


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    main()
