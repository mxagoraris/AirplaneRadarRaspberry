#!/usr/bin/env python3
"""
Aircraft Radar for 64x32 RGB LED Matrix
Displays real-time flight data on Waveshare P2.5 LED Matrix
Uses PIL to generate images optimized for the display
"""

import math
import time
import re
import random
import requests
from PIL import Image, ImageDraw, ImageFont
import subprocess
import os

# ---------------------------------------------------------------------------
# Observer location – Athens, Greece
# ---------------------------------------------------------------------------
OBSERVER_LAT = 37.940256
OBSERVER_LON = 23.742944

BBOX_DELTA = 0.7
LAT_MIN = OBSERVER_LAT - BBOX_DELTA
LAT_MAX = OBSERVER_LAT + BBOX_DELTA
LON_MIN = OBSERVER_LON - BBOX_DELTA
LON_MAX = OBSERVER_LON + BBOX_DELTA

# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------
POLL_INTERVAL = 60  # seconds

# ---------------------------------------------------------------------------
# OpenSky Network API
# ---------------------------------------------------------------------------
OPENSKY_URL = "https://opensky-network.org/api/states/all"
REQUEST_TIMEOUT = 10

# ---------------------------------------------------------------------------
# Airline lookup table (shortened names for display)
# ---------------------------------------------------------------------------
AIRLINE_LOOKUP = {
    "AEE": "Aegean",
    "OAL": "Olympic",
    "SKY": "Sky Express",
    "SEH": "Sky Express",
    "RYR": "Ryanair",
    "EZY": "easyJet",
    "EZS": "easyJet",
    "EJU": "easyJet",
    "WZZ": "Wizz Air",
    "VLG": "Vueling",
    "IBE": "Iberia",
    "BEL": "Brussels",
    "SWR": "Swiss",
    "AUA": "Austrian",
    "EIN": "Aer Lingus",
    "OCN": "Eurowings",
    "BAW": "British Air",
    "DLH": "Lufthansa",
    "AFR": "Air France",
    "KLM": "KLM",
    "TAP": "TAP",
    "THY": "Turkish",
    "TOM": "TUI",
    "FIN": "Finnair",
    "SAS": "SAS",
    "UAE": "Emirates",
    "QTR": "Qatar",
    "ETH": "Ethiopian",
    "ETD": "Etihad",
    "MSR": "EgyptAir",
    "UAL": "United",
    "DAL": "Delta",
    "AAL": "American",
    "CPA": "Cathay",
    "SIA": "Singapore",
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
    """Try progressively shorter prefix slices against AIRLINE_LOOKUP."""
    prefix = extract_airline_prefix(callsign)
    for length in range(len(prefix), 0, -1):
        result = AIRLINE_LOOKUP.get(prefix[:length])
        if result:
            return result
    return UNKNOWN_AIRLINE


# ---------------------------------------------------------------------------
# OpenSky state-vector field indices
# ---------------------------------------------------------------------------
IDX_ICAO24         = 0
IDX_CALLSIGN       = 1
IDX_LONGITUDE      = 5
IDX_LATITUDE       = 6
IDX_BARO_ALTITUDE  = 7
IDX_ON_GROUND      = 8
IDX_VELOCITY       = 9
IDX_TRUE_TRACK     = 10

# ---------------------------------------------------------------------------
# Route lookup – cached
# ---------------------------------------------------------------------------

ADSBDB_URL         = "https://api.adsbdb.com/v0/callsign/{callsign}"
HEXDB_URL          = "https://hexdb.io/api/v1/route/icao/{callsign}"
ROUTE_REQUEST_TIMEOUT = 6
ROUTE_CACHE        = {}
ROUTE_CACHE_MAX_SIZE = 500
ROUTE_UNKNOWN_TTL  = 600


def _evict_oldest_cache_entry():
    """Remove the oldest entry from cache."""
    if ROUTE_CACHE:
        oldest_key = next(iter(ROUTE_CACHE))
        del ROUTE_CACHE[oldest_key]


def _query_adsbdb(callsign):
    """Query adsbdb.com for route data."""
    try:
        url = ADSBDB_URL.format(callsign=callsign)
        response = requests.get(url, timeout=ROUTE_REQUEST_TIMEOUT)
        if response.status_code != 200:
            return None
        data = response.json()
        route = data.get("response", {}).get("flightroute", {})
        origin_data = route.get("origin", {})
        destination_data = route.get("destination", {})
        origin = (origin_data.get("iata_code") or origin_data.get("icao_code") or "").strip()
        destination = (destination_data.get("iata_code") or destination_data.get("icao_code") or "").strip()
        if origin and destination:
            return (origin, destination)
        return None
    except:
        return None


def _query_hexdb(callsign):
    """Query hexdb.io for route data."""
    try:
        url = HEXDB_URL.format(callsign=callsign)
        response = requests.get(url, timeout=ROUTE_REQUEST_TIMEOUT)
        if response.status_code != 200:
            return None
        data = response.json()
        raw_route = data.get("route") or data.get("flightroute") or ""
        raw_route = raw_route.strip()
        if not raw_route:
            return None
        segments = [s.strip() for s in raw_route.split("-") if s.strip()]
        if len(segments) < 2:
            return None
        return (segments[0], segments[-1])
    except:
        return None


def _is_cached(callsign):
    """Return True if callsign is in cache."""
    entry = ROUTE_CACHE.get(callsign)
    if entry is None:
        return False
    if isinstance(entry, tuple) and len(entry) == 2 and entry[0] is not None:
        return True
    if isinstance(entry, tuple) and len(entry) == 2 and entry[0] is None:
        _, cached_at = entry
        return (time.time() - cached_at) < ROUTE_UNKNOWN_TTL
    return False


def lookup_route(callsign):
    """Return (origin, destination) for callsign."""
    if not callsign or callsign.strip().upper() == "N/A":
        return ("?", "?")

    entry = ROUTE_CACHE.get(callsign)
    if entry is not None:
        if entry[0] is not None:
            return entry
        _, cached_at = entry
        if (time.time() - cached_at) < ROUTE_UNKNOWN_TTL:
            return ("?", "?")

    result = _query_adsbdb(callsign) or _query_hexdb(callsign)

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
    """Return True when all fields required for display are present."""
    cs = aircraft.get("callsign", "")
    if not cs or cs.strip().upper() == "N/A":
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
    """Poll the OpenSky Network for state vectors inside bounding box."""
    params = {
        "lamin": LAT_MIN,
        "lomin": LON_MIN,
        "lamax": LAT_MAX,
        "lomax": LON_MAX,
    }
    try:
        response = requests.get(OPENSKY_URL, params=params, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            return None
        data = response.json()
    except:
        return None

    states = data.get("states") or []
    aircraft_list = []

    for state in states:
        lat = state[IDX_LATITUDE]
        lon = state[IDX_LONGITUDE]
        if lat is None or lon is None:
            continue

        raw_callsign = state[IDX_CALLSIGN]
        callsign = (raw_callsign.strip() if raw_callsign else "N/A") or "N/A"

        velocity_ms = state[IDX_VELOCITY]
        velocity_kmh = round(velocity_ms * 3.6) if velocity_ms is not None else None

        dist = round(haversine_km(OBSERVER_LAT, OBSERVER_LON, lat, lon), 1)
        brng = round(bearing_degrees(OBSERVER_LAT, OBSERVER_LON, lat, lon), 1)

        aircraft_list.append({
            "icao24":        state[IDX_ICAO24],
            "callsign":      callsign,
            "latitude":      lat,
            "longitude":     lon,
            "baro_altitude": state[IDX_BARO_ALTITUDE],
            "velocity_kmh":  velocity_kmh,
            "true_track":    state[IDX_TRUE_TRACK],
            "on_ground":     state[IDX_ON_GROUND],
            "distance_km":   dist,
            "bearing":       brng,
            "compass":       bearing_to_compass(brng),
            "airline":       lookup_airline(callsign),
        })

    return aircraft_list


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

MAX_ROUTE_LOOKUPS_PER_CYCLE = 10


def select_one_aircraft(raw_list):
    """Choose one displayable aircraft at random."""
    total_scanned = len(raw_list)

    candidates = []
    for ac in raw_list:
        cs = ac.get("callsign", "")
        if not cs or cs.strip().upper() == "N/A":
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

    displayable_count = len(candidates)

    if not candidates:
        return (None, total_scanned, displayable_count)

    random.shuffle(candidates)

    network_calls_used = 0

    for ac in candidates:
        cs = ac["callsign"]
        cached = _is_cached(cs)

        if not cached:
            if network_calls_used >= MAX_ROUTE_LOOKUPS_PER_CYCLE:
                continue
            network_calls_used += 1

        origin, destination = lookup_route(cs)
        ac["origin"]      = origin
        ac["destination"] = destination

        if is_complete(ac):
            return (ac, total_scanned, displayable_count)

    return (None, total_scanned, displayable_count)


# ---------------------------------------------------------------------------
# Image Generation for 64x32 LED Matrix
# ---------------------------------------------------------------------------

def create_flight_image(aircraft):
    """Create a 64x32 PIL image with flight info."""
    image = Image.new("RGB", (64, 32), color=(0, 0, 0))
    draw = ImageDraw.Draw(image)
    
    # Try to load fonts
    try:
        font_tiny = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 5)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 6)
        font_medium = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 7)
    except:
        font_tiny = font_small = font_medium = ImageFont.load_default()

    if aircraft is None:
        # Searching state
        draw.text((2, 2), "Searching...", fill=(255, 0, 0), font=font_small)
        draw.text((2, 10), "Athens", fill=(0, 255, 0), font=font_small)
        draw.text((2, 20), time.strftime("%H:%M"), fill=(100, 100, 100), font=font_tiny)
    else:
        cs        = aircraft.get("callsign", "N/A")[:8]
        airline   = aircraft.get("airline", "Unknown")[:10]
        origin    = aircraft.get("origin", "?")
        dest      = aircraft.get("destination", "?")
        alt       = aircraft.get("baro_altitude")
        spd       = aircraft.get("velocity_kmh")
        compass   = aircraft.get("compass", "?")

        alt_str  = f"{int(alt)//100}00m" if alt is not None else "?m"
        spd_str  = f"{spd}kph" if spd is not None else "?"

        # Line 1: Callsign (yellow)
        draw.text((1, 1), cs, fill=(255, 255, 0), font=font_medium)
        
        # Line 2: Airline (green)
        draw.text((1, 9), airline, fill=(0, 255, 0), font=font_small)
        
        # Line 3: Route (cyan)
        route_str = f"{origin}-{dest}"
        draw.text((1, 15), route_str, fill=(0, 200, 255), font=font_small)
        
        # Line 4: Alt/Speed (orange)
        draw.text((1, 21), f"A:{alt_str}", fill=(255, 150, 0), font=font_tiny)
        draw.text((25, 21), f"S:{spd_str}", fill=(255, 150, 0), font=font_tiny)
        draw.text((48, 21), compass, fill=(255, 100, 200), font=font_small)

    return image


# ---------------------------------------------------------------------------
# Display on LED Matrix
# ---------------------------------------------------------------------------

def display_image_on_matrix(image):
    """
    Display PIL image on LED matrix using the C++ binary.
    Saves image as PPM and pipes to the display demo.
    """
    try:
        # Save image as PPM format (supported by the C++ binary)
        ppm_path = "/tmp/flight_display.ppm"
        image.save(ppm_path, "PPM")
        
        # The C++ demo binary can display this
        # For now, just save it so we can verify it's being generated
        # Full LED integration would use the rpi-rgb-led-matrix library
        
        return True
    except Exception as e:
        print(f"Error displaying image: {e}")
        return False


# ---------------------------------------------------------------------------
# Main Loop
# ---------------------------------------------------------------------------

def main():
    """Main loop."""
    try:
        print("=" * 60)
        print("Aircraft Radar – Athens, Greece")
        print("LED Matrix Display (64x32)")
        print("=" * 60)
        print()

        cycle = 0
        while True:
            timestamp = time.strftime("%H:%M:%S")
            raw_list = fetch_raw_aircraft()

            if raw_list is None:
                print(f"[{timestamp}] API error – retrying...")
                image = create_flight_image(None)
            else:
                aircraft, total_scanned, displayable_count = select_one_aircraft(raw_list)
                image = create_flight_image(aircraft)
                
                if aircraft:
                    cs = aircraft.get("callsign", "?")
                    airline = aircraft.get("airline", "Unknown")
                    alt = aircraft.get("baro_altitude", "?")
                    spd = aircraft.get("velocity_kmh", "?")
                    dist = aircraft.get("distance_km", "?")
                    origin = aircraft.get("origin", "?")
                    dest = aircraft.get("destination", "?")
                    
                    print(f"[{timestamp}] {cs} – {airline}")
                    print(f"             {origin} → {dest} | Alt: {alt}m | Speed: {spd}km/h | Distance: {dist}km")
                else:
                    print(f"[{timestamp}] Scanned: {total_scanned} | Displayable: {displayable_count}")

            # Display on matrix
            display_image_on_matrix(image)

            cycle += 1
            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        print("\n" + "=" * 60)
        print("Stopped by user. Goodbye! ✈")
        print("=" * 60)
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
