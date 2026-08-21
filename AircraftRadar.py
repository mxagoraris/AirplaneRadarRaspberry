"""
Raspberry Pi Flight Radar

Polls the OpenSky Network for aircraft around Athens, Greece.

Selects ONE aircraft at random per refresh cycle, but ONLY when all
required display information is available:

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

The LED display target is a 64x32 RGB LED matrix.

Dependencies:
    pip install requests

Usage:
    python3 AircraftRadar.py
"""

import math
import time
import re
import random
import requests


# ===========================================================================
# OBSERVER LOCATION
# ===========================================================================

OBSERVER_LAT = 37.940256
OBSERVER_LON = 23.742944


# ===========================================================================
# BOUNDING BOX
# ===========================================================================

# Roughly ±77 km around observer
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
# GEOMETRY
# ===========================================================================

def haversine_km(lat1, lon1, lat2, lon2):
    """
    Return the great-circle distance in kilometres between two points.
    """

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
    """
    Return initial bearing in degrees (0–360).
    """

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
    """
    Convert bearing to 8-point compass direction.
    """

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
# CALLSIGN / AIRLINE HELPERS
# ===========================================================================

def extract_airline_prefix(callsign):
    """
    Extract alphabetic prefix from callsign.

    Example:
        RYR8944 -> RYR
        AEE123  -> AEE
    """

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
    """
    Find airline from callsign prefix.

    Tries progressively shorter prefixes.
    """

    prefix = extract_airline_prefix(callsign)

    for length in range(len(prefix), 0, -1):

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

# callsign -> (origin, destination)
#
# Failed lookups:
# callsign -> (None, timestamp)
ROUTE_CACHE = {}

ROUTE_CACHE_MAX_SIZE = 500

ROUTE_UNKNOWN_TTL = 600


# ===========================================================================
# ROUTE CACHE
# ===========================================================================

def _evict_oldest_cache_entry():
    """
    Remove oldest cache entry.
    """

    if ROUTE_CACHE:

        oldest_key = next(
            iter(ROUTE_CACHE)
        )

        del ROUTE_CACHE[oldest_key]


def _query_adsbdb(callsign):
    """
    Query ADSBDB for flight route.

    Returns:

        (origin, destination)

    or:

        None
    """

    try:

        url = ADSBDB_URL.format(
            callsign=callsign
        )

        response = requests.get(
            url,
            timeout=ROUTE_REQUEST_TIMEOUT
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
    """
    Query HexDB for flight route.

    Supports routes such as:

        LHR-ATH

        EGLL-LGAV-LFPG

    In the second case, the first and last airports
    are used.
    """

    try:

        url = HEXDB_URL.format(
            callsign=callsign
        )

        response = requests.get(
            url,
            timeout=ROUTE_REQUEST_TIMEOUT
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


def _is_cached(callsign):
    """
    Check whether route information is already
    available without making a network request.
    """

    entry = ROUTE_CACHE.get(
        callsign
    )

    if entry is None:
        return False

    # Successful route
    if entry[0] is not None:
        return True

    # Failed route
    _, cached_at = entry

    return (
        time.time() - cached_at
    ) < ROUTE_UNKNOWN_TTL


def lookup_route(callsign):
    """
    Retrieve route using cache + ADSBDB + HexDB.
    """

    if (
        not callsign
        or callsign.strip().upper() == "N/A"
    ):
        return None

    entry = ROUTE_CACHE.get(
        callsign
    )

    if entry is not None:

        # Successful cache entry
        if entry[0] is not None:
            return entry

        # Failed lookup still within TTL
        _, cached_at = entry

        if (
            time.time() - cached_at
        ) < ROUTE_UNKNOWN_TTL:

            return None

    # ---------------------------------------------------------------
    # Try ADSBDB first
    # ---------------------------------------------------------------

    result = _query_adsbdb(
        callsign
    )

    # ---------------------------------------------------------------
    # Try HexDB if ADSBDB failed
    # ---------------------------------------------------------------

    if result is None:

        result = _query_hexdb(
            callsign
        )

    # ---------------------------------------------------------------
    # Cache result
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


# ===========================================================================
# COMPLETENESS CHECK
# ===========================================================================

def is_complete(aircraft):
    """
    Return True ONLY when the aircraft contains
    everything required for the LED display.

    In particular:

        origin != ?
        destination != ?
    """

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

    # Airline must be recognised
    if (
        aircraft.get("airline")
        == UNKNOWN_AIRLINE
    ):
        return False

    # Route must exist
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

    # Flight data
    if (
        aircraft.get("baro_altitude")
        is None
    ):
        return False

    if (
        aircraft.get("velocity_kmh")
        is None
    ):
        return False

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

    # Distance must exist
    if (
        aircraft.get("distance_km")
        is None
    ):
        return False

    return True


# ===========================================================================
# OPENSKY FETCH
# ===========================================================================

def fetch_raw_aircraft():
    """
    Fetch all aircraft inside the bounding box.

    No route requests are made here.
    """

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

        # ---------------------------------------------------------------
        # Position
        # ---------------------------------------------------------------

        lat = state[IDX_LATITUDE]

        lon = state[IDX_LONGITUDE]

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
# AIRCRAFT SELECTION
# ===========================================================================

MAX_ROUTE_LOOKUPS_PER_CYCLE = 10


def select_one_aircraft(raw_list):
    """
    Select one completely valid aircraft at random.

    IMPORTANT:

    We DO NOT select an aircraft merely because OpenSky
    knows about it.

    The aircraft must also have a valid route from ADSBDB
    or HexDB.

    Aircraft with unknown routes are discarded.
    """

    total_scanned = len(
        raw_list
    )

    # ---------------------------------------------------------------
    # Stage 1
    #
    # Cheap filtering before any route API calls.
    # ---------------------------------------------------------------

    candidates = []

    for aircraft in raw_list:

        callsign = (
            aircraft
            .get("callsign", "")
            .strip()
        )

        # No callsign
        if (
            not callsign
            or callsign.upper() == "N/A"
        ):
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
    # Stage 2
    #
    # Randomise candidates.
    # ---------------------------------------------------------------

    random.shuffle(
        candidates
    )

    # ---------------------------------------------------------------
    # Stage 3
    #
    # Resolve routes.
    # ---------------------------------------------------------------

    network_calls_used = 0

    for aircraft in candidates:

        callsign = aircraft[
            "callsign"
        ]

        cached = _is_cached(
            callsign
        )

        # -----------------------------------------------------------
        # New route lookup
        # -----------------------------------------------------------

        if not cached:

            if (
                network_calls_used
                >= MAX_ROUTE_LOOKUPS_PER_CYCLE
            ):
                continue

            network_calls_used += 1

        # -----------------------------------------------------------
        # Get route
        # -----------------------------------------------------------

        route = lookup_route(
            callsign
        )

        if route is None:

            # IMPORTANT:
            #
            # Do NOT assign ("?", "?")
            #
            # Just reject this aircraft.

            continue

        origin, destination = route

        aircraft["origin"] = origin

        aircraft["destination"] = destination

        # -----------------------------------------------------------
        # Final validation
        # -----------------------------------------------------------

        if is_complete(
            aircraft
        ):

            return (
                aircraft,
                total_scanned,
                displayable_count
            )

    # Nothing valid found
    return (
        None,
        total_scanned,
        displayable_count
    )


# ===========================================================================
# TERMINAL DISPLAY
# ===========================================================================

def print_flight_card(aircraft):
    """
    Simple development output.

    This will later be replaced by the actual
    64x32 RGB matrix rendering.
    """

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

    while True:

        timestamp = time.strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        print(
            f"[{timestamp}] "
            "Fetching aircraft..."
        )

        # -----------------------------------------------------------
        # OpenSky
        # -----------------------------------------------------------

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

            # -------------------------------------------------------
            # Select
            # -------------------------------------------------------

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

            # -------------------------------------------------------
            # Display
            # -------------------------------------------------------

            print_flight_card(
                aircraft
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
