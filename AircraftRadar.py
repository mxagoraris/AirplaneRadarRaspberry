"""
Raspberry Pi Flight Radar
64x32 HUB75 LED matrix

Displays one randomly selected aircraft near Athens.

Display:
    RYR8944 - Ryanair
    BOH -> CHQ
    ALT 35000m

An aircraft is displayed ONLY if:
    - It has a valid callsign
    - Its airline is in AIRLINE_LOOKUP
    - It is airborne
    - Altitude is available
    - Speed is available
    - Track is available
    - A valid origin AND destination are found

Route data:
    1. adsbdb.com
    2. hexdb.io

Dependencies:
    pip install requests pillow
    rgbmatrix installed from rpi-rgb-led-matrix
"""

import math
import time
import re
import random
import requests

from rgbmatrix import RGBMatrix, RGBMatrixOptions
from PIL import Image, ImageDraw, ImageFont


# ===========================================================================
# DISPLAY
# ===========================================================================

DISPLAY_WIDTH = 64
DISPLAY_HEIGHT = 32

options = RGBMatrixOptions()
options.rows = 32
options.cols = 64
options.chain_length = 1
options.parallel = 1
options.gpio_slowdown = 2

matrix = RGBMatrix(options=options)


# Fonts
FONT_SMALL = ImageFont.truetype(
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    8
)

FONT_MEDIUM = ImageFont.truetype(
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    10
)


# ===========================================================================
# OBSERVER LOCATION – ATHENS
# ===========================================================================

OBSERVER_LAT = 37.940256
OBSERVER_LON = 23.742944


# ===========================================================================
# BOUNDING BOX
# ===========================================================================

BBOX_DELTA = 0.7

LAT_MIN = OBSERVER_LAT - BBOX_DELTA
LAT_MAX = OBSERVER_LAT + BBOX_DELTA

LON_MIN = OBSERVER_LON - BBOX_DELTA
LON_MAX = OBSERVER_LON + BBOX_DELTA


# ===========================================================================
# POLLING
# ===========================================================================

POLL_INTERVAL = 60


# ===========================================================================
# OPENSKY
# ===========================================================================

OPENSKY_URL = "https://opensky-network.org/api/states/all"

REQUEST_TIMEOUT = 10


# ===========================================================================
# AIRLINE LOOKUP
# ===========================================================================

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

    # North America
    "UAL": "United Airlines",
    "DAL": "Delta Air Lines",
    "AAL": "American Airlines",
    "WJA": "WestJet",
    "ACA": "Air Canada",

    # Asian
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


# ===========================================================================
# GEOMETRY
# ===========================================================================

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


# ===========================================================================
# CALLSIGN / AIRLINE
# ===========================================================================

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


# ===========================================================================
# OPENSKY STATE VECTOR INDICES
# ===========================================================================

IDX_ICAO24 = 0
IDX_CALLSIGN = 1
IDX_ORIGIN_COUNTRY = 2
IDX_LONGITUDE = 5
IDX_LATITUDE = 6
IDX_BARO_ALTITUDE = 7
IDX_ON_GROUND = 8
IDX_VELOCITY = 9
IDX_TRUE_TRACK = 10


# ===========================================================================
# ROUTE LOOKUP
# ===========================================================================

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
        )

        raw_route = raw_route.strip()

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

    entry = ROUTE_CACHE.get(
        callsign
    )

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

        return None

    entry = ROUTE_CACHE.get(
        callsign
    )

    if entry is not None:

        if entry[0] is not None:

            return entry

        _, cached_at = entry

        if (
            time.time() - cached_at
        ) < ROUTE_UNKNOWN_TTL:

            return None

    # Try ADSBDB first
    result = _query_adsbdb(
        callsign
    )

    # Then HexDB
    if result is None:

        result = _query_hexdb(
            callsign
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

    return None


# ===========================================================================
# COMPLETENESS
# ===========================================================================

def is_complete(aircraft):

    callsign = aircraft.get(
        "callsign",
        ""
    )

    if (
        not callsign
        or callsign.upper() == "N/A"
    ):
        return False

    # Airline MUST be known
    if aircraft.get(
        "airline"
    ) == UNKNOWN_AIRLINE:

        return False

    # Route MUST exist
    if not aircraft.get(
        "origin"
    ):
        return False

    if not aircraft.get(
        "destination"
    ):
        return False

    # No unknown route values
    if aircraft["origin"] == "?":
        return False

    if aircraft["destination"] == "?":
        return False

    # Altitude
    if aircraft.get(
        "baro_altitude"
    ) is None:

        return False

    # Speed
    if aircraft.get(
        "velocity_kmh"
    ) is None:

        return False

    # Track
    if aircraft.get(
        "true_track"
    ) is None:

        return False

    # Must be airborne
    if aircraft.get(
        "on_ground",
        True
    ):

        return False

    # Distance
    if aircraft.get(
        "distance_km"
    ) is None:

        return False

    return True


# ===========================================================================
# FETCH OPENSKY
# ===========================================================================

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
                "OpenSky rate limited."
            )

            return None

        if response.status_code != 200:

            print(
                f"OpenSky HTTP {response.status_code}"
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

        if lat is None or lon is None:

            continue

        raw_callsign = state[
            IDX_CALLSIGN
        ]

        callsign = (
            raw_callsign.strip()
            if raw_callsign
            else "N/A"
        )

        if not callsign:

            callsign = "N/A"

        velocity_ms = state[
            IDX_VELOCITY
        ]

        velocity_kmh = (
            round(velocity_ms * 3.6)
            if velocity_ms is not None
            else None
        )

        distance = round(
            haversine_km(
                OBSERVER_LAT,
                OBSERVER_LON,
                lat,
                lon
            ),
            2
        )

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
                lookup_airline(
                    callsign
                ),
        })

    return aircraft_list


# ===========================================================================
# SELECT AIRCRAFT
# ===========================================================================

MAX_ROUTE_LOOKUPS_PER_CYCLE = 10


def select_one_aircraft(raw_list):

    candidates = []

    for aircraft in raw_list:

        callsign = aircraft.get(
            "callsign",
            ""
        )

        # ---------------------------------------------------------------
        # Callsign required
        # ---------------------------------------------------------------

        if (
            not callsign
            or callsign.upper() == "N/A"
        ):

            continue

        # ---------------------------------------------------------------
        # Airline MUST exist in lookup table
        # ---------------------------------------------------------------

        if aircraft.get(
            "airline"
        ) == UNKNOWN_AIRLINE:

            continue

        # ---------------------------------------------------------------
        # Required flight data
        # ---------------------------------------------------------------

        if aircraft.get(
            "baro_altitude"
        ) is None:

            continue

        if aircraft.get(
            "velocity_kmh"
        ) is None:

            continue

        if aircraft.get(
            "true_track"
        ) is None:

            continue

        # ---------------------------------------------------------------
        # Must be airborne
        # ---------------------------------------------------------------

        if aircraft.get(
            "on_ground",
            True
        ):

            continue

        candidates.append(
            aircraft
        )

    if not candidates:

        return None

    # Randomise aircraft
    random.shuffle(
        candidates
    )

    network_calls_used = 0

    for aircraft in candidates:

        callsign = aircraft[
            "callsign"
        ]

        cached = _is_cached(
            callsign
        )

        if not cached:

            if (
                network_calls_used
                >= MAX_ROUTE_LOOKUPS_PER_CYCLE
            ):

                continue

            network_calls_used += 1

        route = lookup_route(
            callsign
        )

        # ---------------------------------------------------------------
        # IMPORTANT:
        #
        # If route cannot be determined, DO NOT display the aircraft.
        # ---------------------------------------------------------------

        if route is None:

            continue

        origin, destination = route

        if (
            not origin
            or not destination
            or origin == "?"
            or destination == "?"
        ):

            continue

        aircraft["origin"] = origin
        aircraft["destination"] = destination

        if is_complete(
            aircraft
        ):

            return aircraft

    return None


# ===========================================================================
# LED DISPLAY HELPERS
# ===========================================================================

def centre_text(
    draw,
    text,
    font,
    y,
    fill=(255, 0, 0)
):

    bbox = draw.textbbox(
        (0, 0),
        text,
        font=font
    )

    text_width = (
        bbox[2] - bbox[0]
    )

    x = (
        DISPLAY_WIDTH - text_width
    ) // 2

    draw.text(
        (x, y),
        text,
        font=font,
        fill=fill
    )


def display_aircraft(
    aircraft
):

    image = Image.new(
        "RGB",
        (
            DISPLAY_WIDTH,
            DISPLAY_HEIGHT
        )
    )

    draw = ImageDraw.Draw(
        image
    )

    # ---------------------------------------------------------------
    # Callsign + airline
    # ---------------------------------------------------------------

    callsign = aircraft[
        "callsign"
    ]

    airline = aircraft[
        "airline"
    ]

    title = (
        f"{callsign} - {airline}"
    )

    # Keep title inside 64 pixels
    centre_text(
        draw,
        title,
        FONT_SMALL,
        1
    )

    # ---------------------------------------------------------------
    # Route
    # ---------------------------------------------------------------

    origin = aircraft[
        "origin"
    ]

    destination = aircraft[
        "destination"
    ]

    route = (
        f"{origin} -> {destination}"
    )

    centre_text(
        draw,
        route,
        FONT_MEDIUM,
        11
    )

    # ---------------------------------------------------------------
    # Altitude
    # ---------------------------------------------------------------

    altitude = aircraft[
        "baro_altitude"
    ]

    altitude_text = (
        f"ALT {int(altitude)}m"
    )

    centre_text(
        draw,
        altitude_text,
        FONT_MEDIUM,
        22
    )

    # Send image to LED panel
    matrix.SetImage(
        image
    )


def display_searching():

    image = Image.new(
        "RGB",
        (
            DISPLAY_WIDTH,
            DISPLAY_HEIGHT
        )
    )

    draw = ImageDraw.Draw(
        image
    )

    centre_text(
        draw,
        "SEARCHING",
        FONT_MEDIUM,
        11
    )

    centre_text(
        draw,
        "FOR FLIGHT",
        FONT_MEDIUM,
        22
    )

    matrix.SetImage(
        image
    )


def clear_display():

    matrix.Clear()


# ===========================================================================
# MAIN
# ===========================================================================

def main():

    print(
        "Aircraft Radar started."
    )

    print(
        "Display: 64x32"
    )

    print(
        f"Refresh: {POLL_INTERVAL}s"
    )

    try:

        while True:

            print(
                "\nFetching aircraft..."
            )

            raw_list = (
                fetch_raw_aircraft()
            )

            if raw_list is None:

                print(
                    "OpenSky fetch failed."
                )

                display_searching()

            else:

                print(
                    f"Aircraft found: "
                    f"{len(raw_list)}"
                )

                aircraft = (
                    select_one_aircraft(
                        raw_list
                    )
                )

                if aircraft is None:

                    print(
                        "No suitable aircraft "
                        "with a known route."
                    )

                    display_searching()

                else:

                    print(
                        f"Selected: "
                        f"{aircraft['callsign']} "
                        f"- "
                        f"{aircraft['airline']}"
                    )

                    print(
                        f"Route: "
                        f"{aircraft['origin']} "
                        f"-> "
                        f"{aircraft['destination']}"
                    )

                    print(
                        f"Altitude: "
                        f"{int(aircraft['baro_altitude'])}m"
                    )

                    display_aircraft(
                        aircraft
                    )

            time.sleep(
                POLL_INTERVAL
            )

    except KeyboardInterrupt:

        print(
            "\nStopping radar..."
        )

        clear_display()


# ===========================================================================
# ENTRY POINT
# ===========================================================================

if __name__ == "__main__":

    main()
