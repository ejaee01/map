"""
MyMap Mobile v0.1

Live GPS-follow map for iPhone/iPad in pure Python.

This version is intentionally separate from the desktop Tkinter app because iOS
does not run Tkinter natively. It uses Kivy for the UI and plyer for device GPS.

To package for iOS, you will still need a Kivy iOS build toolchain and the
appropriate location permissions in the app bundle.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from kivy.app import App
from kivy.clock import Clock
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.togglebutton import ToggleButton

try:
    from kivy_garden.mapview import MapMarker, MapView
except Exception:
    from mapview import MapMarker, MapView  # type: ignore

try:
    from plyer import gps
except Exception:
    gps = None


@dataclass
class GPSFix:
    lat: float
    lon: float
    speed: Optional[float] = None
    bearing: Optional[float] = None
    accuracy: Optional[float] = None
    altitude: Optional[float] = None


def _safe_float(value, default=None):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Approximate distance between two GPS points."""
    earth_radius_m = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2.0) ** 2
    return 2.0 * earth_radius_m * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


class MobileMapRoot(BoxLayout):
    def __init__(self, **kwargs):
        super().__init__(orientation="vertical", padding=dp(8), spacing=dp(8), **kwargs)

        self.follow_mode = True
        self.current_fix: Optional[GPSFix] = None
        self.last_marker_fix: Optional[GPSFix] = None
        self.marker: Optional[MapMarker] = None

        self.status_label = Label(
            text="Waiting for GPS fix...",
            size_hint_y=None,
            height=dp(28),
            halign="left",
            valign="middle",
        )
        self.status_label.bind(size=self._update_text_size)

        self.coords_label = Label(
            text="Lat: --   Lon: --   Speed: --",
            size_hint_y=None,
            height=dp(24),
            halign="left",
            valign="middle",
        )
        self.coords_label.bind(size=self._update_text_size)

        self.map_view = MapView(zoom=17, lat=0.0, lon=0.0, map_source="osm")

        control_row = BoxLayout(size_hint_y=None, height=dp(52), spacing=dp(8))
        self.follow_button = ToggleButton(text="Follow", state="down")
        self.follow_button.bind(on_press=self.toggle_follow)
        self.center_button = Button(text="Center Now")
        self.center_button.bind(on_press=self.center_now)
        self.clear_button = Button(text="Reset Marker")
        self.clear_button.bind(on_press=self.reset_marker)

        control_row.add_widget(self.follow_button)
        control_row.add_widget(self.center_button)
        control_row.add_widget(self.clear_button)

        top_panel = BoxLayout(orientation="vertical", size_hint_y=None, height=dp(60), spacing=dp(2))
        top_panel.add_widget(self.status_label)
        top_panel.add_widget(self.coords_label)

        self.add_widget(top_panel)
        self.add_widget(self.map_view)
        self.add_widget(control_row)

        Clock.schedule_once(self.start_gps, 0.25)

    def _update_text_size(self, instance, _size):
        instance.text_size = (instance.width, None)

    def toggle_follow(self, *_):
        self.follow_mode = self.follow_button.state == "down"
        self.status_label.text = "Follow mode on" if self.follow_mode else "Follow mode paused"
        if self.follow_mode and self.current_fix:
            self._center_map(self.current_fix)

    def center_now(self, *_):
        if self.current_fix:
            self._center_map(self.current_fix, force_zoom=True)

    def reset_marker(self, *_):
        if self.marker is not None:
            try:
                self.map_view.remove_marker(self.marker)
            except Exception:
                self.map_view.remove_widget(self.marker)
            self.marker = None
        self.last_marker_fix = None
        self.status_label.text = "Marker reset"

    def start_gps(self, *_):
        if gps is None:
            self.status_label.text = "plyer.gps is not installed on this build"
            return

        try:
            gps.configure(on_location=self.on_location, on_status=self.on_status)
            gps.start(minTime=1000, minDistance=1)
            self.status_label.text = "GPS started. Waiting for signal..."
        except Exception as exc:
            self.status_label.text = f"GPS unavailable: {exc}"

    def stop_gps(self):
        if gps is None:
            return
        try:
            gps.stop()
        except Exception:
            pass

    def on_status(self, stype, status):
        Clock.schedule_once(lambda _dt: self._set_status(f"GPS {stype}: {status}"))

    def on_location(self, **kwargs):
        fix = GPSFix(
            lat=_safe_float(kwargs.get("lat")),
            lon=_safe_float(kwargs.get("lon")),
            speed=_safe_float(kwargs.get("speed")),
            bearing=_safe_float(kwargs.get("bearing")),
            accuracy=_safe_float(kwargs.get("accuracy")),
            altitude=_safe_float(kwargs.get("altitude")),
        )

        if fix.lat is None or fix.lon is None:
            return

        Clock.schedule_once(lambda _dt: self._apply_fix(fix))

    def _apply_fix(self, fix: GPSFix):
        self.current_fix = fix

        if self.marker is None:
            self.marker = MapMarker(lat=fix.lat, lon=fix.lon)
            self.map_view.add_marker(self.marker)
        else:
            self.marker.lat = fix.lat
            self.marker.lon = fix.lon

        if self.last_marker_fix is None:
            self.last_marker_fix = fix
        else:
            moved = _distance_meters(
                self.last_marker_fix.lat,
                self.last_marker_fix.lon,
                fix.lat,
                fix.lon,
            )
            if moved >= 8.0:
                self.last_marker_fix = fix

        self._set_status("Live GPS fix acquired")
        speed_text = "--"
        if fix.speed is not None:
            speed_text = f"{fix.speed:.1f} m/s"
        accuracy_text = "--"
        if fix.accuracy is not None:
            accuracy_text = f"{fix.accuracy:.1f} m"
        self.coords_label.text = (
            f"Lat: {fix.lat:.6f}   Lon: {fix.lon:.6f}   "
            f"Speed: {speed_text}   Accuracy: {accuracy_text}"
        )

        if self.follow_mode:
            self._center_map(fix)

    def _center_map(self, fix: GPSFix, force_zoom: bool = False):
        self.map_view.center_on(fix.lat, fix.lon)
        if force_zoom:
            self.map_view.zoom = 17

    def _set_status(self, text: str):
        self.status_label.text = text


class MyMapMobileApp(App):
    def build(self):
        self.title = "MyMap Mobile"
        return MobileMapRoot()

    def on_stop(self):
        root = self.root
        if root is not None:
            root.stop_gps()


if __name__ == "__main__":
    MyMapMobileApp().run()
