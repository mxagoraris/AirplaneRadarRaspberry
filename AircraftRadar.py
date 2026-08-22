"""
Raspberry Pi Flight Radar + 64x32 RGB LED Matrix

Polls the OpenSky Network for aircraft around Athens, Greece.

Selects ONE aircraft at random per refresh cycle, but ONLY when all
required information is available:

    - Valid callsign
    - Recognised airline
    - Aircraft airborne
    - Altitude available
    - Speed available
    - Heading available
    - Route origin available
    - Route destination available

Route information is retrieved from:
    1. ADSBDB
    2. HexDB (fallback)

Routes are cached in memory.

LED display:

    Line 1: RYR8944 - Ryanair
    Line 2: BOH -> CHQ
    Line 3: 10363m

Hardware:

    64x32 HUB75 RGB LED matrix
    Raspberry Pi
    GPIO slowdown = 2

Dependencies:

    pip install requests pillow

Run with:

    sudo /home/mxagoraris/aircraft-radar/venv/bin/python3 AircraftRadar.py
"""


import math
import time
import re
import random
import requests



# ===========================================================================
# RGB MATRIX IMPORT
# ===========================================================================

try:
    from rgbmatrix import RGBMatrix, RGBMatrixOptions, graphics
    from PIL import Image, ImageDraw, ImageFont

    RGB_AVAILABLE = True

except Exception as exc:

    RGB_AVAILABLE = False

    print(
        f"WARNING: RGB matrix libraries could not be imported: {exc}"
    )


# ===========================================================================
# OBSERVER LOCATION
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
# HTTPS / CERTIFICATE
# ===========================================================================

# Your Raspberry Pi's system CA bundle.
#
# This avoids the broken certifi path that was previously causing:
#
# "could not find suitable TLS CA certificate bundle"
#
CA_CERT_PATH = "/etc/ssl/certs/ca-certificates.crt"


# ===========================================================================
# OPENSKY
# ===========================================================================

OPENSKY_URL = (
    "https://opensky-network.org/api/states/all"
)

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

    # Cargo / miscellaneous
    "FDX": "FedEx",
    "UPS": "UPS Airlines",
    "BOX": "ASL Airlines",
    "BCS": "European Air Transport",
}

UNKNOWN_AIRLINE = "Unknown"


# ===========================================================================
# RGB MATRIX CONFIGURATION
# ===========================================================================

MATRIX_ROWS = 32
MATRIX_COLS = 64
MATRIX_GPIO_SLOWDOWN = 2

matrix = None


def initialise_matrix():

    global matrix

    if not RGB_AVAILABLE:
        print(
            "RGB matrix is unavailable."
        )
        return False

    try:

        options = RGBMatrixOptions()

        options.rows = MATRIX_ROWS
        options.cols = MATRIX_COLS

        # This matches the demo that worked for you.
        options.gpio_slowdown = MATRIX_GPIO_SLOWDOWN

        options.hardware_mapping = "regular"
        options.drop_privileges = False

        matrix = RGBMatrix(
            options=options
        )

        print(
            "RGB matrix initialised successfully."
        )

        return True

    except Exception as exc:

        print(
            f"LED DISPLAY ERROR during initialization: {exc}"
        )

        matrix = None

        return False


# ===========================================================================
# LED FONT
# ===========================================================================

# =========================================================================== 
# LED FONT
# ===========================================================================

BDF_FONT_PATH = (
    "/home/mxagoraris/aircraft-radar/rpi-rgb-led-matrix/"
    "fonts/4x6.bdf"
)


def load_font():

    import os

    print()
    print("=== FONT TEST INSIDE RADAR ===")

    print(f"Path: {BDF_FONT_PATH}")
    print(f"Exists: {os.path.exists(BDF_FONT_PATH)}")
    print(f"Readable: {os.access(BDF_FONT_PATH, os.R_OK)}")

    print("Creating Font object...")

    font = graphics.Font()

    print("Calling LoadFont...")

    font.LoadFont(BDF_FONT_PATH)

    print("SUCCESS: Font loaded inside AircraftRadar.py")

    return font


# ===========================================================================
# LED TEXT HELPERS
# ===========================================================================

def fit_font(text, maximum_width, starting_size=10):

    """
    Find the largest font that fits inside the matrix width.
    """

    for size in range(
        starting_size,
        3,
        -1
    ):

        font = load_font(size)

        dummy_image = Image.new(
            "RGB",
            (
                MATRIX_COLS,
                MATRIX_ROWS
            )
        )

        draw = ImageDraw.Draw(
            dummy_image
        )

        bbox = draw.textbbox(
            (0, 0),
            text,
            font=font
        )

        width = bbox[2] - bbox[0]

        if width <= maximum_width:

            return font

    return load_font(3)


# =========================================================================== 
# LED TEXT HELPERS
# ===========================================================================

def draw_centered_text(
    canvas,
    text,
    y,
    font,
    colour
):

    """
    Temporary diagnostic version.
    Draws text from the LEFT edge.
    """

    graphics.DrawText(
        canvas,
        font,
        0,
        y,
        colour,
        text
    )

# ===========================================================================
# LED DISPLAY
# ===========================================================================

def display_on_led(aircraft):

    """
    Render the selected aircraft directly onto
    the 64x32 RGB LED matrix using the 5x7 BDF font.

    Display:

        RYR8944 - Ryanair
        BOH -> CHQ
        10363m
    """

    if matrix is None:

        print(
            "LED DISPLAY ERROR: matrix is not initialized."
        )

        return

    if aircraft is None:

        print(
            "No aircraft to display on LED."
        )

        return

    try:

        # ---------------------------------------------------------------
        # Extract values
        # ---------------------------------------------------------------

        callsign = aircraft.get(
            "callsign",
            ""
        )

        airline = aircraft.get(
            "airline",
            ""
        )

        origin = aircraft.get(
            "origin",
            ""
        )

        destination = aircraft.get(
            "destination",
            ""
        )

        altitude = aircraft.get(
            "baro_altitude"
        )

        # ---------------------------------------------------------------
        # Safety check
        # ---------------------------------------------------------------

        if (
            not callsign
            or not airline
            or not origin
            or not destination
            or altitude is None
        ):

            print(
                "LED DISPLAY ERROR: incomplete aircraft data."
            )

            return

        # ---------------------------------------------------------------
        # Format text
        # ---------------------------------------------------------------

        line1 = "ABCDEFG"
        line2 = "ATHLHR"
        line3 = "123456"

        # ---------------------------------------------------------------
        # Load 5x7 BDF font
        # ---------------------------------------------------------------

        font = load_font()

        # ---------------------------------------------------------------
        # Create frame canvas
        # ---------------------------------------------------------------

        canvas = matrix.CreateFrameCanvas()

        # ---------------------------------------------------------------
        # Clear display
        # ---------------------------------------------------------------

        canvas.Clear()

        # ---------------------------------------------------------------
        # COLOURS
        #
        # RGB values:
        #       255, 0, 0   = red
        #       0, 255, 0   = green
        #       0, 0, 255   = blue
        #       255,255,255 = white
        # ---------------------------------------------------------------

        colour_line1 = graphics.Color(
            255,
            255,
            255
        )

        colour_line2 = graphics.Color(
            0,
            255,
            0
        )

        colour_line3 = graphics.Color(
            255,
            160,
            0
        )

        # ---------------------------------------------------------------
        # Draw line 1
        #
        # 5x7 font height is small, so baseline = 7
        # ---------------------------------------------------------------

        draw_centered_text(
            canvas,
            line1,
            7,
            font,
            colour_line1
        )

        # ---------------------------------------------------------------
        # Draw line 2
        # ---------------------------------------------------------------

        draw_centered_text(
            canvas,
            line2,
            19,
            font,
            colour_line2
        )

        # ---------------------------------------------------------------
        # Draw line 3
        # ---------------------------------------------------------------

        draw_centered_text(
            canvas,
            line3,
            31,
            font,
            colour_line3
        )

        # ---------------------------------------------------------------
        # Display frame
        # ---------------------------------------------------------------

        matrix.SwapOnVSync(
            canvas
        )

        print(
            "LED display updated."
        )

    except Exception as exc:

        print(
            f"LED DISPLAY ERROR: {exc}"
        )
    
        import traceback
    
        traceback.print_exc()


# ===========================================================================
# GEOMETRY
# ===========================================================================

def haversine_km(
    lat1,
    lon1,
    lat2,
    lon2
):

    R = 6371.0

    phi1 = math.radians(
        lat1
    )

    phi2 = math.radians(
        lat2
    )

    dphi = math.radians(
        lat2 - lat1
    )

    dlambda = math.radians(
        lon2 - lon1
    )

    a = (
        math.sin(dphi / 2) ** 2
        +
        math.cos(phi1)
        *
        math.cos(phi2)
        *
        math.sin(dlambda / 2) ** 2
    )

    return (
        R
        *
        2
        *
        math.atan2(
            math.sqrt(a),
            math.sqrt(1 - a)
        )
    )


def bearing_degrees(
    lat1,
    lon1,
    lat2,
    lon2
):

    phi1 = math.radians(
        lat1
    )

    phi2 = math.radians(
        lat2
    )

    dlambda = math.radians(
        lon2 - lon1
    )

    x = (
        math.sin(dlambda)
        *
        math.cos(phi2)
    )

    y = (
        math.cos(phi1)
        *
        math.sin(phi2)
        -
        math.sin(phi1)
        *
        math.cos(phi2)
        *
        math.cos(dlambda)
    )

    return (
        math.degrees(
            math.atan2(x, y)
        )
        + 360
    ) % 360


def bearing_to_compass(
    degrees
):

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


# ===========================================================================
# CALLSIGN / AIRLINE
# ===========================================================================

def extract_airline_prefix(
    callsign
):

    match = re.match(
        r"^([A-Za-z]+)",
        callsign or ""
    )

    return (
        match.group(1).upper()
        if match
        else ""
    )


def lookup_airline(
    callsign
):

    prefix = extract_airline_prefix(
        callsign
    )

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


# ===========================================================================
# ROUTE CACHE
# ===========================================================================

def _evict_oldest_cache_entry():

    if ROUTE_CACHE:

        oldest_key = next(
            iter(ROUTE_CACHE)
        )

        del ROUTE_CACHE[
            oldest_key
        ]


def _query_adsbdb(
    callsign
):

    try:

        url = ADSBDB_URL.format(
            callsign=callsign
        )

        response = requests.get(
            url,
            timeout=ROUTE_REQUEST_TIMEOUT,
            verify=CA_CERT_PATH
        )

        if response.status_code == 404:
            return None

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
            origin_data.get(
                "iata_code"
            )
            or origin_data.get(
                "icao_code"
            )
            or ""
        ).strip()

        destination = (
            destination_data.get(
                "iata_code"
            )
            or destination_data.get(
                "icao_code"
            )
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


def _query_hexdb(
    callsign
):

    try:

        url = HEXDB_URL.format(
            callsign=callsign
        )

        response = requests.get(
            url,
            timeout=ROUTE_REQUEST_TIMEOUT,
            verify=CA_CERT_PATH
        )

        if response.status_code == 404:
            return None

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
            segment.strip()
            for segment in raw_route.split("-")
            if segment.strip()
        ]

        if len(segments) < 2:
            return None

        return (
            segments[0],
            segments[-1]
        )

    except Exception:

        return None


def _is_cached(
    callsign
):

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


def lookup_route(
    callsign
):

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

        ROUTE_CACHE[
            callsign
        ] = result

        return result

    ROUTE_CACHE[
        callsign
    ] = (
        None,
        time.time()
    )

    return None


# ===========================================================================
# COMPLETENESS CHECK
# ===========================================================================

def is_complete(
    aircraft
):

    callsign = (
        aircraft
        .get("callsign", "")
        .strip()
    )

    if (
        not callsign
        or callsign.upper() == "N/A"
    ):

        return False

    if (
        aircraft.get("airline")
        == UNKNOWN_AIRLINE
    ):

        return False

    origin = aircraft.get(
        "origin"
    )

    destination = aircraft.get(
        "destination"
    )

    if not origin or not destination:

        return False

    if (
        origin == "?"
        or destination == "?"
    ):

        return False

    if (
        aircraft.get(
            "baro_altitude"
        )
        is None
    ):

        return False

    if (
        aircraft.get(
            "velocity_kmh"
        )
        is None
    ):

        return False

    if (
        aircraft.get(
            "true_track"
        )
        is None
    ):

        return False

    if aircraft.get(
        "on_ground",
        True
    ):

        return False

    if (
        aircraft.get(
            "distance_km"
        )
        is None
    ):

        return False

    return True


# ===========================================================================
# OPENSKY FETCH
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
            timeout=REQUEST_TIMEOUT,
            verify=CA_CERT_PATH
        )

        if response.status_code == 429:

            print(
                "OpenSky rate limit (HTTP 429)."
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

    states = (
        data.get("states")
        or []
    )

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

        # ---------------------------------------------------------------
        # Callsign
        # ---------------------------------------------------------------

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

        # ---------------------------------------------------------------
        # Speed
        # ---------------------------------------------------------------

        velocity_ms = state[
            IDX_VELOCITY
        ]

        if velocity_ms is not None:

            velocity_kmh = round(
                velocity_ms * 3.6
            )

        else:

            velocity_kmh = None

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

        # ---------------------------------------------------------------
        # Aircraft object
        # ---------------------------------------------------------------

        aircraft_list.append({

            "icao24":
                state[IDX_ICAO24],

            "callsign":
                callsign,

            "origin_country":
                state[
                    IDX_ORIGIN_COUNTRY
                ],

            "latitude":
                lat,

            "longitude":
                lon,

            "baro_altitude":
                state[
                    IDX_BARO_ALTITUDE
                ],

            "velocity_kmh":
                velocity_kmh,

            "true_track":
                state[
                    IDX_TRUE_TRACK
                ],

            "on_ground":
                state[
                    IDX_ON_GROUND
                ],

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
# AIRCRAFT SELECTION
# ===========================================================================

MAX_ROUTE_LOOKUPS_PER_CYCLE = 10


def select_one_aircraft(
    raw_list
):

    total_scanned = len(
        raw_list
    )

    # ---------------------------------------------------------------
    # Cheap filtering
    # ---------------------------------------------------------------

    candidates = []

    for aircraft in raw_list:

        callsign = (
            aircraft
            .get("callsign", "")
            .strip()
        )

        if (
            not callsign
            or callsign.upper() == "N/A"
        ):

            continue

        if (
            aircraft.get("airline")
            == UNKNOWN_AIRLINE
        ):

            continue

        if (
            aircraft.get(
                "baro_altitude"
            )
            is None
        ):

            continue

        if (
            aircraft.get(
                "velocity_kmh"
            )
            is None
        ):

            continue

        if (
            aircraft.get(
                "true_track"
            )
            is None
        ):

            continue

        if aircraft.get(
            "on_ground",
            True
        ):

            continue

        candidates.append(
            aircraft
        )

    displayable_count = len(
        candidates
    )

    if not candidates:

        return (
            None,
            total_scanned,
            displayable_count
        )

    # ---------------------------------------------------------------
    # Randomise
    # ---------------------------------------------------------------

    random.shuffle(
        candidates
    )

    # ---------------------------------------------------------------
    # Route lookup
    # ---------------------------------------------------------------

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

        # -----------------------------------------------------------
        # IMPORTANT:
        #
        # If ADSBDB + HexDB cannot give us a route,
        # completely discard this aircraft.
        #
        # Therefore '?' can NEVER reach the display.
        # -----------------------------------------------------------

        if route is None:

            continue

        origin, destination = route

        aircraft[
            "origin"
        ] = origin

        aircraft[
            "destination"
        ] = destination

        if is_complete(
            aircraft
        ):

            return (
                aircraft,
                total_scanned,
                displayable_count
            )

    return (
        None,
        total_scanned,
        displayable_count
    )


# ===========================================================================
# TERMINAL DISPLAY
# ===========================================================================

def print_flight_card(
    aircraft
):

    if aircraft is None:

        print()
        print(
            "No complete flight found."
        )
        print()

        return

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

    speed = aircraft[
        "velocity_kmh"
    ]

    distance = aircraft[
        "distance_km"
    ]

    compass = aircraft[
        "compass"
    ]

    print()

    print(
        f"{callsign} - {airline}"
    )

    print(
        f"{origin} -> {destination}"
    )

    print(
        f"{altitude:,}m  "
        f"{speed}km/h"
    )

    print(
        f"{distance}km "
        f"{compass}"
    )

    print()


# ===========================================================================
# MAIN
# ===========================================================================

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
        f"{POLL_INTERVAL} seconds"
    )

    print(
        "Press Ctrl+C to quit."
    )

    print()

    # -----------------------------------------------------------------------
    # Initialize LED matrix
    #
    # IMPORTANT:
    # This happens AFTER the initial information has been printed.
    # If it fails, we will see the error rather than getting a silent exit.
    # -----------------------------------------------------------------------

    print("=== TESTING FONT BEFORE MATRIX INITIALIZATION ===")
    test_font = load_font()
    print("=== FONT TEST FINISHED ===")

    initialise_matrix()

    print()

    while True:

        timestamp = time.strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        print(
            f"[{timestamp}] "
            "Fetching aircraft..."
        )

        # -------------------------------------------------------------------
        # OpenSky
        # -------------------------------------------------------------------

        raw_list = fetch_raw_aircraft()

        if raw_list is None:

            print(
                "Could not retrieve aircraft."
            )

        else:

            print(
                f"OpenSky returned "
                f"{len(raw_list)} aircraft."
            )

            # ---------------------------------------------------------------
            # Select
            # ---------------------------------------------------------------

            (
                aircraft,
                total_scanned,
                displayable_count
            ) = select_one_aircraft(
                raw_list
            )

            print(
                f"Candidates: "
                f"{displayable_count}"
            )

            # ---------------------------------------------------------------
            # TERMINAL OUTPUT FIRST
            #
            # This is deliberately before the LED call.
            # ---------------------------------------------------------------

            print_flight_card(
                aircraft
            )

            # ---------------------------------------------------------------
            # LED OUTPUT SECOND
            # ---------------------------------------------------------------

            if aircraft is not None:

                display_on_led(
                    aircraft
                )

            else:

                print(
                    "LED not updated because "
                    "no complete flight was found."
                )

        print(
            f"Next refresh in "
            f"{POLL_INTERVAL} seconds..."
        )

        print()

        time.sleep(
            POLL_INTERVAL
        )


# ===========================================================================
# ENTRY POINT
# ===========================================================================

if __name__ == "__main__":

    try:

        main()

    except KeyboardInterrupt:

        print(
            "\nStopped by user."
        )

        # ---------------------------------------------------------------
        # Clear matrix when exiting.
        # ---------------------------------------------------------------

        if matrix is not None:

            try:

                matrix.Clear()

            except Exception:

                pass
