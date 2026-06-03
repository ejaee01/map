# MyMap v0.0 - Setup & Installation Guide

## System Requirements
- Python 3.7+
- pip or conda package manager
- ~500MB disk space (for OSM data)
- Internet connection (for Nominatim geocoding and OSM data)

## Installation Steps

### 1. Install Dependencies
```bash
pip install osmnx networkx folium geopy tkinterweb tkintermapview
```

**Note**: `tkinterweb` is optional but required for embedded maps. Without it, you can still view maps by opening them in your browser.

For Windows users, if tkinterweb installation fails:
```bash
pip install tkinterweb --no-binary tkinterweb
```

### 2. Run the Application
```bash
python mymap_navigation.py
```

The GUI window will open. Default test locations are:
- **Start**: San Francisco, CA
- **End**: Oakland, CA

## How to Use

### Route Planning
1. Enter start and end locations (city names work best, e.g., "San Francisco, CA")
2. Click **"Calculate Routes"** 
   - First run will download street network (slow, ~30-60s for SF Bay Area)
   - Subsequent runs in same region are faster
3. Routes appear sorted by type: Fastest, Shortest, Free, Alternate

### Viewing Routes
- Click **"View Map"** to open an interactive Folium map in your browser
- Routes are color-coded (blue, green, purple, orange, red)
- Each route shows distance, time, and estimated cost on hover

### Navigation
1. Select a route from the list
2. Click **"Start Navigation"** to begin step-by-step navigation
3. Click **"Next Step"** to advance through turn-by-turn instructions
4. Progress bar tracks distance/time remaining

## Features Implemented

✅ **A* Pathfinding**: Real graph-based pathfinding with heuristic search
✅ **Multiple Route Types**: Fastest (time), Shortest (distance), Free (toll-avoidant), Alternate
✅ **Real OSM Data**: Uses OpenStreetMap street networks via OSMnx
✅ **Traffic Simulation**: Realistic traffic conditions (green/yellow/red) along routes
✅ **Embedded Maps**: Maps display directly in the Python GUI (no browser needed)
✅ **Traffic Visualization**: Routes color-coded by traffic conditions on the map
✅ **Turn-by-Turn Navigation**: Generated turn instructions with step tracking
✅ **Route Metrics**: Distance (km/mi), time, cost (tolls), computation time
✅ **Graphical UI**: Full Tkinter GUI, no terminal interaction required
✅ **Location Search**: Address geocoding via Nominatim/OpenStreetMap  

## Troubleshooting

### "ModuleNotFoundError: No module named 'osmnx'"
```bash
pip install --upgrade osmnx networkx folium geopy
```

### "Could not find locations"
- Try entering full addresses with cities: "1234 Main St, San Francisco, CA"
- Or just city pairs: "San Francisco, CA" to "Oakland, CA"

### Network download too slow
- First download takes time. Subsequent runs in same region use cache.
- Try smaller regions (e.g., single cities instead of states)

### Map doesn't open in browser
- Check your default browser is set
- Manual workaround: map HTML file is in system temp directory

### Optional toll pricing
- If you want more accurate toll costs, set `MAPQUEST_API_KEY` in your environment.
- The app will use MapQuest's toll-aware route cost when available, and fall back to a heuristic estimate otherwise.

## Mobile / iPhone Version

The mobile entry point is [`mobile0.1.py`](./mobile0.1.py). It is a Kivy app that follows the phone's live GPS position and recenters the map automatically.

### Important iOS note
- You cannot run the Tkinter desktop app directly on iPhone.
- You will need to package the Python app with a Kivy iOS toolchain.
- Add location permission strings to the iOS app bundle so the system can grant GPS access.

### Mobile dependencies
```bash
pip install kivy plyer kivy-garden.mapview
```

### What it does
- Shows a live OpenStreetMap view
- Uses the phone's actual GPS location
- Keeps the map centered on your position while follow mode is enabled
- Updates the marker as you move, which is useful for driving or walking

## Architecture Notes

- **GeoUtils**: Geocoding and distance calculations (Haversine formula)
- **RoutingEngine**: Loads OSM graphs, runs A* pathfinding, simulates traffic
- **RouteMetrics**: Stores computed route results
- **NavigationState**: Tracks step-by-step navigation progress
- **MapVisualizer**: Generates Folium HTML maps
- **MyMapApp**: Tkinter GUI orchestrating all components

All code is in a single `mymap_navigation.py` file (~650 lines, well-commented).
