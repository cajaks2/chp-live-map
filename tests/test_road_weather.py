import datetime as dt
import io
import json
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

import app
import road_weather as weather
from app import WebSettings, create_app
from generate_live_map import build_html


NOW = 1788550000


def forecast_row(*, temperature=42.0, probability=70, rain=0.08, snow=0.0, freezing=2500):
    times = [NOW + index * 3600 for index in range(weather.FORECAST_HOURS)]
    return {
        "elevation": 1800,
        "hourly_units": {"temperature_2m": "°F", "rain": "inch", "snowfall": "inch"},
        "hourly": {
            "time": times,
            "temperature_2m": [temperature] * len(times),
            "precipitation_probability": [probability] * len(times),
            "rain": [rain] * len(times),
            "snowfall": [snow] * len(times),
            "freezing_level_height": [freezing] * len(times),
        },
    }


def forecast_payload(region="forest", **values):
    return [forecast_row(**values) for _ in weather.forecast_points(region)]


def setup_function():
    weather._cache.clear()
    weather._retry_after.clear()


def test_forecasts_classify_rain_snow_and_possible_ice():
    rain = weather.parse_forecasts(forecast_payload(rain=0.04), "forest", NOW)
    snow = weather.parse_forecasts(forecast_payload(rain=0, snow=0.08), "forest", NOW)
    ice = weather.parse_forecasts(
        forecast_payload(temperature=31, rain=0, probability=55, freezing=1750), "forest", NOW
    )
    assert rain and {point["hazard"] for point in rain} == {"rain"}
    assert snow and {point["hazard"] for point in snow} == {"snow"}
    assert ice and {point["hazard"] for point in ice} == {"ice"}
    assert rain[0]["starts_at"] == dt.datetime.fromtimestamp(NOW, dt.timezone.utc).isoformat()
    assert rain[0]["ends_at"] == dt.datetime.fromtimestamp(NOW + weather.FORECAST_HOURS * 3600, dt.timezone.utc).isoformat()
    assert rain[0]["periods"] == [{
        "starts_at": dt.datetime.fromtimestamp(NOW, dt.timezone.utc).isoformat(),
        "ends_at": dt.datetime.fromtimestamp(NOW + weather.FORECAST_HOURS * 3600, dt.timezone.utc).isoformat(),
    }]


def test_dry_forecasts_do_not_add_map_markers():
    points = weather.parse_forecasts(
        forecast_payload("malibu", probability=10, rain=0, snow=0), "malibu", NOW
    )
    assert points == []


def test_forecast_keeps_separate_rain_periods():
    payload = forecast_payload("malibu", probability=10, rain=0, snow=0)
    for row in payload:
        row["hourly"]["precipitation_probability"][1] = 70
        row["hourly"]["rain"][1] = 0.05
        row["hourly"]["precipitation_probability"][4] = 70
        row["hourly"]["rain"][4] = 0.05
    point = weather.parse_forecasts(payload, "malibu", NOW)[0]
    assert len(point["periods"]) == 2


def test_alerts_are_filtered_to_region_and_weather_events():
    result = weather.parse_alerts({"features": [
        {"properties": {"id": "one", "event": "Winter Storm Warning", "areaDesc": "Los Angeles County", "severity": "Severe", "headline": "Heavy snow expected"}},
        {"properties": {"id": "two", "event": "Fire Weather Watch", "areaDesc": "San Diego County", "severity": "Moderate", "headline": "Elsewhere"}},
    ]}, "forest")
    assert [alert["event"] for alert in result] == ["Winter Storm Warning"]


def test_load_batches_forecasts_and_keeps_key_server_side(monkeypatch):
    calls = []
    def fetch(request, timeout):
        calls.append(request.full_url)
        if "api.weather.gov" in request.full_url:
            return io.BytesIO(b'{"features": []}')
        return io.BytesIO(json.dumps(forecast_payload()).encode())
    monkeypatch.setattr(weather.time, "time", lambda: NOW)
    monkeypatch.setattr(weather, "urlopen", fetch)
    monkeypatch.setenv("OPEN_METEO_API_KEY", "synthetic-key")
    result = weather.load_road_weather("forest")
    assert weather.load_road_weather("forest") == result
    open_meteo_url = urlsplit(calls[0])
    params = parse_qs(open_meteo_url.query)
    assert open_meteo_url.hostname == "customer-api.open-meteo.com"
    assert params["forecast_hours"] == [str(weather.FORECAST_HOURS)]
    assert "synthetic-key" not in json.dumps(result)
    assert len(calls) == 2


def test_endpoint_and_map_layer_menu(tmp_path, monkeypatch):
    result = {
        "region": "forest", "source": "NWS and Open-Meteo", "points": [], "alerts": [],
        "forecast_hours": 6, "fetched_at": dt.datetime.fromtimestamp(NOW, dt.timezone.utc).isoformat(),
    }
    monkeypatch.setattr(app, "load_road_weather", lambda region: result)
    with TestClient(create_app(WebSettings(database=tmp_path / "map.sqlite", base_path="/map"))) as client:
        response = client.get("/map/api/v1/road-weather?region=forest")
        assert response.status_code == 200
        assert response.json() == result
        assert response.headers["Cache-Control"] == "public, max-age=60"
    rendered = build_html([], "2026-09-06T10:00:00-07:00", 72, region="forest", base_path="/map")
    assert 'class="map-layer-menu"' in rendered
    assert 'data-road-weather-layer-toggle' in rendered
    assert 'const endpoint = "/map/api/v1/road-weather"' in rendered
    assert "Rain · snow · ice by elevation" in rendered
    assert rendered.index('class="map-layer-menu"') > rendered.index('<main id="map">')
    header_menu = rendered[rendered.index('<details class="view-menu">'):rendered.index('</details></div>', rendered.index('<details class="view-menu">'))]
    assert "Air temperature" not in header_menu
    assert "Fire cameras" not in header_menu
    assert "Fire cameras" in rendered
    assert ".road-weather-label.is-rain span" in rendered
    assert "point.hazard.toUpperCase()" in rendered
    assert 'className: "road-weather-map-popup"' in rendered
    assert "forecastWindow" in rendered
    assert "bindTooltip(label" not in rendered
    assert "Timing is hourly guidance and may shift" in rendered
    assert "if (popupOpen) return" in rendered
    assert 'marker.on("popupopen"' in rendered
    assert 'if (menu.open && !menu.contains(event.target))' in rendered
