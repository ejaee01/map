"""
MyMap - Raspberry Pi 5 Edition (mainpi.py)
================================================================================
Turn-by-turn navigation adapted to run on a Raspberry Pi 5, built for a
touchscreen / car-dashboard style deployment.

WHAT'S DIFFERENT FROM THE DESKTOP VERSION
--------------------------------------------------------------------------------
- Runs natively on Raspberry Pi OS (Bookworm, 64-bit) on a Pi 5.
- Auto-scales fonts/buttons to whatever screen is attached (official 7"
  touchscreen, a car head unit, or a normal HDMI monitor) and can boot
  straight into fullscreen kiosk mode.
- Draws its own turn-arrow icons (straight, slight left/right, left/right,
  hard left/right, U-turn, roundabout, arrived) on a Tk canvas instead of
  loading external image files - nothing to download, nothing to break if
  the network is flaky, works the same on every screen size.
- Instead of a wall of direction buttons, the navigation screen shows only
  the ONE icon + instruction relevant to your NEXT light or turn. The route
  is pre-broken into a single ordered list of "events" - every turn AND
  every traffic light along the way, in the order you'll reach them - so
  tapping Next/Prev (or a live GPS fix) always advances exactly one event
  at a time and the map view re-centers on it every time.
- Optional live GPS auto-advance via gpsd, with automatic fallback to
  manual Next/Prev stepping if no GPS hardware/daemon is present.

RASPBERRY PI 5 SETUP
--------------------------------------------------------------------------------
1. Flash Raspberry Pi OS (64-bit, Bookworm) with Raspberry Pi Imager and boot
   to the desktop - this app needs a display (X11, or Wayland + XWayland).

2. System packages:
     sudo apt update
     sudo apt install -y python3-venv python3-tk python3-pil python3-pil.imagetk

3. (Recommended) Create a virtual environment - Bookworm's system Python is
   "externally managed" and will refuse a plain `pip install`:
     python3 -m venv ~/mymap-env
     source ~/mymap-env/bin/activate

4. Install Python dependencies. Raspberry Pi OS already points pip at
   piwheels (prebuilt ARM wheels), so this should be quick - no waiting on
   slow from-source builds:
     pip install requests geopy tkintermapview pillow

5. OPTIONAL - live GPS instead of manual Next/Prev stepping. Works with any
   USB/serial NMEA GPS receiver:
     sudo apt install -y gpsd gpsd-clients
     pip install gpsd-py3
     sudo gpsd /dev/ttyACM0 -F /var/run/gpsd.sock   # adjust device as needed
   If gpsd isn't running, or gpsd-py3 isn't installed, the app automatically
   falls back to manual mode - no code changes needed.

6. Run it:
     python3 mainpi.py                 # auto-detects screen size
     python3 mainpi.py --fullscreen    # force kiosk mode
     python3 mainpi.py --windowed      # force a normal window

7. OPTIONAL - launch on boot (e.g. a kiosk box mounted in a car): add a
   .desktop entry under ~/.config/autostart/ that runs
   `python3 /home/pi/mymap/mainpi.py --fullscreen`.

Press F11 to toggle fullscreen at any time, Escape to leave fullscreen.
================================================================================
"""

import argparse
import math
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import messagebox, ttk

import requests
from geopy.geocoders import Nominatim

try:
    import tkintermapview
    TKINTERMAPVIEW_AVAILABLE = True
except ImportError:
    tkintermapview = None
    TKINTERMAPVIEW_AVAILABLE = False

try:
    import gpsd  # gpsd-py3
    GPSD_MODULE_AVAILABLE = True
except ImportError:
    gpsd = None
    GPSD_MODULE_AVAILABLE = False


# ============================================================================
# DATA MODELS
# ============================================================================

@dataclass
class Location:
    """Geographic location."""
    name: str
    lat: float
    lon: float


@dataclass
class Step:
    """A navigation step with location details."""
    instruction: str
    distance_m: float
    duration_s: float
    lat: float
    lon: float
    road_name: str = ""
    maneuver_type: str = ""
    maneuver_modifier: str = ""


@dataclass
class Route:
    """Complete route with metrics."""
    route_type: str
    steps: List[Step]
    waypoints: List[Tuple[float, float]]
    distance_miles: float
    duration_seconds: float
    cost_usd: float
    computation_time_sec: float
    avg_speed_mph: float = 0.0
    traffic_signals: int = 0
    signal_delay_sec: float = 0.0
    turns: int = 0
    u_turns: int = 0
    turn_delay_sec: float = 0.0
    traffic_signal_points: List[Tuple[float, float]] = None

    def __post_init__(self):
        if self.traffic_signal_points is None:
            self.traffic_signal_points = []


@dataclass
class NavEvent:
    """One thing the driver will encounter next: a turn or a traffic light."""
    kind: str            # "turn" or "light"
    category: str        # icon category, see MANEUVER_LABELS
    lat: float
    lon: float
    road_name: str
    distance_m: float            # cumulative distance along the route
    step_index: Optional[int]    # originating Step index, if kind == "turn"
    instruction: str = ""


# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in meters."""
    R = 6371000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


def simplify_points(points: List[Tuple[float, float]], max_points: int = 120) -> List[Tuple[float, float]]:
    """Reduce a path to fewer points so map rendering stays responsive."""
    if len(points) <= max_points:
        return points
    step = max(len(points) // max_points, 1)
    simplified = points[::step]
    if simplified[-1] != points[-1]:
        simplified.append(points[-1])
    return simplified


def lighten_hex_color(color: str, amount: float = 0.55) -> str:
    """Blend a hex color toward white to create a softer overlay color."""
    color = color.lstrip("#")
    if len(color) != 6:
        return "#ffffff"
    try:
        r = int(color[0:2], 16)
        g = int(color[2:4], 16)
        b = int(color[4:6], 16)
    except ValueError:
        return "#ffffff"
    r = int(r + (255 - r) * amount)
    g = int(g + (255 - g) * amount)
    b = int(b + (255 - b) * amount)
    return f"#{r:02x}{g:02x}{b:02x}"


def route_zoom(distance_miles: float) -> int:
    """Choose a reasonable zoom level for a route distance."""
    if distance_miles < 5:
        return 15
    if distance_miles < 20:
        return 13
    if distance_miles < 60:
        return 11
    return 9


def get_road_view_params(step: Step, avg_speed: float = 30.0) -> Tuple[int, float]:
    """Determine zoom level and visible radius (miles) based on road type."""
    road_name = (step.road_name or "").lower()
    maneuver = (step.maneuver_type or "").lower()

    highway_keywords = ["interstate", "i-", "us highway", "us-", "route", "expressway",
                         "freeway", "highway", "turnpike", "parkway"]
    is_highway = any(keyword in road_name for keyword in highway_keywords)
    is_fast_road = avg_speed > 45
    is_ramp = "ramp" in maneuver

    if is_ramp:
        return 15, 0.5
    elif is_highway or (is_fast_road and avg_speed > 55):
        return 13, 3.5
    elif is_fast_road and avg_speed > 40:
        return 14, 2.5
    elif avg_speed > 25:
        return 14, 2.0
    else:
        return 15, 1.2


# ---- Maneuver classification -> a small, fixed set of icon categories ----

MANEUVER_LABELS: Dict[str, str] = {
    "straight": "Continue Straight",
    "slight_left": "Slight Left",
    "left": "Turn Left",
    "sharp_left": "Hard Left",
    "uturn": "Make a U-Turn",
    "slight_right": "Slight Right",
    "right": "Turn Right",
    "sharp_right": "Hard Right",
    "roundabout": "Enter Roundabout",
    "arrive": "You Have Arrived",
}

# Bend angle (degrees) used to draw each category's arrow. 0 = straight up,
# positive = bends right, negative = bends left.
ARROW_ANGLES: Dict[str, float] = {
    "straight": 0,
    "slight_left": -35,
    "left": -90,
    "sharp_left": -140,
    "slight_right": 35,
    "right": 90,
    "sharp_right": 140,
}


def classify_maneuver(maneuver_type: str, maneuver_modifier: str) -> str:
    """Map OSRM maneuver type/modifier onto one of our fixed icon categories."""
    m_type = (maneuver_type or "").lower()
    modifier = (maneuver_modifier or "").lower()

    if m_type == "uturn" or "u-turn" in modifier:
        return "uturn"
    if m_type == "arrive":
        return "arrive"
    if m_type in ("roundabout", "rotary"):
        return "roundabout"
    if m_type == "depart":
        if "left" in modifier:
            return "slight_left" if "slight" in modifier else "left"
        if "right" in modifier:
            return "slight_right" if "slight" in modifier else "right"
        return "straight"

    if "sharp left" in modifier:
        return "sharp_left"
    if "sharp right" in modifier:
        return "sharp_right"
    if "slight left" in modifier:
        return "slight_left"
    if "slight right" in modifier:
        return "slight_right"
    if modifier == "left":
        return "left"
    if modifier == "right":
        return "right"
    return "straight"


def build_maneuver_instruction(maneuver_type: str, maneuver_modifier: str, road_name: str) -> str:
    """Create a readable turn instruction from OSRM maneuver data."""
    road = f" onto {road_name}" if road_name else ""
    modifier = (maneuver_modifier or "").replace("_", " ")
    maneuver_type = (maneuver_type or "").lower()

    if maneuver_type == "depart":
        return f"Head {modifier}".strip() if modifier else "Depart"
    if maneuver_type == "arrive":
        return "You have arrived"
    if maneuver_type == "uturn":
        return f"Make a U-turn{road}"
    if maneuver_type == "turn":
        return f"Turn {modifier}{road}" if modifier else f"Turn{road}"
    if maneuver_type in {"merge", "fork", "roundabout", "rotary", "off ramp", "on ramp", "new name", "continue"}:
        return f"{maneuver_type.title()} {modifier}{road}" if modifier else f"{maneuver_type.title()}{road}"

    return f"Continue on {road_name}" if road_name else "Continue"


def generate_terse_instruction(step: Step) -> str:
    """Generate a terse, concise turn-by-turn instruction."""
    category = classify_maneuver(step.maneuver_type, step.maneuver_modifier)
    label = MANEUVER_LABELS.get(category, "Continue")
    if step.road_name:
        return f"{label} onto {step.road_name}"
    return label


# ============================================================================
# ARROW ICON DRAWING (pure Tk canvas - no image assets needed, works offline)
# ============================================================================

def draw_maneuver_icon(canvas: tk.Canvas, category: str, w: int, h: int, color: str = "#1a73e8") -> None:
    """Draw the arrow icon for a maneuver category onto a (cleared) canvas."""
    canvas.delete("all")
    if category == "uturn":
        _draw_uturn(canvas, w, h, color)
    elif category == "roundabout":
        _draw_roundabout(canvas, w, h, color)
    elif category == "arrive":
        _draw_arrive(canvas, w, h, color)
    else:
        angle = ARROW_ANGLES.get(category, 0)
        _draw_bent_arrow(canvas, angle, w, h, color)


def _draw_bent_arrow(canvas: tk.Canvas, angle_deg: float, w: int, h: int, color: str) -> None:
    """A stem coming up from the bottom that bends toward angle_deg and ends in an arrowhead."""
    cx = w / 2
    base_y = h * 0.90
    bend_y = h * 0.48
    rad = math.radians(angle_deg)
    length = h * 0.36
    end_x = cx + length * math.sin(rad)
    end_y = bend_y - length * math.cos(rad)

    width = max(10, int(w * 0.09))
    canvas.create_line(
        cx, base_y, cx, bend_y, end_x, end_y,
        smooth=True, splinesteps=24, fill=color, width=width,
        capstyle=tk.ROUND, joinstyle=tk.ROUND,
        arrow=tk.LAST, arrowshape=(int(w * 0.17), int(w * 0.21), int(w * 0.09))
    )


def _draw_uturn(canvas: tk.Canvas, w: int, h: int, color: str) -> None:
    """A loop: up, over the top, and back down, with the arrowhead pointing down."""
    cx = w / 2
    r = w * 0.16
    base_y = h * 0.90
    top_y = h * 0.34
    left_x = cx - r
    right_x = cx + r

    pts = [left_x, base_y, left_x, top_y + r]
    steps = 10
    for i in range(steps + 1):
        theta = math.pi * i / steps
        x = cx - r * math.cos(theta)
        y = top_y + r - r * math.sin(theta)
        pts.extend([x, y])
    pts.extend([right_x, base_y * 0.82])

    width = max(8, int(w * 0.08))
    canvas.create_line(
        *pts, smooth=True, splinesteps=24, fill=color, width=width,
        capstyle=tk.ROUND, joinstyle=tk.ROUND,
        arrow=tk.LAST, arrowshape=(int(w * 0.15), int(w * 0.19), int(w * 0.08))
    )


def _draw_roundabout(canvas: tk.Canvas, w: int, h: int, color: str) -> None:
    cx, cy = w / 2, h * 0.5
    r = w * 0.22
    canvas.create_oval(cx - r, cy - r, cx + r, cy + r, outline=color, width=max(6, int(w * 0.05)))
    _draw_bent_arrow(canvas, 70, w, h, color)


def _draw_arrive(canvas: tk.Canvas, w: int, h: int, color: str) -> None:
    cx, cy = w / 2, h / 2
    r = w * 0.26
    canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=color, outline=color)
    canvas.create_text(cx, cy, text="\u2691", font=("Arial", int(w * 0.24)), fill="white")


# ============================================================================
# GEOCODING / ROUTING (OSRM)
# ============================================================================

class GeoUtils:
    """Geocoding and location search via Nominatim, plus Overpass helper URL."""

    def __init__(self):
        self.geolocator = Nominatim(user_agent="mymap_pi")
        self.cache: Dict[str, object] = {}
        self.last_request_time = 0.0
        self.overpass_url = "https://overpass-api.de/api/interpreter"

    def _rate_limit(self):
        elapsed = time.time() - self.last_request_time
        if elapsed < 1.5:
            time.sleep(1.5 - elapsed)
        self.last_request_time = time.time()

    def geocode(self, address: str) -> Optional[Location]:
        if address in self.cache:
            return self.cache[address]
        clean_address = address.rsplit(" (", 1)[0] if " (" in address else address
        try:
            self._rate_limit()
            location = self.geolocator.geocode(clean_address, timeout=8)
            if location:
                result = Location(name=address, lat=location.latitude, lon=location.longitude)
                self.cache[address] = result
                return result
        except Exception as e:
            print(f"Geocoding error: {e}")
        return None

    def search_locations(self, query: str, limit: int = 5) -> List[str]:
        if len(query) < 2:
            return []
        cache_key = f"search_{query}"
        if cache_key in self.cache:
            return self.cache[cache_key]
        results: List[str] = []
        try:
            self._rate_limit()
            locations = self.geolocator.geocode(query, exactly_one=False, timeout=8)
            if locations:
                for loc in locations[:limit]:
                    results.append(loc.address)
        except Exception as e:
            print(f"Search error: {e}")
        self.cache[cache_key] = results[:limit]
        return self.cache[cache_key]


class OSRMRouter:
    """Routing via the public OSRM demo server."""

    OSRM_URL = "http://router.project-osrm.org/route/v1/driving"

    @staticmethod
    def get_route(start: Location, end: Location, alternatives: int = 0,
                   exclude: Optional[str] = None, waypoints: Optional[List[Location]] = None) -> Optional[Dict]:
        try:
            query_parts = [
                "steps=true", "overview=full", "geometries=geojson",
                "annotations=speed,duration,distance", "continue_straight=true",
            ]
            if alternatives:
                query_parts.append(f"alternatives={alternatives}")
            if exclude:
                query_parts.append(f"exclude={exclude}")

            coords = [f"{start.lon},{start.lat}"]
            for wp in waypoints or []:
                coords.append(f"{wp.lon},{wp.lat}")
            coords.append(f"{end.lon},{end.lat}")

            url = f"{OSRMRouter.OSRM_URL}/{';'.join(coords)}?{'&'.join(query_parts)}"
            response = requests.get(url, timeout=12)
            data = response.json()
            if data.get("code") == "Ok":
                return data
        except Exception as e:
            print(f"OSRM error: {e}")
        return None


class RoutingEngine:
    """Compute a route from OSRM and enrich it with turn/light metadata."""

    def __init__(self, geo_utils: GeoUtils):
        self.geo_utils = geo_utils
        self.mapquest_api_key = os.getenv("MAPQUEST_API_KEY") or os.getenv("MAPQUEST_KEY")

    def compute_route(self, start: Location, end: Location) -> Optional[Route]:
        start_time = time.time()
        data = OSRMRouter.get_route(start, end)
        if not data or not data.get("routes"):
            return None
        return self._build_route(data["routes"][0], start, end, start_time)

    def _build_route(self, osrm_route: Dict, start: Location, end: Location, start_time: float) -> Route:
        steps = self._parse_steps(osrm_route["legs"][0]["steps"])
        waypoints = self._extract_waypoints(osrm_route)
        if len(waypoints) < 2:
            waypoints = [(start.lat, start.lon), (end.lat, end.lon)]

        distance_miles = osrm_route["distance"] * 0.000621371
        duration_sec = osrm_route["duration"]
        avg_speed_mph = self._estimate_average_speed_mph(osrm_route)
        turn_count, u_turn_count, turn_delay_sec = self._estimate_turn_delay(steps)
        traffic_signal_points = self._fetch_traffic_signal_points(waypoints)
        traffic_signals = len(traffic_signal_points)
        signal_delay_sec = traffic_signals * 18.0

        return Route(
            route_type="Route",
            steps=steps,
            waypoints=waypoints,
            distance_miles=distance_miles,
            duration_seconds=duration_sec + signal_delay_sec + turn_delay_sec,
            cost_usd=0.0,
            computation_time_sec=time.time() - start_time,
            avg_speed_mph=avg_speed_mph,
            traffic_signals=traffic_signals,
            signal_delay_sec=signal_delay_sec,
            turns=turn_count,
            u_turns=u_turn_count,
            turn_delay_sec=turn_delay_sec,
            traffic_signal_points=traffic_signal_points,
        )

    def _extract_waypoints(self, osrm_route: Dict) -> List[Tuple[float, float]]:
        try:
            geometry = osrm_route.get("geometry", {})
            if isinstance(geometry, dict) and "coordinates" in geometry:
                return [(lat, lon) for lon, lat in geometry["coordinates"]]
            if isinstance(geometry, list):
                return [(lat, lon) for lon, lat in geometry]
        except Exception as e:
            print(f"Waypoint extraction error: {e}")
        return []

    def _parse_steps(self, osrm_steps: List[Dict]) -> List[Step]:
        steps = []
        for osrm_step in osrm_steps:
            distance = osrm_step.get("distance", 0)
            duration = osrm_step.get("duration", 0)
            location = osrm_step.get("intersections", [{}])[0]
            lat = location.get("location", [0, 0])[1]
            lon = location.get("location", [0, 0])[0]
            maneuver = osrm_step.get("maneuver", {})
            maneuver_type = maneuver.get("type", "")
            maneuver_modifier = maneuver.get("modifier", "")
            road_name = osrm_step.get("name", "")
            instruction = build_maneuver_instruction(maneuver_type, maneuver_modifier, road_name)
            steps.append(Step(
                instruction=instruction, distance_m=distance, duration_s=duration,
                lat=lat, lon=lon, road_name=road_name,
                maneuver_type=maneuver_type, maneuver_modifier=maneuver_modifier,
            ))
        return steps

    def _estimate_turn_delay(self, steps: List[Step]) -> Tuple[int, int, float]:
        turn_count = 0
        u_turn_count = 0
        delay = 0.0
        turn_like = {"turn", "merge", "fork", "roundabout", "rotary", "off ramp", "on ramp", "new name"}
        for step in steps:
            m_type = (step.maneuver_type or "").lower()
            modifier = (step.maneuver_modifier or "").lower()
            if m_type == "uturn":
                turn_count += 1
                u_turn_count += 1
                delay += 18.0
                continue
            if m_type in turn_like or modifier in {"left", "right", "slight left", "slight right", "sharp left", "sharp right"}:
                turn_count += 1
                if "sharp" in modifier:
                    delay += 7.0
                elif "slight" in modifier:
                    delay += 3.0
                elif modifier in {"left", "right"}:
                    delay += 4.0
                else:
                    delay += 2.0
        return turn_count, u_turn_count, delay

    def _estimate_average_speed_mph(self, osrm_route: Dict) -> float:
        try:
            speeds = osrm_route.get("legs", [{}])[0].get("annotation", {}).get("speed", [])
            valid = [s for s in speeds if isinstance(s, (int, float)) and s > 0]
            if valid:
                return round(sum(valid) / len(valid) * 2.23694, 1)
        except Exception as e:
            print(f"Speed estimation error: {e}")
        return 0.0

    def _fetch_traffic_signal_points(self, waypoints: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
        if len(waypoints) < 2:
            return []
        lats = [p[0] for p in waypoints]
        lons = [p[1] for p in waypoints]
        south, north, west, east = min(lats), max(lats), min(lons), max(lons)
        overpass_query = f"""
        [out:json][timeout:8];
        (node["highway"="traffic_signals"]({south},{west},{north},{east}););
        out body;
        """
        try:
            response = requests.post(self.geo_utils.overpass_url, data=overpass_query, timeout=10)
            if response.status_code == 200:
                elements = response.json().get("elements", [])
                points = []
                for el in elements:
                    lat = el.get("lat") or el.get("center", {}).get("lat")
                    lon = el.get("lon") or el.get("center", {}).get("lon")
                    if lat is not None and lon is not None:
                        points.append((lat, lon))
                return points
        except Exception as e:
            print(f"Traffic signal lookup error: {e}")
        return []


class FuelCalculator:
    """Rough trip cost estimate (fuel + wear)."""

    FUEL_PRICE_PER_GALLON = 3.50
    WEAR_COST_PER_MILE = 0.165

    def total_trip_cost(self, distance_miles: float, mpg: float) -> Dict[str, float]:
        mpg = mpg if mpg and mpg > 0 else 25.0
        fuel = round((distance_miles / mpg) * self.FUEL_PRICE_PER_GALLON, 2)
        wear = round(distance_miles * self.WEAR_COST_PER_MILE, 2)
        return {"fuel": fuel, "wear": wear, "total": round(fuel + wear, 2)}


# ============================================================================
# NAV EVENTS: merge every turn + every traffic light into one ordered list
# ============================================================================

def build_nav_events(route: Route) -> List[NavEvent]:
    """
    Turn a route's steps and traffic-signal points into a single list, in the
    order the driver will reach them, so navigation can step through "every
    light" (and every turn) one at a time.
    """
    waypoints = route.waypoints
    if not waypoints:
        return []

    cum = [0.0]
    for i in range(1, len(waypoints)):
        cum.append(cum[-1] + haversine_m(*waypoints[i - 1], *waypoints[i]))

    def nearest_cum(lat: float, lon: float) -> float:
        best_idx, best_d = 0, float("inf")
        for i, (wlat, wlon) in enumerate(waypoints):
            d = (wlat - lat) ** 2 + (wlon - lon) ** 2
            if d < best_d:
                best_d, best_idx = d, i
        return cum[best_idx]

    events: List[NavEvent] = []
    for i, step in enumerate(route.steps):
        category = classify_maneuver(step.maneuver_type, step.maneuver_modifier)
        events.append(NavEvent(
            kind="turn", category=category, lat=step.lat, lon=step.lon,
            road_name=step.road_name, distance_m=nearest_cum(step.lat, step.lon),
            step_index=i, instruction=generate_terse_instruction(step),
        ))

    for lat, lon in route.traffic_signal_points:
        dist = nearest_cum(lat, lon)
        if any(abs(dist - e.distance_m) < 45 for e in events):
            continue  # a turn already covers this spot; don't double up
        events.append(NavEvent(
            kind="light", category="straight", lat=lat, lon=lon,
            road_name="", distance_m=dist, step_index=None,
            instruction="Continue through the light",
        ))

    events.sort(key=lambda e: e.distance_m)

    deduped: List[NavEvent] = []
    for e in events:
        if deduped and abs(e.distance_m - deduped[-1].distance_m) < 25:
            if e.kind == "turn" and deduped[-1].kind != "turn":
                deduped[-1] = e
            continue
        deduped.append(e)

    return deduped


# ============================================================================
# OPTIONAL LIVE GPS (gpsd) - falls back to manual mode if unavailable
# ============================================================================

class GPSProvider:
    """Thin wrapper around gpsd-py3. If gpsd isn't installed/running, the
    app just stays in manual (button-driven) mode - nothing else changes."""

    def __init__(self):
        self.available = False
        self.lat: Optional[float] = None
        self.lon: Optional[float] = None
        self._stop_flag = False
        if GPSD_MODULE_AVAILABLE:
            try:
                gpsd.connect()
                self.available = True
            except Exception as e:
                print(f"GPS not available (is gpsd running?): {e}")
                self.available = False

    def start(self, callback):
        """Poll gpsd in a background thread; call callback(lat, lon) on each fix."""
        if not self.available:
            return
        self._stop_flag = False

        def poll():
            while not self._stop_flag:
                try:
                    packet = gpsd.get_current()
                    if packet.mode >= 2:
                        self.lat, self.lon = packet.lat, packet.lon
                        callback(self.lat, self.lon)
                except Exception:
                    pass
                time.sleep(2.0)

        threading.Thread(target=poll, daemon=True).start()

    def stop(self):
        self._stop_flag = True


# ============================================================================
# GUI APPLICATION
# ============================================================================

class NavigationApp(tk.Tk):
    """Multi-screen navigation app, scaled and adapted for a Raspberry Pi 5."""

    def __init__(self, force_fullscreen: Optional[bool] = None):
        super().__init__()
        self.title("MyMap - Raspberry Pi Navigation")

        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        # Scale UI to the attached screen: smaller on an official 7" touch
        # panel, normal-to-larger on an HDMI monitor.
        self.scale = max(0.75, min(1.6, screen_w / 1280))
        small_screen = screen_w <= 900 or screen_h <= 550

        use_fullscreen = force_fullscreen if force_fullscreen is not None else small_screen
        if use_fullscreen:
            self.attributes("-fullscreen", True)
        else:
            win_w = min(1000, int(screen_w * 0.9))
            win_h = min(720, int(screen_h * 0.9))
            self.geometry(f"{win_w}x{win_h}")

        self.bind("<F11>", lambda e: self.attributes("-fullscreen", not self.attributes("-fullscreen")))
        self.bind("<Escape>", lambda e: self.attributes("-fullscreen", False))

        # Services
        self.geo_utils = GeoUtils()
        self.routing_engine = RoutingEngine(self.geo_utils)
        self.fuel_calc = FuelCalculator()
        self.gps = GPSProvider()

        # State
        self.current_route: Optional[Route] = None
        self.nav_events: List[NavEvent] = []
        self.current_event_index = 0
        self.start_location: Optional[Location] = None
        self.end_location: Optional[Location] = None
        self.start_search_timer = None
        self.end_search_timer = None

        self.container = tk.Frame(self)
        self.container.pack(side="top", fill="both", expand=True)
        self.container.grid_rowconfigure(0, weight=1)
        self.container.grid_columnconfigure(0, weight=1)

        self.frames: Dict[str, tk.Frame] = {}
        self._create_screens()
        self._show_screen("search")

    def run_on_ui_thread(self, func, *args, **kwargs):
        self.after(0, lambda: func(*args, **kwargs))

    def _create_screens(self):
        self.frames["search"] = SearchScreen(self.container, self)
        self.frames["routes"] = RoutesScreen(self.container, self)
        self.frames["navigation"] = NavigationScreen(self.container, self)
        for frame in self.frames.values():
            frame.grid(row=0, column=0, sticky="nsew")

    def _show_screen(self, screen_name: str):
        frame = self.frames[screen_name]
        frame.tkraise()
        if hasattr(frame, "on_show"):
            frame.on_show()

    def on_closing(self):
        self.gps.stop()
        self.destroy()


# ============================================================================
# SCREEN: SEARCH
# ============================================================================

class SearchScreen(tk.Frame):
    """Start/end location search screen."""

    def __init__(self, parent, app: NavigationApp):
        super().__init__(parent, bg="white")
        self.app = app
        self._build_ui()

    def _build_ui(self):
        s = self.app.scale
        header = tk.Frame(self, bg="#1f1f1f")
        header.pack(fill=tk.X)
        tk.Label(header, text="MyMap Navigation (Raspberry Pi)", font=("Arial", int(16 * s), "bold"),
                 fg="white", bg="#1f1f1f").pack(pady=int(12 * s), padx=10)

        main = tk.Frame(self, bg="white")
        main.pack(fill=tk.BOTH, expand=True)

        tk.Label(main, text="Start location", font=("Arial", int(11 * s), "bold"), bg="white").pack(anchor=tk.W, padx=16, pady=(16, 4))
        self.start_entry = tk.Entry(main, font=("Arial", int(12 * s)))
        self.start_entry.pack(padx=16, pady=(0, 6), fill=tk.X, ipady=int(4 * s))
        self.start_entry.bind("<KeyRelease>", lambda e: self._on_start_input())

        self.start_listbox = tk.Listbox(main, height=4, font=("Arial", int(10 * s)))
        self.start_listbox.pack(padx=16, pady=(0, 12), fill=tk.X)
        self.start_listbox.bind("<<ListboxSelect>>", self._on_start_select)

        tk.Label(main, text="End location", font=("Arial", int(11 * s), "bold"), bg="white").pack(anchor=tk.W, padx=16, pady=(0, 4))
        self.end_entry = tk.Entry(main, font=("Arial", int(12 * s)))
        self.end_entry.pack(padx=16, pady=(0, 6), fill=tk.X, ipady=int(4 * s))
        self.end_entry.bind("<KeyRelease>", lambda e: self._on_end_input())

        self.end_listbox = tk.Listbox(main, height=4, font=("Arial", int(10 * s)))
        self.end_listbox.pack(padx=16, pady=(0, 12), fill=tk.X)
        self.end_listbox.bind("<<ListboxSelect>>", self._on_end_select)

        self.find_btn = tk.Button(main, text="Search for Route", command=self._on_find_route,
                                   bg="#4285f4", fg="white", font=("Arial", int(13 * s), "bold"),
                                   height=2, relief=tk.FLAT)
        self.find_btn.pack(fill=tk.X, padx=16, pady=12)

        self.status_label = tk.Label(main, text="Ready to search", fg="#5f6368", font=("Arial", int(10 * s)), bg="white")
        self.status_label.pack(pady=(0, 10))

        gps_note = "GPS: connected" if self.app.gps.available else "GPS: not detected (manual mode will be used)"
        tk.Label(main, text=gps_note, fg=("#188038" if self.app.gps.available else "#b06000"),
                 font=("Arial", int(9 * s)), bg="white").pack(pady=(0, 10))

    def _on_start_input(self):
        query = self.start_entry.get().strip()
        self.start_listbox.delete(0, tk.END)
        if len(query) < 2:
            return
        if self.app.start_search_timer:
            self.after_cancel(self.app.start_search_timer)
        self.app.start_search_timer = self.after(800, lambda: threading.Thread(
            target=self._search_start, args=(query,), daemon=True).start())

    def _search_start(self, query):
        suggestions = self.app.geo_utils.search_locations(query)
        def update():
            self.start_listbox.delete(0, tk.END)
            for s in suggestions:
                self.start_listbox.insert(tk.END, s)
        self.app.run_on_ui_thread(update)

    def _on_start_select(self, event):
        selection = self.start_listbox.curselection()
        if selection:
            self.start_entry.delete(0, tk.END)
            self.start_entry.insert(0, self.start_listbox.get(selection[0]))
            self.start_listbox.delete(0, tk.END)

    def _on_end_input(self):
        query = self.end_entry.get().strip()
        self.end_listbox.delete(0, tk.END)
        if len(query) < 2:
            return
        if self.app.end_search_timer:
            self.after_cancel(self.app.end_search_timer)
        self.app.end_search_timer = self.after(800, lambda: threading.Thread(
            target=self._search_end, args=(query,), daemon=True).start())

    def _search_end(self, query):
        suggestions = self.app.geo_utils.search_locations(query)
        def update():
            self.end_listbox.delete(0, tk.END)
            for s in suggestions:
                self.end_listbox.insert(tk.END, s)
        self.app.run_on_ui_thread(update)

    def _on_end_select(self, event):
        selection = self.end_listbox.curselection()
        if selection:
            self.end_entry.delete(0, tk.END)
            self.end_entry.insert(0, self.end_listbox.get(selection[0]))
            self.end_listbox.delete(0, tk.END)

    def _on_find_route(self):
        start_addr = self.start_entry.get().strip()
        end_addr = self.end_entry.get().strip()
        if not start_addr or not end_addr:
            messagebox.showerror("Error", "Please enter both a start and an end location.")
            return
        self.status_label.config(text="Finding route...", fg="blue")
        self.find_btn.config(state=tk.DISABLED)
        threading.Thread(target=self._find_route_worker, args=(start_addr, end_addr), daemon=True).start()

    def _find_route_worker(self, start_addr: str, end_addr: str):
        self.app.run_on_ui_thread(self.status_label.config, text="Geocoding...", fg="blue")
        start = self.app.geo_utils.geocode(start_addr)
        end = self.app.geo_utils.geocode(end_addr)
        if not start or not end:
            self.app.run_on_ui_thread(self.status_label.config, text="Error: could not find one of those locations", fg="red")
            self.app.run_on_ui_thread(self.find_btn.config, state=tk.NORMAL)
            return

        self.app.run_on_ui_thread(self.status_label.config, text="Computing route...", fg="blue")
        route = self.app.routing_engine.compute_route(start, end)
        if not route:
            self.app.run_on_ui_thread(self.status_label.config, text="Error: no route found", fg="red")
            self.app.run_on_ui_thread(self.find_btn.config, state=tk.NORMAL)
            return

        self.app.start_location = start
        self.app.end_location = end
        self.app.current_route = route
        self.app.run_on_ui_thread(self.status_label.config, text="Route ready", fg="green")
        self.app.run_on_ui_thread(self.find_btn.config, state=tk.NORMAL)
        self.app.run_on_ui_thread(self.app._show_screen, "routes")


# ============================================================================
# SCREEN: ROUTE OVERVIEW
# ============================================================================

class RoutesScreen(tk.Frame):
    """Shows the full computed route on a map before starting navigation."""

    def __init__(self, parent, app: NavigationApp):
        super().__init__(parent, bg="white")
        self.app = app
        self.map_widget = None
        self._build_ui()

    def _build_ui(self):
        s = self.app.scale
        header = tk.Frame(self, bg="lightblue")
        header.pack(fill=tk.X)
        tk.Label(header, text="Route Overview", font=("Arial", int(14 * s), "bold"), bg="lightblue").pack(side=tk.LEFT, padx=10, pady=8)
        tk.Button(header, text="\u2190 Back", command=lambda: self.app._show_screen("search")).pack(side=tk.RIGHT, padx=10, pady=6)

        self.info_label = tk.Label(self, text="", font=("Arial", int(11 * s), "bold"), bg="lightyellow")
        self.info_label.pack(fill=tk.X, padx=10, pady=6)

        self.map_frame = tk.Frame(self, bg="white", relief=tk.SUNKEN, borderwidth=2)
        self.map_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))

        if TKINTERMAPVIEW_AVAILABLE:
            self.map_widget = tkintermapview.TkinterMapView(self.map_frame, width=800, height=500)
            self.map_widget.pack(fill=tk.BOTH, expand=True)
        else:
            tk.Label(self.map_frame, text="Install tkintermapview to see the map:\npip install tkintermapview",
                     font=("Arial", int(10 * s)), fg="blue").pack(fill=tk.BOTH, expand=True)

        self.start_btn = tk.Button(self, text="Start Navigation", command=self._on_start_navigation,
                                    bg="#34a853", fg="white", font=("Arial", int(13 * s), "bold"), height=2, relief=tk.FLAT)
        self.start_btn.pack(fill=tk.X, padx=10, pady=(0, 10))

    def on_show(self):
        route = self.app.current_route
        if route:
            self.info_label.config(
                text=f"{route.distance_miles:.1f} mi  |  {int(route.duration_seconds // 60)} min  |  "
                     f"{route.turns} turns  |  {route.traffic_signals} lights"
            )
        self._draw_map()

    def _draw_map(self):
        if not TKINTERMAPVIEW_AVAILABLE or not self.map_widget:
            return
        route = self.app.current_route
        start, end = self.app.start_location, self.app.end_location
        if not route or not start or not end:
            return

        self.map_widget.delete_all_path()
        self.map_widget.delete_all_marker()
        self.map_widget.set_position((start.lat + end.lat) / 2, (start.lon + end.lon) / 2)
        self.map_widget.set_zoom(route_zoom(route.distance_miles))

        coords = simplify_points(route.waypoints, max_points=400)
        if len(coords) >= 2:
            self.map_widget.set_path(coords, color=lighten_hex_color("#ff4d4d", 0.72), width=8)
            self.map_widget.set_path(coords, color="#ff4d4d", width=4)

        for lat, lon in route.traffic_signal_points[:20]:
            self.map_widget.set_marker(lat, lon, text="\U0001F6A6")

        self.map_widget.set_marker(start.lat, start.lon, text="Start")
        self.map_widget.set_marker(end.lat, end.lon, text="End")

    def _on_start_navigation(self):
        if not self.app.current_route:
            return
        self.app._show_screen("navigation")


# ============================================================================
# SCREEN: NAVIGATION - one light/turn at a time
# ============================================================================

class NavigationScreen(tk.Frame):
    """
    Turn-by-turn screen. Shows exactly one big arrow icon + instruction for
    the NEXT light or turn only, and re-centers the map on it every time you
    advance - either by tapping Next/Prev, or automatically via GPS.
    """

    def __init__(self, parent, app: NavigationApp):
        super().__init__(parent, bg="white")
        self.app = app
        self.map_widget = None
        self._build_ui()

    def _build_ui(self):
        s = self.app.scale
        header = tk.Frame(self, bg="#1f1f1f")
        header.pack(fill=tk.X)
        tk.Label(header, text="Navigation", font=("Arial", int(15 * s), "bold"), fg="white", bg="#1f1f1f").pack(side=tk.LEFT, padx=10, pady=8)
        self.gps_status = tk.Label(header, text="", font=("Arial", int(9 * s)), fg="#ffcc00", bg="#1f1f1f")
        self.gps_status.pack(side=tk.LEFT, padx=10)
        tk.Button(header, text="Change Route", command=lambda: self.app._show_screen("routes")).pack(side=tk.RIGHT, padx=10, pady=6)

        body = tk.Frame(self, bg="white")
        body.pack(fill=tk.BOTH, expand=True)

        map_frame = tk.Frame(body, bg="white")
        map_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=8, pady=8)
        if TKINTERMAPVIEW_AVAILABLE:
            self.map_widget = tkintermapview.TkinterMapView(map_frame, width=520, height=440)
            self.map_widget.pack(fill=tk.BOTH, expand=True)
        else:
            tk.Label(map_frame, text="Install tkintermapview to see the map.", font=("Arial", int(10 * s))).pack(fill=tk.BOTH, expand=True)

        side_w = int(300 * s)
        side = tk.Frame(body, bg="white", width=side_w)
        side.pack(side=tk.RIGHT, fill=tk.Y, padx=8, pady=8)
        side.pack_propagate(False)

        icon_size = int(220 * s)
        self.icon_canvas = tk.Canvas(side, width=icon_size, height=icon_size, bg="white", highlightthickness=0)
        self.icon_canvas.pack(pady=(10, 4))

        self.instruction_label = tk.Label(side, text="Starting...", font=("Arial", int(15 * s), "bold"),
                                           wraplength=side_w - 20, justify="center", bg="white", fg="#1a73e8")
        self.instruction_label.pack(pady=4)

        self.distance_label = tk.Label(side, text="", font=("Arial", int(24 * s), "bold"), fg="darkgreen", bg="white")
        self.distance_label.pack(pady=4)

        self.progress_label = tk.Label(side, text="", font=("Arial", int(10 * s)), fg="gray", bg="white")
        self.progress_label.pack(pady=4)

        btn_row = tk.Frame(side, bg="white")
        btn_row.pack(pady=10, fill=tk.X)
        self.prev_btn = tk.Button(btn_row, text="\u25c0 Prev", font=("Arial", int(12 * s), "bold"),
                                   height=2, command=self._on_prev)
        self.prev_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=4)
        self.next_btn = tk.Button(btn_row, text="Next \u25b6", font=("Arial", int(12 * s), "bold"),
                                   height=2, bg="#c8f7d0", command=self._on_next)
        self.next_btn.pack(side=tk.LEFT, expand=True, fill=tk.X, padx=4)

        fuel_frame = tk.Frame(side, bg="white")
        fuel_frame.pack(pady=6)
        tk.Label(fuel_frame, text="MPG:", bg="white", font=("Arial", int(10 * s))).pack(side=tk.LEFT)
        self.mpg_entry = tk.Entry(fuel_frame, width=5, font=("Arial", int(10 * s)))
        self.mpg_entry.insert(0, "25")
        self.mpg_entry.pack(side=tk.LEFT, padx=4)
        self.mpg_entry.bind("<KeyRelease>", lambda e: self._update_fuel())
        self.fuel_label = tk.Label(fuel_frame, text="", bg="white", font=("Arial", int(10 * s)))
        self.fuel_label.pack(side=tk.LEFT, padx=6)

    def on_show(self):
        if self.app.current_route:
            self.app.nav_events = build_nav_events(self.app.current_route)
        else:
            self.app.nav_events = []
        self.app.current_event_index = 0
        self._update_display()
        self._draw_map()
        self._update_fuel()
        self._start_gps()

    def _start_gps(self):
        if self.app.gps.available:
            self.gps_status.config(text="GPS: auto-advancing", fg="#00e676")
            self.app.gps.start(self._on_gps_fix)
        else:
            self.gps_status.config(text="Manual mode (no GPS)", fg="#ffcc00")

    def _on_gps_fix(self, lat, lon):
        events = self.app.nav_events
        idx = self.app.current_event_index
        if idx >= len(events):
            return
        target = events[idx]
        if haversine_m(lat, lon, target.lat, target.lon) < 40:
            self.app.run_on_ui_thread(self._advance_from_gps)

    def _advance_from_gps(self):
        if self.app.current_event_index < len(self.app.nav_events):
            self.app.current_event_index += 1
            self._update_display()
            self._draw_map()

    def _current_progress_m(self) -> float:
        events = self.app.nav_events
        idx = self.app.current_event_index
        return events[idx - 1].distance_m if idx > 0 else 0.0

    def _update_display(self):
        events = self.app.nav_events
        idx = self.app.current_event_index
        icon_size = int(self.icon_canvas["width"])

        if not events:
            self.instruction_label.config(text="No route loaded")
            self.distance_label.config(text="")
            self.progress_label.config(text="")
            self.prev_btn.config(state=tk.DISABLED)
            self.next_btn.config(state=tk.DISABLED)
            return

        if idx >= len(events):
            draw_maneuver_icon(self.icon_canvas, "arrive", icon_size, icon_size)
            self.instruction_label.config(text="You have arrived!")
            self.distance_label.config(text="--")
            self.progress_label.config(text=f"Completed all {len(events)} lights/turns")
            self.prev_btn.config(state=tk.NORMAL if idx > 0 else tk.DISABLED)
            self.next_btn.config(state=tk.DISABLED)
            return

        event = events[idx]
        draw_maneuver_icon(self.icon_canvas, event.category, icon_size, icon_size)

        if event.kind == "light":
            label = "Continue Straight"
            if event.road_name:
                label += f" on {event.road_name}"
            label += "\n(approaching traffic light)"
        else:
            label = MANEUVER_LABELS.get(event.category, "Continue")
            if event.road_name:
                label += f"\non {event.road_name}"
        self.instruction_label.config(text=label)

        dist_to_event_m = max(0.0, event.distance_m - self._current_progress_m())
        if dist_to_event_m > 300:
            self.distance_label.config(text=f"{dist_to_event_m / 1609.34:.1f} mi")
        else:
            self.distance_label.config(text=f"{int(dist_to_event_m * 3.28084)} ft")

        kind_word = "light" if event.kind == "light" else "turn"
        self.progress_label.config(text=f"{kind_word.title()} {idx + 1} of {len(events)}")

        self.prev_btn.config(state=tk.NORMAL if idx > 0 else tk.DISABLED)
        self.next_btn.config(state=tk.NORMAL)

    def _draw_map(self):
        if not TKINTERMAPVIEW_AVAILABLE or not self.map_widget or not self.app.current_route:
            return
        events = self.app.nav_events
        idx = self.app.current_event_index
        route = self.app.current_route

        if idx < len(events):
            center_lat, center_lon = events[idx].lat, events[idx].lon
            step_idx = events[idx].step_index
        elif self.app.end_location:
            center_lat, center_lon = self.app.end_location.lat, self.app.end_location.lon
            step_idx = None
        else:
            return

        self.map_widget.delete_all_path()
        self.map_widget.delete_all_marker()

        ref_step = route.steps[step_idx] if step_idx is not None and step_idx < len(route.steps) else (route.steps[0] if route.steps else None)
        if ref_step:
            zoom, radius_mi = get_road_view_params(ref_step, route.avg_speed_mph)
        else:
            zoom, radius_mi = 15, 1.0

        self.map_widget.set_position(center_lat, center_lon)
        self.map_widget.set_zoom(zoom)

        visible = [wp for wp in route.waypoints
                   if math.sqrt((wp[0] - center_lat) ** 2 + (wp[1] - center_lon) ** 2) * 69 < radius_mi]
        if len(visible) >= 2:
            simp = simplify_points(visible, max_points=200)
            self.map_widget.set_path(simp, color=lighten_hex_color("#ff4d4d", 0.72), width=8)
            self.map_widget.set_path(simp, color="#ff4d4d", width=4)

        self.map_widget.set_marker(center_lat, center_lon, text="\u25cf You")

        for e in events[idx + 1: idx + 4]:
            if math.sqrt((e.lat - center_lat) ** 2 + (e.lon - center_lon) ** 2) * 69 < radius_mi:
                icon_char = "\U0001F6A6" if e.kind == "light" else "\u21aa"
                self.map_widget.set_marker(e.lat, e.lon, text=icon_char)

    def _update_fuel(self):
        try:
            mpg = float(self.mpg_entry.get())
        except ValueError:
            mpg = 25.0
        if not self.app.current_route:
            return
        costs = self.app.fuel_calc.total_trip_cost(self.app.current_route.distance_miles, mpg)
        self.fuel_label.config(text=f"${costs['total']:.2f} trip cost")

    def _on_prev(self):
        if self.app.current_event_index > 0:
            self.app.current_event_index -= 1
            self._update_display()
            self._draw_map()

    def _on_next(self):
        if self.app.current_event_index < len(self.app.nav_events):
            self.app.current_event_index += 1
            self._update_display()
            self._draw_map()


# ============================================================================
# MAIN
# ============================================================================

def _check_display():
    """On Linux (i.e. the Pi), fail with a clear message if there's no display."""
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        print(
            "No display detected (DISPLAY/WAYLAND_DISPLAY is not set).\n"
            "This app needs a graphical session. On a Raspberry Pi, boot to the\n"
            "desktop (raspi-config -> System Options -> Boot / Auto Login -> Desktop),\n"
            "or run this over VNC / a connected monitor."
        )
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="MyMap navigation for Raspberry Pi 5")
    parser.add_argument("--fullscreen", action="store_true", help="Force fullscreen kiosk mode")
    parser.add_argument("--windowed", action="store_true", help="Force a normal window")
    args = parser.parse_args()

    _check_display()

    if not TKINTERMAPVIEW_AVAILABLE:
        print("NOTE: tkintermapview is not installed - the map view will be disabled.")
        print("      Install it with: pip install tkintermapview")

    force_fullscreen = True if args.fullscreen else (False if args.windowed else None)

    app = NavigationApp(force_fullscreen=force_fullscreen)
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()


if __name__ == "__main__":
    main()
