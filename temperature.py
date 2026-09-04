"""Bounded, cached elevation-adjusted air temperature estimates from Open-Meteo."""

import datetime as dt
import json
import math
import os
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# Fixed sample locations, not weather stations. Elevations come from the provider's
# terrain model at the requested coordinate; never substitute model grid coordinates.
# Road sample coordinates are selected from the existing Caltrans/LA County
# forest mile markers and OSM-derived Malibu corridor geometry in this project.
# The extra terrain sample covers Mount Wilson. More labels appear as you zoom.
SAMPLE_POINTS = {
    "forest": (
        ('Mount Wilson area', 34.225, -118.057),
        ('Angeles Crest Highway · mile 25', 34.214157, -118.200201),
        ('Angeles Crest Highway · mile 30', 34.257238, -118.196628),
        ('Angeles Crest Highway · mile 35', 34.266057, -118.134584),
        ('Angeles Crest Highway · mile 41', 34.267809, -118.06927),
        ('Angeles Crest Highway · mile 46', 34.285096, -117.993434),
        ('Angeles Crest Highway · mile 51', 34.330667, -117.999783),
        ('Angeles Crest Highway · mile 56', 34.353079, -117.946586),
        ('Angeles Crest Highway · mile 61', 34.352558, -117.886415),
        ('Angeles Crest Highway · mile 66', 34.34889, -117.82924),
        ('Angeles Crest Highway · mile 72', 34.368809, -117.781006),
        ('Angeles Crest Highway · mile 77', 34.376329, -117.726422),
        ('Angeles Crest Highway · mile 82', 34.367502, -117.656901),
        ('Angeles Forest Highway · mile 0.81', 34.498346, -118.114513),
        ('Angeles Forest Highway · mile 10.74', 34.388247, -118.086089),
        ('Angeles Forest Highway · mile 17.13', 34.32358, -118.127637),
        ('Angeles Forest Highway · mile 24.98', 34.271479, -118.154222),
        ('Big Tujunga · mile 0.04', 34.2923, -118.28586),
        ('Big Tujunga · mile 3.57', 34.294489, -118.236576),
        ('Big Tujunga · mile 6.02', 34.282837, -118.20736),
        ('Big Tujunga · mile 9.94', 34.297002, -118.161954),
        ('Upper Big Tujunga · mile 0.02', 34.328687, -118.119827),
        ('Upper Big Tujunga · mile 3.05', 34.310395, -118.091554),
        ('Upper Big Tujunga · mile 6.2', 34.29143, -118.05022),
        ('Upper Big Tujunga · mile 9', 34.273543, -118.042378),
        ('Glendora Mountain · mile 0.16', 34.229789, -117.773863),
        ('Glendora Mountain · mile 4.38', 34.207165, -117.80155),
        ('Glendora Mountain · mile 10.14', 34.171527, -117.848269),
        ('Glendora Mountain · mile 14', 34.155449, -117.836643),
        ('Glendora Ridge · mile 0.16', 34.203687, -117.806336),
        ('Glendora Ridge · mile 4.55', 34.217726, -117.751811),
        ('Glendora Ridge · mile 7.9', 34.219375, -117.711292),
        ('Glendora Ridge · mile 11.93', 34.235399, -117.662231),
        ('Highway 39 · mile 17.14', 34.158534, -117.903956),
        ('Highway 39 · mile 22.81', 34.210513, -117.864833),
        ('Highway 39 · mile 29.57', 34.265133, -117.844701),
        ('Highway 39 · mile 38.4', 34.312098, -117.839472),
        ('Mount Baldy · mile 0.42', 34.230969, -117.663264),
        ('Mount Baldy · mile 2.63', 34.204482, -117.676734),
        ('Mount Baldy · mile 4.36', 34.180142, -117.677659),
        ('Mount Baldy · mile 6.3', 34.154477, -117.685729),
    ),
    "malibu": (
        ('Pacific Coast Highway', 34.0125, -118.4975),
        ('Pacific Coast Highway', 34.0386, -118.5561),
        ('Pacific Coast Highway', 34.0382, -118.6227),
        ('Pacific Coast Highway', 34.0342, -118.6874),
        ('Pacific Coast Highway', 34.0261, -118.7634),
        ('Pacific Coast Highway', 34.0224, -118.8305),
        ('Pacific Coast Highway', 34.0405, -118.8871),
        ('Pacific Coast Highway', 34.0534, -118.9639),
        ('Pacific Coast Highway', 34.0794, -119.0277),
        ('Pacific Coast Highway', 34.1072, -119.0793),
        ('Topanga Canyon Boulevard', 34.0401, -118.5793),
        ('Topanga Canyon Boulevard', 34.1022, -118.5915),
        ('Topanga Canyon Boulevard', 34.1461, -118.6056),
        ('Malibu Canyon / Las Virgenes', 34.0347, -118.7034),
        ('Malibu Canyon / Las Virgenes', 34.0802, -118.7037),
        ('Malibu Canyon / Las Virgenes', 34.1413, -118.6999),
        ('Kanan Road', 34.0277, -118.7995),
        ('Kanan Road', 34.1073, -118.8048),
        ('Kanan Road', 34.1422, -118.762),
        ('Decker Road', 34.0415, -118.8944),
        ('Decker Road', 34.0682, -118.8943),
        ('Decker Road', 34.0886, -118.8734),
        ('Encinal Canyon Road', 34.0943, -118.8284),
        ('Encinal Canyon Road', 34.0775, -118.8777),
        ('Encinal Canyon Road', 34.0404, -118.8852),
        ('Latigo Canyon Road', 34.03, -118.7547),
        ('Latigo Canyon Road', 34.0679, -118.7805),
        ('Latigo Canyon Road', 34.0896, -118.8154),
        ('Tuna Canyon Road', 34.0774, -118.6064),
        ('Tuna Canyon Road', 34.0605, -118.6176),
        ('Tuna Canyon Road', 34.0395, -118.5895),
    ),
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
            "kind": "estimate",
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
