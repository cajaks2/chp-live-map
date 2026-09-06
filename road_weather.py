"""Cached, elevation-aware road weather guidance for Crestmap regions."""

import datetime as dt
import json
import math
import os
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from temperature import PRIORITY_POINTS, ROAD_POINTS


CACHE_SECONDS = 15 * 60
FORECAST_HOURS = 6
MAX_AGE_SECONDS = 2 * 60 * 60
NWS_USER_AGENT = "Crestmap-road-weather/1.0 (+https://crestmap.us/about)"
REGION_ALERT_TERMS = {
    "forest": ("Los Angeles", "San Bernardino"),
    "malibu": ("Los Angeles", "Ventura"),
}
WEATHER_ALERT_EVENTS = (
    "Winter Storm", "Snow", "Ice", "Freezing", "Blizzard", "Flood",
    "Thunderstorm", "Rain", "Wind", "Dense Fog",
)

_cache = {}
_retry_after = {}
_lock = threading.Lock()


class RoadWeatherUnavailable(Exception):
    """No usable road forecast or alert data is available."""


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def forecast_points(region):
    """Use named anchors and a lighter road sample to limit the forecast request."""
    roads = ROAD_POINTS[region]
    stride = 2 if region == "forest" else 1
    return PRIORITY_POINTS.get(region, ()) + tuple(roads[::stride])


def parse_forecasts(payload, region, now):
    samples = forecast_points(region)
    rows = payload if isinstance(payload, list) else [payload]
    if len(rows) != len(samples):
        raise RoadWeatherUnavailable()
    points = []
    for sample, row in zip(samples, rows):
        if not isinstance(row, dict) or not _number(row.get("elevation")):
            continue
        hourly = row.get("hourly") or {}
        units = row.get("hourly_units") or {}
        times = hourly.get("time") or []
        temperatures = hourly.get("temperature_2m") or []
        probabilities = hourly.get("precipitation_probability") or []
        rain = hourly.get("rain") or []
        snowfall = hourly.get("snowfall") or []
        freezing = hourly.get("freezing_level_height") or []
        if units.get("temperature_2m") != "°F" or units.get("rain") != "inch" or units.get("snowfall") != "inch":
            continue
        count = min(FORECAST_HOURS, len(times), len(temperatures), len(probabilities), len(rain), len(snowfall), len(freezing))
        usable = []
        for index in range(count):
            values = (times[index], temperatures[index], probabilities[index], rain[index], snowfall[index], freezing[index])
            if not all(_number(value) for value in values):
                continue
            if -900 <= now - times[index] <= MAX_AGE_SECONDS or 0 <= times[index] - now <= FORECAST_HOURS * 3600:
                usable.append(values)
        if not usable:
            continue
        elevation_m = float(row["elevation"])
        min_temperature = min(item[1] for item in usable)
        probability = max(item[2] for item in usable)
        rain_inches = sum(max(0, item[3]) for item in usable)
        snow_inches = sum(max(0, item[4]) for item in usable)
        min_freezing_level = min(item[5] for item in usable)
        hazard = None
        if snow_inches >= 0.02:
            hazard = "snow"
        elif probability >= 30 and min_temperature <= 34 and min_freezing_level <= elevation_m + 300:
            hazard = "ice"
        elif probability >= 35 and rain_inches >= 0.02:
            hazard = "rain"
        if not hazard:
            continue
        if hazard == "snow":
            affected = [item for item in usable if item[4] >= 0.01]
        elif hazard == "ice":
            affected = [item for item in usable if item[2] >= 30 and item[1] <= 34 and item[5] <= elevation_m + 300]
        else:
            affected = [item for item in usable if item[2] >= 35 and item[3] >= 0.01]
        if not affected:
            affected = usable
        periods = []
        for item in affected:
            start = item[0]
            if periods and start <= periods[-1][1]:
                periods[-1][1] = max(periods[-1][1], start + 3600)
            else:
                periods.append([start, start + 3600])
        name, latitude, longitude = sample
        points.append({
            "name": name,
            "latitude": latitude,
            "longitude": longitude,
            "elevation_m": round(elevation_m, 1),
            "hazard": hazard,
            "precipitation_probability": round(probability),
            "rain_inches": round(rain_inches, 2),
            "snow_inches": round(snow_inches, 2),
            "minimum_temperature_f": round(min_temperature, 1),
            "minimum_freezing_level_m": round(min_freezing_level, 1),
            "starts_at": dt.datetime.fromtimestamp(affected[0][0], dt.timezone.utc).isoformat(),
            "ends_at": dt.datetime.fromtimestamp(affected[-1][0] + 3600, dt.timezone.utc).isoformat(),
            "periods": [
                {
                    "starts_at": dt.datetime.fromtimestamp(start, dt.timezone.utc).isoformat(),
                    "ends_at": dt.datetime.fromtimestamp(end, dt.timezone.utc).isoformat(),
                }
                for start, end in periods
            ],
            "valid_until": dt.datetime.fromtimestamp(usable[-1][0] + 3600, dt.timezone.utc).isoformat(),
        })
    return points


def parse_alerts(payload, region):
    alerts = []
    for feature in (payload.get("features") or []) if isinstance(payload, dict) else []:
        properties = feature.get("properties") or {}
        event = properties.get("event") or ""
        area = properties.get("areaDesc") or ""
        if not any(term.casefold() in area.casefold() for term in REGION_ALERT_TERMS[region]):
            continue
        if not any(term.casefold() in event.casefold() for term in WEATHER_ALERT_EVENTS):
            continue
        alerts.append({
            "id": properties.get("id") or feature.get("id"),
            "event": event,
            "severity": properties.get("severity") or "Unknown",
            "headline": properties.get("headline") or event,
            "area": area,
            "expires": properties.get("expires") or properties.get("ends"),
        })
    return alerts[:5]


def load_road_weather(region):
    if region not in ROAD_POINTS:
        raise ValueError("Unknown road-weather region")
    with _lock:
        now = time.time()
        cached = _cache.get(region)
        if cached and now - cached[0] < CACHE_SECONDS:
            return cached[1]
        if now < _retry_after.get(region, 0):
            raise RoadWeatherUnavailable()
        samples = forecast_points(region)
        params = {
            "latitude": ",".join(str(point[1]) for point in samples),
            "longitude": ",".join(str(point[2]) for point in samples),
            "hourly": "temperature_2m,precipitation_probability,rain,snowfall,freezing_level_height",
            "temperature_unit": "fahrenheit",
            "precipitation_unit": "inch",
            "forecast_hours": FORECAST_HOURS,
            "timeformat": "unixtime",
            "cell_selection": "land",
        }
        api_key = os.environ.get("OPEN_METEO_API_KEY")
        host = "customer-api.open-meteo.com" if api_key else "api.open-meteo.com"
        if api_key:
            params["apikey"] = api_key
        try:
            request = Request(f"https://{host}/v1/forecast?{urlencode(params)}", headers={"User-Agent": NWS_USER_AGENT})
            with urlopen(request, timeout=12) as response:
                forecast_payload = json.loads(response.read(512_000))
            points = parse_forecasts(forecast_payload, region, now)
        except Exception:
            _retry_after[region] = now + 60
            raise RoadWeatherUnavailable() from None

        alerts = []
        try:
            request = Request("https://api.weather.gov/alerts/active?area=CA", headers={"User-Agent": NWS_USER_AGENT, "Accept": "application/geo+json"})
            with urlopen(request, timeout=8) as response:
                alerts = parse_alerts(json.loads(response.read(1_000_000)), region)
        except Exception:
            pass
        result = {
            "region": region,
            "source": "NWS and Open-Meteo",
            "points": points,
            "alerts": alerts,
            "forecast_hours": FORECAST_HOURS,
            "fetched_at": dt.datetime.fromtimestamp(now, dt.timezone.utc).isoformat(),
        }
        _cache[region] = (now, result)
        _retry_after.pop(region, None)
        return result
