"""
MyMap v0.0 - Multi-screen Navigation with Route Selection Map
Shows all routes on map before selection, then detailed turn-by-turn navigation.
"""

import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk
import threading
import requests
import time
import webbrowser
import tempfile
import os
import math
import http.server
import socketserver
from pathlib import Path
from typing import List, Tuple, Optional, Dict
from dataclasses import dataclass
from difflib import SequenceMatcher
from random import random

import folium
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut

try:
    import tkintermapview
    TKINTERMAPVIEW_AVAILABLE = True
except ImportError:
    tkintermapview = None
    TKINTERMAPVIEW_AVAILABLE = False

try:
    from tkinterweb import HtmlFrame
    TKINTERWEB_AVAILABLE = True
except ImportError:
    TKINTERWEB_AVAILABLE = False


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
    traffic_conditions: List[str] = None
    traffic_signal_points: List[Tuple[float, float]] = None

    def __post_init__(self):
        if self.traffic_conditions is None:
            self.traffic_conditions = self._generate_traffic_conditions()
        if self.traffic_signal_points is None:
            self.traffic_signal_points = []

    def _generate_traffic_conditions(self) -> List[str]:
        """Generate realistic traffic conditions along the route."""
        conditions = []
        for _ in self.waypoints:
            rand = random()
            if rand < 0.6:
                conditions.append("green")
            elif rand < 0.85:
                conditions.append("yellow")
            else:
                conditions.append("red")
        return conditions


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
        if modifier:
            return f"Turn {modifier}{road}"
        return f"Turn{road}"
    if maneuver_type in {"merge", "fork", "roundabout", "rotary", "off ramp", "on ramp", "new name", "continue"}:
        if modifier:
            return f"{maneuver_type.title()} {modifier}{road}"
        return f"{maneuver_type.title()}{road}"

    if road_name:
        return f"Continue on {road_name}"
    return "Continue"


# ============================================================================
# UTILITIES
# ============================================================================

class GeoUtils:
    """Fast geocoding and location services."""

    def __init__(self):
        self.geolocator = Nominatim(user_agent="mymap_v0.0")
        self.cache = {}
        self.last_request_time = 0
        self.overpass_url = "https://overpass-api.de/api/interpreter"

    def _query_overpass(self, query: str, bbox: str = "-74.3,40.5,-73.7,40.9") -> List[Dict]:
        """Query Overpass API for POIs around a bounding box."""
        overpass_query = f"""
        [bbox:{bbox}];
        (
            node["name"~"{query}",i];
            way["name"~"{query}",i];
            relation["name"~"{query}",i];
        );
        out center;
        """

        try:
            response = requests.post(self.overpass_url, data=overpass_query, timeout=10)
            if response.status_code == 200:
                data = response.json()
                results = []
                for element in data.get("elements", []):
                    tags = element.get("tags", {})
                    name = tags.get("name", "")
                    if name:
                        lat = element.get("lat") or element.get("center", {}).get("lat")
                        lon = element.get("lon") or element.get("center", {}).get("lon")
                        if lat and lon:
                            results.append({
                                "name": name,
                                "lat": lat,
                                "lon": lon,
                                "type": tags.get("amenity") or tags.get("shop") or tags.get("leisure") or "place"
                            })
                return results
        except Exception as e:
            print(f"Overpass API error: {e}")

        return []

    def _rate_limit(self):
        """Enforce strict rate limiting (3 seconds between requests)."""
        elapsed = time.time() - self.last_request_time
        if elapsed < 3.0:
            time.sleep(3.0 - elapsed)
        self.last_request_time = time.time()

    def geocode(self, address: str) -> Optional[Location]:
        """Geocode address to coordinates."""
        if address in self.cache:
            return self.cache[address]

        # Remove POI type suffix if present (e.g., "Costco (shop)" -> "Costco")
        clean_address = address.rsplit(" (", 1)[0] if " (" in address else address

        try:
            self._rate_limit()
            location = self.geolocator.geocode(clean_address, timeout=5)
            if location:
                result = Location(
                    name=address,
                    lat=location.latitude,
                    lon=location.longitude
                )
                self.cache[address] = result
                return result
        except Exception as e:
            print(f"Geocoding error: {e}")

        return None


    def search_locations(self, query: str, limit: int = 5) -> List[str]:
        """Search for location suggestions from Nominatim and Overpass API."""
        if len(query) < 2:
            return []

        cache_key = f"search_{query}"
        if cache_key in self.cache:
            return self.cache[cache_key]

        results = []

        # Query Nominatim for addresses/cities
        try:
            self._rate_limit()
            locations = self.geolocator.geocode(query, exactly_one=False, timeout=5)
            if locations:
                for loc in locations[:limit]:
                    addr = loc.address
                    results.append(addr)
        except Exception as e:
            print(f"Nominatim search error: {e}")

        # Query Overpass API for POIs (non-blocking, separate thread)
        threading.Thread(
            target=self._async_overpass_search,
            args=(query, cache_key),
            daemon=True
        ).start()

        self.cache[cache_key] = results[:limit]
        return self.cache[cache_key]

    def _async_overpass_search(self, query: str, cache_key: str):
        """Asynchronously search Overpass API and update cache."""
        try:
            pois = self._query_overpass(query)
            if pois:
                current = self.cache.get(cache_key, [])
                # Add POI names to results
                for poi in pois[:3]:
                    poi_name = f"{poi['name']} ({poi['type']})"
                    if poi_name not in current:
                        current.append(poi_name)
                self.cache[cache_key] = current[:5]
        except Exception as e:
            print(f"Overpass search error: {e}")


class OSRMRouter:
    """Fast routing via OSRM (Open Source Routing Machine)."""

    OSRM_URL = "http://router.project-osrm.org/route/v1/driving"

    @staticmethod
    def get_route(
        start: Location,
        end: Location,
        alternatives: int = 0,
        exclude: Optional[str] = None,
        waypoints: Optional[List[Location]] = None
    ) -> Optional[Dict]:
        """Query OSRM API for route."""
        try:
            query_parts = [
                "steps=true",
                "overview=full",
                "geometries=geojson",
                "annotations=speed,duration,distance",
                "continue_straight=true",
            ]
            if alternatives:
                query_parts.append(f"alternatives={alternatives}")
            if exclude:
                query_parts.append(f"exclude={exclude}")

            coords = [f"{start.lon},{start.lat}"]
            for waypoint in waypoints or []:
                coords.append(f"{waypoint.lon},{waypoint.lat}")
            coords.append(f"{end.lon},{end.lat}")

            url = (
                f"{OSRMRouter.OSRM_URL}/"
                f"{';'.join(coords)}"
                f"?{'&'.join(query_parts)}"
            )
            
            response = requests.get(url, timeout=10)
            data = response.json()
            
            if data.get("code") == "Ok":
                return data
        except Exception as e:
            print(f"OSRM error: {e}")
        
        return None


# ============================================================================
# ROUTING ENGINE
# ============================================================================

class RoutingEngine:
    """Compute multiple routes from OSRM."""

    def __init__(self, geo_utils: GeoUtils):
        self.geo_utils = geo_utils
        self.mapquest_api_key = os.getenv("MAPQUEST_API_KEY") or os.getenv("MAPQUEST_KEY")

    def compute_routes(self, start: Location, end: Location, progress_callback=None) -> List[Route]:
        """Get multiple route variations."""
        start_time = time.time()
        routes: List[Route] = []

        def advance(done: int, total: int, message: str):
            if progress_callback:
                progress_callback(done, total, message)

        fastest_data = OSRMRouter.get_route(start, end, alternatives=2)
        if fastest_data and fastest_data.get("routes"):
            route_payloads = [("Fastest", fastest_data["routes"][0])]

            alternative_route = next(
                (route for index, route in enumerate(fastest_data["routes"]) if index != 0),
                fastest_data["routes"][0]
            )
            if self._route_signature(alternative_route) == self._route_signature(fastest_data["routes"][0]):
                alternative_route = self._build_detour_route(start, end, fastest_data["routes"][0])
            route_payloads.append(("Alternative", alternative_route))
        else:
            return []
        free_data = OSRMRouter.get_route(start, end, exclude="motorway")
        free_route = None
        if free_data and free_data.get("routes"):
            free_route = free_data["routes"][0]
        else:
            free_route = fastest_data["routes"][0]
        if self._route_signature(free_route) == self._route_signature(fastest_data["routes"][0]):
            free_route = self._build_toll_avoidance_route(start, end, fastest_data["routes"][0])
        route_payloads.append(("Free", free_route))

        total_units = sum(len(payload[1].get("legs", [{}])[0].get("steps", [])) + 3 for payload in route_payloads)
        progress_state = {"done": 0, "total": max(total_units, 1)}
        advance(0, progress_state["total"], "Preparing road tasks...")

        def route_advance(message: str, units: int = 1):
            progress_state["done"] += units
            advance(progress_state["done"], progress_state["total"], message)

        for route_type, osrm_route in route_payloads:
            routes.append(
                self._build_route_variant(
                    route_type,
                    osrm_route,
                    start,
                    end,
                    start_time,
                    force_no_tolls=(route_type == "Free"),
                    progress_step=route_advance,
                )
            )

        route_order = {"Fastest": 0, "Alternative": 1, "Free": 2}
        advance(progress_state["done"], progress_state["total"], "Route comparison ready")
        return sorted(routes, key=lambda route: route_order.get(route.route_type, 99))

    def _route_signature(self, osrm_route: Dict) -> Tuple[Tuple[float, float], ...]:
        """Create a coarse signature so identical routes can be detected."""
        points = self._extract_waypoints(osrm_route)
        if not points:
            return tuple()
        sample = simplify_points(points, max_points=24)
        return tuple((round(lat, 4), round(lon, 4)) for lat, lon in sample[:24])

    def _build_detour_route(self, start: Location, end: Location, base_route: Dict) -> Dict:
        """Force a more distinct alternative by routing through an offset midpoint."""
        midpoint_lat = (start.lat + end.lat) / 2
        midpoint_lon = (start.lon + end.lon) / 2

        lat_span = end.lat - start.lat
        lon_span = end.lon - start.lon
        length = math.hypot(lat_span, lon_span) or 1.0
        perp_lat = -lon_span / length
        perp_lon = lat_span / length

        route_distance_miles = base_route.get("distance", 0) * 0.000621371
        offset_miles = max(5.0, min(route_distance_miles * 0.15, 40.0))
        offset_degrees = offset_miles / 69.0

        via = Location(
            name="Alternative detour",
            lat=midpoint_lat + perp_lat * offset_degrees,
            lon=midpoint_lon + perp_lon * offset_degrees
        )
        detour_data = OSRMRouter.get_route(start, end, waypoints=[via])
        if detour_data and detour_data.get("routes"):
            return detour_data["routes"][0]

        via = Location(
            name="Alternative detour",
            lat=midpoint_lat - perp_lat * offset_degrees,
            lon=midpoint_lon - perp_lon * offset_degrees
        )
        detour_data = OSRMRouter.get_route(start, end, waypoints=[via])
        if detour_data and detour_data.get("routes"):
            return detour_data["routes"][0]

        return base_route

    def _build_toll_avoidance_route(self, start: Location, end: Location, base_route: Dict) -> Dict:
        """Force a more distinct toll-avoidant route by routing via an offset corridor."""
        midpoint_lat = (start.lat + end.lat) / 2
        midpoint_lon = (start.lon + end.lon) / 2

        lat_span = end.lat - start.lat
        lon_span = end.lon - start.lon
        length = math.hypot(lat_span, lon_span) or 1.0
        perp_lat = -lon_span / length
        perp_lon = lat_span / length

        route_distance_miles = base_route.get("distance", 0) * 0.000621371
        offset_miles = max(10.0, min(route_distance_miles * 0.22, 50.0))
        offset_degrees = offset_miles / 69.0

        candidates = [
            [
                Location(
                    name="Free route detour A",
                    lat=midpoint_lat + perp_lat * offset_degrees,
                    lon=midpoint_lon + perp_lon * offset_degrees,
                ),
                Location(
                    name="Free route detour B",
                    lat=midpoint_lat + perp_lat * (offset_degrees * 0.55),
                    lon=midpoint_lon + perp_lon * (offset_degrees * 0.55),
                ),
            ],
            [
                Location(
                    name="Free route detour A",
                    lat=midpoint_lat - perp_lat * offset_degrees,
                    lon=midpoint_lon - perp_lon * offset_degrees,
                ),
                Location(
                    name="Free route detour B",
                    lat=midpoint_lat - perp_lat * (offset_degrees * 0.55),
                    lon=midpoint_lon - perp_lon * (offset_degrees * 0.55),
                ),
            ],
        ]

        best_candidate = None
        best_distance = float("inf")
        for via_points in candidates:
            detour_data = OSRMRouter.get_route(start, end, waypoints=via_points)
            if detour_data and detour_data.get("routes"):
                route = detour_data["routes"][0]
                distance = route.get("distance", float("inf"))
                if distance < best_distance:
                    best_distance = distance
                    best_candidate = route

        return best_candidate or self._build_detour_route(start, end, base_route)

    def _build_route_variant(
        self,
        route_type: str,
        osrm_route: Dict,
        start: Location,
        end: Location,
        start_time: float,
        force_no_tolls: bool = False,
        progress_step=None
    ) -> Route:
        """Build a route variant and add light traffic-delay estimates."""
        steps = self._parse_steps(osrm_route["legs"][0]["steps"], route_type, progress_step=progress_step)
        waypoints = self._extract_waypoints(osrm_route)
        if progress_step:
            progress_step(f"{route_type}: extracted road geometry")
        if len(waypoints) < 2:
            waypoints = [(start.lat, start.lon), (end.lat, end.lon)]

        distance_miles = osrm_route["distance"] * 0.000621371
        duration_sec = osrm_route["duration"]
        avg_speed_mph = self._estimate_average_speed_mph(osrm_route)
        turn_count, u_turn_count, turn_delay_sec = self._estimate_turn_delay(steps)
        traffic_signal_points = self._fetch_traffic_signal_points(waypoints)
        traffic_signals = len(traffic_signal_points)
        if progress_step:
            progress_step(f"{route_type}: counted traffic lights")
        signal_delay_sec = traffic_signals * 18.0
        computation_time = time.time() - start_time

        route_duration = duration_sec + signal_delay_sec + turn_delay_sec
        toll_cost = 0.0 if force_no_tolls else self._estimate_toll_cost(osrm_route, steps, start, end)
        if progress_step:
            progress_step(f"{route_type}: finalized route")

        return Route(
            route_type=route_type,
            steps=steps,
            waypoints=waypoints,
            distance_miles=distance_miles,
            duration_seconds=route_duration,
            cost_usd=toll_cost,
            computation_time_sec=computation_time,
            avg_speed_mph=avg_speed_mph,
            traffic_signals=traffic_signals,
            signal_delay_sec=signal_delay_sec,
            turns=turn_count,
            u_turns=u_turn_count,
            turn_delay_sec=turn_delay_sec,
            traffic_signal_points=traffic_signal_points
        )

    def _extract_waypoints(self, osrm_route: Dict) -> List[Tuple[float, float]]:
        """Extract waypoints (lat, lon) from OSRM geometry."""
        try:
            geometry = osrm_route.get("geometry", {})
            if isinstance(geometry, dict) and "coordinates" in geometry:
                coords = geometry["coordinates"]
                return [(lat, lon) for lon, lat in coords]
            if isinstance(geometry, list):
                return [(lat, lon) for lon, lat in geometry]
        except Exception as e:
            print(f"Waypoint extraction error: {e}")
        
        return []

    def _parse_steps(self, osrm_steps: List[Dict], route_type: str = "", progress_step=None) -> List[Step]:
        """Convert OSRM steps to our Step format."""
        steps = []
        
        total_steps = max(len(osrm_steps), 1)
        for index, osrm_step in enumerate(osrm_steps, start=1):
            instruction = osrm_step.get("maneuver", {}).get("instruction", "Continue")
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
                instruction=instruction,
                distance_m=distance,
                duration_s=duration,
                lat=lat,
                lon=lon
                ,road_name=road_name,
                maneuver_type=maneuver_type,
                maneuver_modifier=maneuver_modifier
            ))
            if progress_step:
                progress_step(f"{route_type}: road segment {index}/{total_steps}")
        
        return steps

    def _estimate_toll_cost(self, route: Dict, steps: List[Step], start: Location, end: Location) -> float:
        """Estimate toll cost using a toll API when available, otherwise heuristics."""
        api_cost = self._lookup_mapquest_toll_cost(start, end)
        if api_cost is not None:
            return api_cost

        road_text = " ".join(
            filter(
                None,
                [step.road_name for step in steps] + [step.instruction for step in steps]
            )
        ).lower()

        toll_keywords = [
            "toll", "turnpike", "parkway", "expressway", "express lanes",
            "cashless toll", "bridge", "tunnel", "plaza"
        ]
        if any(keyword in road_text for keyword in toll_keywords):
            distance_miles = route["distance"] * 0.000621371
            base = 4.50
            distance_component = max(0.0, (distance_miles - 25.0) * 0.12)
            return round(base + distance_component, 2)

        distance_miles = route["distance"] * 0.000621371
        if distance_miles > 50:
            return round(distance_miles * 0.18, 2)
        return 0.0

    def _lookup_mapquest_toll_cost(self, start: Location, end: Location) -> Optional[float]:
        """Query MapQuest for toll cost when an API key is configured."""
        if not self.mapquest_api_key:
            return None

        try:
            url = "https://www.mapquestapi.com/directions/v2/route"
            params = {
                "key": self.mapquest_api_key,
                "from": start.name,
                "to": end.name,
                "tollCost": "true",
                "outFormat": "json",
            }
            response = requests.get(url, params=params, timeout=12)
            data = response.json()
            route = data.get("route", {})
            toll_cost = route.get("tollCost")
            if isinstance(toll_cost, (int, float)):
                return round(float(toll_cost), 2)
        except Exception as e:
            print(f"MapQuest toll lookup error: {e}")

        return None

    def _estimate_turn_delay(self, steps: List[Step]) -> Tuple[int, int, float]:
        """Estimate delay caused by turns and U-turns."""
        turn_count = 0
        u_turn_count = 0
        delay = 0.0

        turn_like = {"turn", "merge", "fork", "roundabout", "rotary", "off ramp", "on ramp", "new name"}
        for step in steps:
            maneuver_type = (step.maneuver_type or "").lower()
            modifier = (step.maneuver_modifier or "").lower()
            if maneuver_type == "uturn" or "u-turn" in step.instruction.lower():
                turn_count += 1
                u_turn_count += 1
                delay += 18.0
                continue

            if maneuver_type in turn_like or modifier in {"left", "right", "slight left", "slight right", "sharp left", "sharp right"}:
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
        """Estimate average speed from OSRM segment annotations."""
        try:
            annotations = osrm_route.get("legs", [{}])[0].get("annotation", {})
            speeds = annotations.get("speed", [])
            if speeds:
                valid_speeds = [speed for speed in speeds if isinstance(speed, (int, float)) and speed > 0]
                if valid_speeds:
                    return round(sum(valid_speeds) / len(valid_speeds) * 2.23694, 1)
        except Exception as e:
            print(f"Speed estimation error: {e}")

        return 0.0

    def _fetch_traffic_signal_points(self, waypoints: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
        """Fetch traffic signal coordinates near the route path."""
        if len(waypoints) < 2:
            return []

        lats = [point[0] for point in waypoints]
        lons = [point[1] for point in waypoints]
        south, north = min(lats), max(lats)
        west, east = min(lons), max(lons)

        overpass_query = f"""
        [out:json][timeout:8];
        (
          node["highway"="traffic_signals"]({south},{west},{north},{east});
        );
        out body;
        """

        try:
            response = requests.post(self.geo_utils.overpass_url, data=overpass_query, timeout=10)
            if response.status_code == 200:
                data = response.json()
                elements = data.get("elements", [])
                points: List[Tuple[float, float]] = []
                for element in elements:
                    lat = element.get("lat") or element.get("center", {}).get("lat")
                    lon = element.get("lon") or element.get("center", {}).get("lon")
                    if lat is not None and lon is not None:
                        points.append((lat, lon))
                return points
        except Exception as e:
            print(f"Traffic signal estimation error: {e}")

        return []


# ============================================================================
# MAP VISUALIZATION
# ============================================================================

class MapVisualizer:
    """Create maps for route selection and navigation."""

    @staticmethod
    def create_route_selection_map(start: Location, end: Location, routes: List[Route]) -> str:
        """Create map showing all routes for selection."""
        center_lat = (start.lat + end.lat) / 2
        center_lon = (start.lon + end.lon) / 2

        map_obj = folium.Map(
            location=[center_lat, center_lon],
            zoom_start=10,
            tiles='CartoDB positron'
        )

        colors = ['blue', 'green', 'purple', 'orange', 'red']

        # Add routes with traffic coloring
        for idx, route in enumerate(routes):
            color = colors[min(idx, len(colors) - 1)]

            # Draw route with traffic conditions
            for i in range(len(route.waypoints) - 1):
                traffic_color = route.traffic_conditions[i] if i < len(route.traffic_conditions) else 'green'
                display_color = {'green': '#2ecc71', 'yellow': '#f1c40f', 'red': '#e74c3c'}.get(traffic_color, '#2ecc71')

                folium.PolyLine(
                    [route.waypoints[i], route.waypoints[i + 1]],
                    color=display_color,
                    weight=4,
                    opacity=0.8
                ).add_to(map_obj)

            popup_text = (
                f"<b>{route.route_type}</b><br>"
                f"Distance: {route.distance_miles:.1f} mi<br>"
                f"Time: {int(route.duration_seconds // 60)} mins<br>"
                f"Cost: ${route.cost_usd:.2f}"
            )

        # Start and end markers
        folium.Marker(
            [start.lat, start.lon],
            popup=f"<b>Start</b><br>{start.name}",
            icon=folium.Icon(color='green', icon='play', prefix='fa')
        ).add_to(map_obj)

        folium.Marker(
            [end.lat, end.lon],
            popup=f"<b>End</b><br>{end.name}",
            icon=folium.Icon(color='red', icon='stop', prefix='fa')
        ).add_to(map_obj)

        # Add traffic legend
        legend_html = '''
        <div style="position: fixed;
                    bottom: 50px; right: 50px; width: 180px; height: 140px;
                    background-color: white; border:2px solid grey; z-index:9999;
                    font-size:14px; padding: 10px">
        <p style="margin: 5px 0;"><b>Traffic Conditions</b></p>
        <p style="margin: 5px 0;"><span style="background-color: #2ecc71; padding: 5px 10px; border-radius: 3px;">Green - Free</span></p>
        <p style="margin: 5px 0;"><span style="background-color: #f1c40f; padding: 5px 10px; border-radius: 3px; color: black;">Yellow - Congested</span></p>
        <p style="margin: 5px 0;"><span style="background-color: #e74c3c; padding: 5px 10px; border-radius: 3px;">Red - Heavy</span></p>
        </div>
        '''
        map_obj.get_root().html.add_child(folium.Element(legend_html))

        temp_file = tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False)
        map_obj.save(temp_file.name)
        temp_file.close()
        return temp_file.name

    @staticmethod
    def create_navigation_map(route: Route, start: Location, end: Location,
                            current_step_idx: int) -> str:
        """Create map showing current navigation progress."""
        # Zoom level based on distance
        if route.distance_miles < 5:
            zoom = 15
        elif route.distance_miles < 20:
            zoom = 13
        else:
            zoom = 11

        map_obj = folium.Map(
            location=[route.waypoints[0][0], route.waypoints[0][1]],
            zoom_start=zoom,
            tiles='CartoDB positron'
        )

        # Draw full route with traffic conditions
        for i in range(len(route.waypoints) - 1):
            traffic_color = route.traffic_conditions[i] if i < len(route.traffic_conditions) else 'green'
            display_color = {'green': '#2ecc71', 'yellow': '#f1c40f', 'red': '#e74c3c'}.get(traffic_color, '#2ecc71')
            opacity = 0.3 if i < current_step_idx else 0.8

            folium.PolyLine(
                [route.waypoints[i], route.waypoints[i + 1]],
                color=display_color,
                weight=4,
                opacity=opacity
            ).add_to(map_obj)

        # Current position marker
        if current_step_idx < len(route.steps):
            step = route.steps[current_step_idx]
            folium.Marker(
                [step.lat, step.lon],
                popup="Current Location",
                icon=folium.Icon(color='blue', icon='dot-circle-o', prefix='fa')
            ).add_to(map_obj)

        # Destination
        folium.Marker(
            [end.lat, end.lon],
            popup=f"Destination: {end.name}",
            icon=folium.Icon(color='red', icon='flag', prefix='fa')
        ).add_to(map_obj)

        # Add traffic legend
        legend_html = '''
        <div style="position: fixed;
                    bottom: 50px; right: 50px; width: 180px; height: 140px;
                    background-color: white; border:2px solid grey; z-index:9999;
                    font-size:14px; padding: 10px">
        <p style="margin: 5px 0;"><b>Traffic Conditions</b></p>
        <p style="margin: 5px 0;"><span style="background-color: #2ecc71; padding: 5px 10px; border-radius: 3px;">Green - Free</span></p>
        <p style="margin: 5px 0;"><span style="background-color: #f1c40f; padding: 5px 10px; border-radius: 3px; color: black;">Yellow - Congested</span></p>
        <p style="margin: 5px 0;"><span style="background-color: #e74c3c; padding: 5px 10px; border-radius: 3px;">Red - Heavy</span></p>
        </div>
        '''
        map_obj.get_root().html.add_child(folium.Element(legend_html))

        temp_file = tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False)
        map_obj.save(temp_file.name)
        temp_file.close()
        return temp_file.name


# ============================================================================
# GUI APPLICATION
# ============================================================================

class NavigationApp(tk.Tk):
    """Multi-screen navigation application."""

    def __init__(self):
        super().__init__()
        self.title("MyMap v0.0 Navigation")
        self.geometry("800x900")
        
        # Services
        self.geo_utils = GeoUtils()
        self.routing_engine = RoutingEngine(self.geo_utils)
        
        # State
        self.routes: List[Route] = []
        self.current_route: Optional[Route] = None
        self.start_location: Optional[Location] = None
        self.end_location: Optional[Location] = None
        self.current_step_index = 0
        
        # Debouncing
        self.start_search_timer = None
        self.end_search_timer = None
        
        # Screens
        self.current_screen = "search"
        
        # Container
        self.container = tk.Frame(self)
        self.container.pack(side="top", fill="both", expand=True)
        self.container.grid_rowconfigure(0, weight=1)
        self.container.grid_columnconfigure(0, weight=1)
        
        self.frames = {}
        self._create_screens()
        self._show_screen("search")

    def run_on_ui_thread(self, func, *args, **kwargs):
        """Schedule a callback on the Tk main thread."""
        self.after(0, lambda: func(*args, **kwargs))

    def _create_screens(self):
        """Create all screen frames."""
        self.frames["search"] = SearchScreen(self.container, self)
        self.frames["routes"] = RoutesScreen(self.container, self)
        self.frames["navigation"] = NavigationScreen(self.container, self)
        
        for frame in self.frames.values():
            frame.grid(row=0, column=0, sticky="nsew")

    def _show_screen(self, screen_name: str):
        """Switch to a screen."""
        self.current_screen = screen_name
        frame = self.frames[screen_name]
        frame.tkraise()
        if hasattr(frame, 'on_show'):
            frame.on_show()


# ============================================================================
# SCREEN: SEARCH
# ============================================================================

class SearchScreen(tk.Frame):
    """Initial location search screen."""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self._build_ui()

    def _build_ui(self):
        """Build search UI with Google Maps-inspired styling."""
        # Header
        header = tk.Frame(self, bg="#1f1f1f")
        header.pack(fill=tk.X)
        tk.Label(header, text="MyMap Navigation", font=("Arial", 16, "bold"), fg="white", bg="#1f1f1f").pack(pady=12, padx=10)

        # Main container
        main_frame = tk.Frame(self, bg="white")
        main_frame.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)

        # Start location
        start_label = tk.Label(main_frame, text="Start location", font=("Arial", 10, "bold"), fg="#202124", bg="white")
        start_label.pack(anchor=tk.W, padx=16, pady=(16, 4))

        self.start_entry = tk.Entry(main_frame, width=70, font=("Arial", 11), relief=tk.FLAT, bd=1)
        self.start_entry.pack(padx=16, pady=(0, 8), fill=tk.X)
        self.start_entry.insert(0, "207, Woodcliff Boulevard, Woodcliff, Mount Pleasant, Marlboro Township, Monmouth County, New Jersey, 07751, United States")
        self.start_entry.bind("<KeyRelease>", lambda e: self._on_start_input())
        self.start_entry.config(bg="#f0f0f0", highlightthickness=1, highlightcolor="#4285f4", highlightbackground="#dadce0")

        start_frame = tk.Frame(main_frame, bg="white")
        start_frame.pack(padx=16, pady=(0, 12), fill=tk.BOTH)

        scrollbar_start = tk.Scrollbar(start_frame)
        scrollbar_start.pack(side=tk.RIGHT, fill=tk.Y)

        self.start_listbox = tk.Listbox(start_frame, height=4, width=70, font=("Arial", 10), yscrollcommand=scrollbar_start.set, relief=tk.FLAT, bd=0)
        self.start_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.start_listbox.config(bg="#f8f9fa", highlightthickness=0)
        scrollbar_start.config(command=self.start_listbox.yview)
        self.start_listbox.bind("<<ListboxSelect>>", self._on_start_select)

        # End location
        end_label = tk.Label(main_frame, text="End location", font=("Arial", 10, "bold"), fg="#202124", bg="white")
        end_label.pack(anchor=tk.W, padx=16, pady=(16, 4))

        self.end_entry = tk.Entry(main_frame, width=70, font=("Arial", 11), relief=tk.FLAT, bd=1)
        self.end_entry.pack(padx=16, pady=(0, 8), fill=tk.X)
        self.end_entry.insert(0, "")
        self.end_entry.bind("<KeyRelease>", lambda e: self._on_end_input())
        self.end_entry.config(bg="#f0f0f0", highlightthickness=1, highlightcolor="#4285f4", highlightbackground="#dadce0")

        end_frame = tk.Frame(main_frame, bg="white")
        end_frame.pack(padx=16, pady=(0, 12), fill=tk.BOTH)

        scrollbar_end = tk.Scrollbar(end_frame)
        scrollbar_end.pack(side=tk.RIGHT, fill=tk.Y)

        self.end_listbox = tk.Listbox(end_frame, height=4, width=70, font=("Arial", 10), yscrollcommand=scrollbar_end.set, relief=tk.FLAT, bd=0)
        self.end_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.end_listbox.config(bg="#f8f9fa", highlightthickness=0)
        scrollbar_end.config(command=self.end_listbox.yview)
        self.end_listbox.bind("<<ListboxSelect>>", self._on_end_select)

        # Calculate button
        button_frame = tk.Frame(main_frame, bg="white")
        button_frame.pack(fill=tk.X, padx=16, pady=16)
        self.find_btn = tk.Button(button_frame, text="Search for routes", command=self._on_find_routes, bg="#4285f4", fg="white", font=("Arial", 11, "bold"), height=2, relief=tk.FLAT, cursor="hand2")
        self.find_btn.pack(fill=tk.X)

        self.status_label = tk.Label(main_frame, text="Ready to search", fg="#5f6368", font=("Arial", 9), bg="white")
        self.status_label.pack(pady=(0, 16))

        self.progress_text = tk.StringVar(value="")
        self.progress_value = tk.DoubleVar(value=0)
        self.progress_label = tk.Label(main_frame, textvariable=self.progress_text, fg="#5f6368", font=("Arial", 9), bg="white")
        self.progress_bar = ttk.Progressbar(main_frame, mode="determinate", maximum=100, variable=self.progress_value)

    def _on_start_input(self):
        """Debounced start search."""
        query = self.start_entry.get().strip()
        self.start_listbox.delete(0, tk.END)
        
        if len(query) < 2:
            return
        
        if self.app.start_search_timer:
            self.after_cancel(self.app.start_search_timer)
        
        self.app.start_search_timer = self.after(1000, lambda: threading.Thread(target=self._search_start, args=(query,), daemon=True).start())

    def _search_start(self, query):
        """Search for start locations."""
        suggestions = self.app.geo_utils.search_locations(query, limit=5)
        def update_listbox():
            self.start_listbox.delete(0, tk.END)
            for s in suggestions:
                self.start_listbox.insert(tk.END, s)
        self.app.run_on_ui_thread(update_listbox)

    def _on_start_select(self, event):
        """Select start location."""
        selection = self.start_listbox.curselection()
        if selection:
            self.start_entry.delete(0, tk.END)
            self.start_entry.insert(0, self.start_listbox.get(selection[0]))
            self.start_listbox.delete(0, tk.END)

    def _on_end_input(self):
        """Debounced end search."""
        query = self.end_entry.get().strip()
        self.end_listbox.delete(0, tk.END)
        
        if len(query) < 2:
            return
        
        if self.app.end_search_timer:
            self.after_cancel(self.app.end_search_timer)
        
        self.app.end_search_timer = self.after(1000, lambda: threading.Thread(target=self._search_end, args=(query,), daemon=True).start())

    def _search_end(self, query):
        """Search for end locations."""
        suggestions = self.app.geo_utils.search_locations(query, limit=5)
        def update_listbox():
            self.end_listbox.delete(0, tk.END)
            for s in suggestions:
                self.end_listbox.insert(tk.END, s)
        self.app.run_on_ui_thread(update_listbox)

    def _on_end_select(self, event):
        """Select end location."""
        selection = self.end_listbox.curselection()
        if selection:
            self.end_entry.delete(0, tk.END)
            self.end_entry.insert(0, self.end_listbox.get(selection[0]))
            self.end_listbox.delete(0, tk.END)

    def _on_find_routes(self):
        """Calculate routes."""
        start_addr = self.start_entry.get().strip()
        end_addr = self.end_entry.get().strip()

        if not start_addr or not end_addr:
            messagebox.showerror("Error", "Please enter both locations.")
            return

        self.status_label.config(text="Finding routes...", fg="blue")
        self.find_btn.config(state=tk.DISABLED)
        self._show_progress(0, 1, "Preparing route search...")
        self.update()

        threading.Thread(target=self._find_routes_worker_safe, args=(start_addr, end_addr), daemon=True).start()

    def _find_routes_worker_safe(self, start_addr: str, end_addr: str):
        """Worker thread with thread-safe UI updates."""
        self.app.run_on_ui_thread(self.status_label.config, text="Geocoding...", fg="blue")
        self.app.run_on_ui_thread(self._show_progress, 0, 1, "Geocoding locations...")

        start = self.app.geo_utils.geocode(start_addr)
        end = self.app.geo_utils.geocode(end_addr)

        if not start or not end:
            self.app.run_on_ui_thread(self.status_label.config, text="Error: Could not find locations", fg="red")
            self.app.run_on_ui_thread(self.find_btn.config, state=tk.NORMAL)
            self.app.run_on_ui_thread(self._hide_progress)
            return

        self.app.run_on_ui_thread(self.status_label.config, text="Computing routes...", fg="blue")

        def progress_callback(done, total, message):
            self.app.run_on_ui_thread(self._show_progress, done, total, message)

        self.app.routes = self.app.routing_engine.compute_routes(start, end, progress_callback=progress_callback)

        if not self.app.routes:
            self.app.run_on_ui_thread(self.status_label.config, text="Error: No routes found", fg="red")
            self.app.run_on_ui_thread(self.find_btn.config, state=tk.NORMAL)
            self.app.run_on_ui_thread(self._hide_progress)
            return

        self.app.start_location = start
        self.app.end_location = end
        self.app.run_on_ui_thread(self.status_label.config, text="Routes calculated", fg="green")
        self.app.run_on_ui_thread(self.find_btn.config, state=tk.NORMAL)
        self.app.run_on_ui_thread(self._hide_progress)
        self.app.run_on_ui_thread(self.app._show_screen, "routes")

    def _show_progress(self, done: int, total: int, message: str):
        """Update the route progress bar."""
        total = max(total, 1)
        done = max(0, min(done, total))
        percent = (done / total) * 100
        self.progress_text.set(f"{message}  ({done}/{total} road tasks)")
        self.progress_value.set(percent)
        if not self.progress_label.winfo_ismapped():
            self.progress_label.pack(fill=tk.X, padx=16, pady=(0, 4))
        if not self.progress_bar.winfo_ismapped():
            self.progress_bar.pack(fill=tk.X, padx=16, pady=(0, 16))
        self.update_idletasks()

    def _hide_progress(self):
        """Hide the route progress UI."""
        self.progress_text.set("")
        self.progress_value.set(0)
        if self.progress_bar.winfo_ismapped():
            self.progress_bar.pack_forget()
        if self.progress_label.winfo_ismapped():
            self.progress_label.pack_forget()

    def _find_routes_worker(self, start_addr: str, end_addr: str):
        """Worker thread."""
        # Geocode
        self.status_label.config(text="Geocoding...", fg="blue")
        self.update()

        start = self.app.geo_utils.geocode(start_addr)
        end = self.app.geo_utils.geocode(end_addr)

        if not start or not end:
            self.status_label.config(text="Error: Could not find locations", fg="red")
            self.find_btn.config(state=tk.NORMAL)
            return

        self.status_label.config(text="Computing routes...", fg="blue")
        self.update()

        # Compute routes
        self.app.routes = self.app.routing_engine.compute_routes(start, end)
        
        if not self.app.routes:
            self.status_label.config(text="Error: No routes found", fg="red")
            self.find_btn.config(state=tk.NORMAL)
            return

        self.app.start_location = start
        self.app.end_location = end
        self.status_label.config(text="✓ Routes calculated", fg="green")
        self.find_btn.config(state=tk.NORMAL)
        
        self.app._show_screen("routes")


# ============================================================================
# SCREEN: ROUTES (MAP + SELECTION)
# ============================================================================

class RoutesScreen(tk.Frame):
    """Route selection screen with embedded map."""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.map_frame = None
        self.map_widget = None
        self._build_ui()

    def _build_ui(self):
        """Build routes UI with embedded map."""
        # Header
        header = tk.Frame(self, bg="lightblue")
        header.pack(fill=tk.X, padx=10, pady=10)
        tk.Label(header, text="Select Your Route", font=("Arial", 14, "bold"), bg="lightblue").pack(side=tk.LEFT)
        tk.Button(header, text="← Back", command=lambda: self.app._show_screen("search")).pack(side=tk.RIGHT)

        # Main container with routes panel and map panel
        container = tk.Frame(self)
        container.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        left_panel = tk.Frame(container)
        left_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=False, padx=(0, 5))

        right_panel = tk.Frame(container)
        right_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(5, 0))

        routes_frame = tk.LabelFrame(left_panel, text="Route Options", padx=10, pady=10, font=("Arial", 10, "bold"))
        routes_frame.pack(fill=tk.BOTH, expand=True)
        self.routes_frame = routes_frame
        self.route_buttons = []
        self._render_route_buttons()

        tk.Label(right_panel, text="Route Map", font=("Arial", 10, "bold")).pack(anchor=tk.W)

        self.map_frame = tk.Frame(right_panel, bg="white", relief=tk.SUNKEN, borderwidth=2)
        self.map_frame.pack(fill=tk.BOTH, expand=True)

        if TKINTERMAPVIEW_AVAILABLE:
            self.map_widget = tkintermapview.TkinterMapView(self.map_frame, width=700, height=600)
            self.map_widget.pack(fill=tk.BOTH, expand=True)
            self.map_widget.set_position(40.3573, -74.2635)
            self.map_widget.set_zoom(11)
        else:
            tk.Label(
                self.map_frame,
                text="Install tkintermapview to display the route map.\npip install tkintermapview",
                font=("Arial", 10),
                fg="blue"
            ).pack(fill=tk.BOTH, expand=True)

    def on_show(self):
        """Refresh when shown."""
        self._render_route_buttons()
        self._draw_map()

    def _render_route_buttons(self):
        """Render the route buttons inside the options frame."""
        if not hasattr(self, "routes_frame"):
            return

        for child in self.routes_frame.winfo_children():
            child.destroy()
        self.route_buttons = []

        if not self.app.routes:
            tk.Label(self.routes_frame, text="No routes available yet.", font=("Arial", 10), fg="#666").pack(anchor=tk.W)
            return

        route_order = {"Fastest": 0, "Alternative": 1, "Free": 2}
        for route in sorted(self.app.routes, key=lambda item: route_order.get(item.route_type, 99)):
            btn_text = (
                f"{route.route_type}\n"
                f"{route.distance_miles:.1f} mi  |  "
                f"{int(route.duration_seconds // 60)} min  |  "
                f"${route.cost_usd:.2f}\n"
                f"Avg {route.avg_speed_mph:.0f} mph  |  "
                f"{route.traffic_signals} lights | {route.turns} turns"
            )
            btn = tk.Button(
                self.routes_frame,
                text=btn_text,
                command=lambda r=route: self._on_route_selected(r),
                font=("Arial", 10),
                bg="lightyellow",
                height=4,
                relief=tk.RAISED,
                borderwidth=2,
                anchor="w",
                justify="left"
            )
            btn.pack(fill=tk.X, pady=5)
            self.route_buttons.append(btn)

    def _draw_map(self):
        """Draw routes directly in tkintermapview."""
        if not TKINTERMAPVIEW_AVAILABLE or not self.map_widget:
            return

        if not self.app.start_location or not self.app.end_location or not self.app.routes:
            return

        self.map_widget.delete_all_path()
        self.map_widget.delete_all_marker()

        start = self.app.start_location
        end = self.app.end_location
        self.map_widget.set_position((start.lat + end.lat) / 2, (start.lon + end.lon) / 2)
        self.map_widget.set_zoom(route_zoom(max(route.distance_miles for route in self.app.routes)))

        colors = {"Fastest": "#ff4d4d", "Alternative": "#4d6fff", "Free": "#2ecc71"}
        for route in self.app.routes:
            coords = simplify_points(route.waypoints, max_points=400)
            if len(coords) >= 2:
                route_color = colors.get(route.route_type, "#ff9900")
                self.map_widget.set_path(coords, color=lighten_hex_color(route_color, 0.72), width=8)
                self.map_widget.set_path(coords, color=route_color, width=4)
            for light_lat, light_lon in route.traffic_signal_points[:10]:
                self.map_widget.set_marker(light_lat, light_lon, text="TL")

        self.map_widget.set_marker(start.lat, start.lon, text="Start")
        self.map_widget.set_marker(end.lat, end.lon, text="End")

    def _on_route_selected(self, route: Route):
        """Select a route and go to navigation."""
        self.app.current_route = route
        self.app.current_step_index = 0
        self.app._show_screen("navigation")


# ============================================================================
# SCREEN: NAVIGATION
# ============================================================================

class NavigationScreen(tk.Frame):
    """Turn-by-turn navigation screen with embedded map."""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.map_frame = None
        self.map_widget = None
        self._build_ui()

    def _build_ui(self):
        """Build navigation UI with embedded map."""
        # Header
        header = tk.Frame(self, bg="lightblue")
        header.pack(fill=tk.X, padx=10, pady=10)
        tk.Label(header, text="Navigation", font=("Arial", 14, "bold"), bg="lightblue").pack(side=tk.LEFT)
        tk.Button(header, text="← Change Route", command=lambda: self.app._show_screen("routes")).pack(side=tk.RIGHT)

        # Route info
        if self.app.current_route:
            info = (
                f"{self.app.current_route.route_type} Route  |  "
                f"{self.app.current_route.distance_miles:.1f} mi  |  "
                f"{int(self.app.current_route.duration_seconds // 60)} min  |  "
                f"${self.app.current_route.cost_usd:.2f}  |  "
                f"{self.app.current_route.turns} turns, {self.app.current_route.u_turns} U-turns, "
                f"{self.app.current_route.traffic_signals} lights"
            )
            tk.Label(self, text=info, font=("Arial", 10, "bold"), bg="lightyellow").pack(fill=tk.X, padx=10, pady=5)

        # Main container: map on left, instructions on right
        container = tk.Frame(self)
        container.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        left_panel = tk.Frame(container)
        left_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))

        right_panel = tk.Frame(container)
        right_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=False, padx=(5, 0))

        tk.Label(left_panel, text="Route Map", font=("Arial", 10, "bold")).pack(anchor=tk.W)

        self.map_frame = tk.Frame(left_panel, bg="white", relief=tk.SUNKEN, borderwidth=2, height=300)
        self.map_frame.pack(fill=tk.BOTH, expand=True)

        if TKINTERMAPVIEW_AVAILABLE:
            self.map_widget = tkintermapview.TkinterMapView(self.map_frame, width=700, height=500)
            self.map_widget.pack(fill=tk.BOTH, expand=True)
            self.map_widget.set_position(40.3573, -74.2635)
            self.map_widget.set_zoom(11)
        else:
            tk.Label(
                self.map_frame,
                text="Install tkintermapview to display the route map.\npip install tkintermapview",
                font=("Arial", 10),
                fg="blue"
            ).pack(fill=tk.BOTH, expand=True)

        # Right: Instructions
        instr_container = tk.Frame(right_panel)
        instr_container.pack(fill=tk.BOTH, expand=False)

        # Current instruction (large, bold)
        self.current_label = tk.Label(
            instr_container,
            text="Starting navigation...",
            font=("Arial", 14, "bold"),
            wraplength=300,
            justify="center",
            fg="darkblue",
            bg="lightyellow",
            padx=10,
            pady=10
        )
        self.current_label.pack(fill=tk.X, pady=(0, 10))

        # Upcoming instructions
        upcoming_frame = tk.LabelFrame(instr_container, text="Upcoming (Next 3)", padx=8, pady=8, font=("Arial", 9, "bold"))
        upcoming_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 10))

        self.upcoming_text = scrolledtext.ScrolledText(
            upcoming_frame,
            height=6,
            width=40,
            font=("Arial", 9),
            state=tk.DISABLED
        )
        self.upcoming_text.pack(fill=tk.BOTH, expand=True)

        # Progress
        self.progress_label = tk.Label(instr_container, text="", font=("Arial", 8), fg="gray")
        self.progress_label.pack(fill=tk.X, pady=(0, 10))

        # Next button
        self.next_btn = tk.Button(
            instr_container,
            text="Next Turn ➡️",
            command=self._on_next_turn,
            bg="lightgreen",
            font=("Arial", 11, "bold"),
            height=2
        )
        self.next_btn.pack(fill=tk.X)

    def on_show(self):
        """Update when shown."""
        self._update_display()
        self._draw_map()

    def _draw_map(self):
        """Draw the active route directly in tkintermapview."""
        if not TKINTERMAPVIEW_AVAILABLE or not self.map_widget:
            return

        if not self.app.current_route or not self.app.end_location:
            return

        route = self.app.current_route
        start = self.app.start_location
        end = self.app.end_location
        if not start:
            return

        self.map_widget.delete_all_path()
        self.map_widget.delete_all_marker()

        self.map_widget.set_position((start.lat + end.lat) / 2, (start.lon + end.lon) / 2)
        self.map_widget.set_zoom(route_zoom(route.distance_miles))

        coords = simplify_points(route.waypoints, max_points=400)
        if len(coords) >= 2:
            self.map_widget.set_path(coords, color=lighten_hex_color("#ff4d4d", 0.72), width=8)
            self.map_widget.set_path(coords, color="#ff4d4d", width=4)

        self.map_widget.set_marker(start.lat, start.lon, text="Start")
        self.map_widget.set_marker(end.lat, end.lon, text="End")
        for light_lat, light_lon in route.traffic_signal_points[:15]:
            self.map_widget.set_marker(light_lat, light_lon, text="TL")

        if route.steps and self.app.current_step_index < len(route.steps):
            step = route.steps[self.app.current_step_index]
            self.map_widget.set_marker(step.lat, step.lon, text="Current")

    def _update_display(self):
        """Refresh navigation display."""
        if not self.app.current_route:
            return

        route = self.app.current_route
        idx = self.app.current_step_index

        # Current instruction with distance
        if idx < len(route.steps):
            step = route.steps[idx]
            distance_ft = step.distance_m * 3.28084

            if distance_ft > 1000:
                distance_str = f"{distance_ft / 5280:.1f} mi"
            else:
                distance_str = f"{int(distance_ft)} ft"

            current_text = f"{step.instruction}\n({distance_str})"
            self.current_label.config(text=current_text)
        else:
            self.current_label.config(text="🎉 You have arrived!")
            self.next_btn.config(state=tk.DISABLED)
            return

        # Upcoming turns (next 3)
        self.upcoming_text.config(state=tk.NORMAL)
        self.upcoming_text.delete(1.0, tk.END)

        upcoming_count = 0
        for i in range(idx + 1, min(idx + 4, len(route.steps))):
            step = route.steps[i]
            distance_ft = step.distance_m * 3.28084

            if distance_ft > 1000:
                distance_str = f"{distance_ft / 5280:.1f} mi"
            else:
                distance_str = f"{int(distance_ft)} ft"

            turn_num = upcoming_count + 1
            self.upcoming_text.insert(tk.END, f"{turn_num}. {step.instruction} ({distance_str})\n\n")
            upcoming_count += 1

        if upcoming_count == 0:
            self.upcoming_text.insert(tk.END, "No more turns - destination ahead!")

        self.upcoming_text.config(state=tk.DISABLED)

        # Progress
        total_dist = route.distance_miles
        remaining_dist = sum(s.distance_m for s in route.steps[idx:]) * 0.000621371
        progress = ((total_dist - remaining_dist) / total_dist * 100) if total_dist > 0 else 0

        self.progress_label.config(
            text=f"Progress: {progress:.0f}% | Remaining: {remaining_dist:.1f} mi | Step {idx + 1}/{len(route.steps)}"
        )

    def _on_next_turn(self):
        """Advance to next turn."""
        if not self.app.current_route:
            return

        if self.app.current_step_index < len(self.app.current_route.steps) - 1:
            self.app.current_step_index += 1
            self._update_display()
            self._draw_map()


# ============================================================================
# MAIN
# ============================================================================

if __name__ == "__main__":
    app = NavigationApp()
    app.mainloop()
