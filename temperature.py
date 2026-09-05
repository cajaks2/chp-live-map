"""Bounded, cached elevation-adjusted air temperature estimates from Open-Meteo."""

import datetime as dt
import json
import math
import os
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from geo_bounds import REGION_BOUNDS, coordinates_in_region_bounds
from mile_markers import MILE_MARKERS

GRID_SPACING = {
    "forest": (0.055, 0.065),
    "malibu": (0.040, 0.055),
}

# Named terrain anchors appear before the general grid so the client retains
# them when nearby temperature labels need to be thinned at the current zoom.
PRIORITY_POINTS = {
    "forest": (
        ("Newcomb's Ranch", 34.329766, -118.002015),
        ("Highway 39 lower canyon", 34.236361, -117.851057),
        ("Highway 39 upper canyon", 34.286386, -117.843999),
        ("GMR / GRR junction", 34.203687, -117.806336),
        ("Glendora Ridge Road east", 34.220110, -117.712940),
        ("Mount Baldy Road upper canyon", 34.230969, -117.663264),
    ),
    "malibu": (("Rock Store / Old Place area", 34.112087, -118.783186),),
}
PRIORITY_POINT_NAMES = {
    name for points in PRIORITY_POINTS.values() for name, _latitude, _longitude in points
}

ROAD_NAMES = {
    "crest": "Angeles Crest Highway",
    "forest": "Angeles Forest Highway",
    "big_tujunga": "Big Tujunga Canyon Road",
    "upper_big_tujunga": "Upper Big Tujunga Canyon Road",
    "glendora_mountain": "Glendora Mountain Road",
    "glendora_ridge": "Glendora Ridge Road",
    "highway_39": "Highway 39",
    "mount_baldy": "Mount Baldy Road",
}


def forest_road_sample_points():
    """Select the surveyed marker nearest each five-mile interval per road."""
    samples = []
    for road, points in MILE_MARKERS.items():
        nearest = {}
        for mile, latitude, longitude in points:
            target = round(float(mile) / 5) * 5
            distance = abs(float(mile) - target)
            if target not in nearest or distance < nearest[target][0]:
                nearest[target] = (distance, mile, latitude, longitude)
        for _distance, mile, latitude, longitude in nearest.values():
            samples.append(
                (f"{ROAD_NAMES[road]} near mile {mile:g}", latitude, longitude)
            )
    return tuple(samples)


# These points follow the principal Malibu corridors already drawn by the app.
# They supplement the terrain grid without claiming to be pavement readings.
MALIBU_ROAD_POINTS = (
    ("Pacific Coast Highway near Santa Monica", 34.0261, -118.5155),
    ("Pacific Coast Highway near Topanga", 34.0394, -118.6035),
    ("Pacific Coast Highway near Malibu Canyon", 34.0338, -118.7151),
    ("Pacific Coast Highway near Kanan Dume", 34.0166, -118.8191),
    ("Pacific Coast Highway near Point Dume", 34.0405, -118.8871),
    ("Pacific Coast Highway near Trancas", 34.0613, -118.9836),
    ("Pacific Coast Highway near Point Mugu", 34.0939, -119.0689),
    ("Topanga Canyon Boulevard lower canyon", 34.0742, -118.5885),
    ("Topanga Canyon Boulevard upper canyon", 34.1367, -118.5991),
    ("Malibu Canyon Road lower canyon", 34.0537, -118.6966),
    ("Malibu Canyon Road upper canyon", 34.1173, -118.7085),
    ("Kanan Dume Road lower canyon", 34.0589, -118.7988),
    ("Kanan Road upper canyon", 34.1304, -118.7632),
    ("Decker Road lower canyon", 34.0501, -118.8976),
    ("Decker Road upper canyon", 34.0835, -118.8782),
    ("Encinal Canyon Road", 34.0775, -118.8777),
    ("Latigo Canyon Road", 34.0633, -118.7780),
    ("Tuna Canyon Road", 34.0605, -118.6176),
)
ROAD_POINTS = {
    "forest": forest_road_sample_points(),
    "malibu": MALIBU_ROAD_POINTS,
}
ROAD_POINT_NAMES = {
    name for points in ROAD_POINTS.values() for name, _latitude, _longitude in points
}


def terrain_sample_points(region):
    """Build a staggered, evenly distributed grid inside the product region."""
    lat_min, lat_max, lon_min, lon_max = REGION_BOUNDS[region]
    lat_step, lon_step = GRID_SPACING[region]
    points = []
    row = 0
    latitude = lat_min + lat_step / 2
    while latitude < lat_max:
        longitude = lon_min + lon_step / 2 + (row % 2) * lon_step / 2
        while longitude < lon_max:
            latitude_value = round(latitude, 6)
            longitude_value = round(longitude, 6)
            if coordinates_in_region_bounds(latitude_value, longitude_value, region):
                near_baseline = any(
                    abs(anchor_latitude - latitude_value) < lat_step / 2
                    and abs(anchor_longitude - longitude_value) < lon_step / 2
                    for _name, anchor_latitude, anchor_longitude
                    in PRIORITY_POINTS.get(region, ()) + ROAD_POINTS.get(region, ())
                )
                if not near_baseline:
                    points.append(
                        (f"{region.title()} terrain sample", latitude_value, longitude_value)
                    )
            longitude += lon_step
        row += 1
        latitude += lat_step
    return tuple(points)


# Road locations provide an elevation-sensitive driving baseline while a thinner
# terrain grid fills gaps around them. These are model locations, not stations.
SAMPLE_POINTS = {
    region: (PRIORITY_POINTS.get(region, ()) + ROAD_POINTS.get(region, ())
             + terrain_sample_points(region))
    for region in REGION_BOUNDS
}
CACHE_SECONDS = 900
MAX_AGE_SECONDS = 3600
_cache = {}
_retry_after = {}
_lock = threading.Lock()


class TemperatureUnavailable(Exception):
    """The provider has no fresh, usable estimates."""


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def parse_estimates(payload, region, now):
    """Reject missing, implausible and stale values rather than invent readings."""
    samples = SAMPLE_POINTS[region]
    if not isinstance(payload, list) or len(payload) != len(samples):
        raise TemperatureUnavailable()
    points = []
    for sample, row in zip(samples, payload):
        if not isinstance(row, dict):
            continue
        current = row.get("current") or {}
        if not isinstance(current, dict):
            continue
        value = current.get("temperature_2m")
        elevation = row.get("elevation")
        timestamp = current.get("time")
        units = row.get("current_units") or {}
        if not isinstance(units, dict) or units.get("temperature_2m") != "°F":
            continue
        if not all(_number(v) for v in (value, elevation, timestamp)):
            continue
        if not (-100 <= value <= 150 and -500 <= elevation <= 9000):
            continue
        if not (-900 <= now - timestamp <= MAX_AGE_SECONDS):
            continue
        name, latitude, longitude = sample
        points.append({
            "name": name, "latitude": latitude, "longitude": longitude,
            "temperature_f": round(value, 1), "elevation_m": elevation,
            "valid_at": dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat(),
            "kind": "estimate", "priority": name in PRIORITY_POINT_NAMES,
            "road": name in ROAD_POINT_NAMES,
        })
    if not points:
        raise TemperatureUnavailable()
    return {"region": region, "source": "Open-Meteo", "points": points,
            "fetched_at": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat()}


def load_temperatures(region):
    """One batch per region per 15 minutes, with bounded retries on failure."""
    if region not in SAMPLE_POINTS:
        raise ValueError("Unknown temperature region")
    with _lock:
        now = time.time()
        cached = _cache.get(region)
        if cached and now - cached[0] < CACHE_SECONDS:
            fresh_points = [point for point in cached[1]["points"]
                            if now - dt.datetime.fromisoformat(point["valid_at"]).timestamp() <= MAX_AGE_SECONDS]
            if fresh_points:
                return {**cached[1], "points": fresh_points}
        if now < _retry_after.get(region, 0):
            raise TemperatureUnavailable()
        samples = SAMPLE_POINTS[region]
        params = {
            "latitude": ",".join(str(p[1]) for p in samples),
            "longitude": ",".join(str(p[2]) for p in samples),
            "current": "temperature_2m", "temperature_unit": "fahrenheit",
            "timeformat": "unixtime", "cell_selection": "land",
            # Leaving elevation unset enables the provider's 90 m DEM downscaling.
        }
        api_key = os.environ.get("OPEN_METEO_API_KEY")
        host = "customer-api.open-meteo.com" if api_key else "api.open-meteo.com"
        if api_key:
            params["apikey"] = api_key
        request = Request(f"https://{host}/v1/forecast?{urlencode(params)}",
                          headers={"User-Agent": "Crestmap-temperature/1.0"})
        try:
            with urlopen(request, timeout=10) as response:
                payload = json.loads(response.read(256_000))
            result = parse_estimates(payload, region, now)
        except Exception:
            # Do not log request URLs: customer URLs contain the API key.
            _retry_after[region] = now + 60
            raise TemperatureUnavailable() from None
        _cache[region] = (now, result)
        _retry_after.pop(region, None)
        return result
