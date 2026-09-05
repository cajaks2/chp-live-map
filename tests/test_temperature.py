import datetime as dt
import io
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

import app
import temperature as weather
from app import WebSettings, create_app
from generate_live_map import build_html

NOW = 1788550000


def payload(region="forest"):
    return [{"elevation": 1650, "current_units": {"temperature_2m": "°F"},
             "current": {"temperature_2m": 54.2, "time": NOW - 300}}
            for _ in weather.SAMPLE_POINTS[region]]


@pytest.fixture(autouse=True)
def clean_cache(monkeypatch):
    weather._cache.clear()
    weather._retry_after.clear()
    monkeypatch.delenv("OPEN_METEO_API_KEY", raising=False)
    monkeypatch.setattr(weather.time, "time", lambda: NOW)
    monkeypatch.setattr(weather, "load_station_observations", lambda _region, _now: [])


@pytest.mark.parametrize("region", ["forest", "malibu"])
def test_samples_form_a_broad_terrain_grid(region):
    points = weather.SAMPLE_POINTS[region]
    assert len(points) >= 50
    assert len({latitude for _name, latitude, _longitude in points}) >= 5
    assert len({longitude for _name, _latitude, longitude in points}) >= 8
    assert all(
        "road" not in name.casefold() and "highway" not in name.casefold()
        for name, _latitude, _longitude in points
        if name not in weather.PRIORITY_POINT_NAMES
        and name not in weather.ROAD_POINT_NAMES
    )


def test_named_landmark_samples_are_priority_points():
    expected_forest_names = [
        "Newcomb's Ranch",
        "Highway 39 lower canyon",
        "Highway 39 upper canyon",
        "GMR / GRR junction",
        "Glendora Ridge Road east",
        "Mount Baldy Road upper canyon",
    ]
    assert [point[0] for point in weather.SAMPLE_POINTS["forest"][:6]] == expected_forest_names
    assert weather.SAMPLE_POINTS["malibu"][0] == (
        "Rock Store / Old Place area", 34.112087, -118.783186
    )
    for region in ("forest", "malibu"):
        result = weather.parse_estimates(payload(region), region, NOW)
        priority_count = len(weather.PRIORITY_POINTS[region])
        assert all(point["priority"] is True for point in result["points"][:priority_count])
        assert all(point["road"] is True for point in result["points"][:priority_count])
        assert all(point["priority"] is False for point in result["points"][priority_count:])


def test_road_samples_form_the_baseline_in_both_regions():
    assert len(weather.ROAD_POINTS["forest"]) >= 70
    assert len(weather.ROAD_POINTS["malibu"]) >= 15
    for region in ("forest", "malibu"):
        names = {name for name, _latitude, _longitude in weather.ROAD_POINTS[region]}
        result = weather.parse_estimates(payload(region), region, NOW)
        road_points = [point for point in result["points"] if point["name"] in names]
        assert {point["name"] for point in road_points} == names
        assert all(point["road"] is True for point in road_points)
        assert all(point["priority"] is False for point in road_points)


@pytest.mark.parametrize("region", ["forest", "malibu"])
def test_coordinates_are_requested_points_and_elevation_is_provider_terrain(region):
    data = payload(region)
    data[0].update(latitude=0, longitude=0)
    result = weather.parse_estimates(data, region, NOW)
    assert result["points"][0]["latitude"] == weather.SAMPLE_POINTS[region][0][1]
    assert result["points"][0]["elevation_m"] == 1650
    assert all(p["kind"] == "estimate" for p in result["points"])


@pytest.mark.parametrize("field,value", [("temperature_2m", None), ("temperature_2m", float("nan")),
                                        ("temperature_2m", 200), ("time", NOW - 3601),
                                        ("time", NOW + 901)])
def test_invalid_values_are_omitted(field, value):
    data = payload()
    data[0]["current"][field] = value
    assert len(weather.parse_estimates(data, "forest", NOW)["points"]) == len(data) - 1


def test_wrong_units_and_missing_elevation_are_not_displayed():
    data = payload()
    data[0]["current_units"]["temperature_2m"] = "°C"
    data[1]["elevation"] = None
    assert len(weather.parse_estimates(data, "forest", NOW)["points"]) == len(data) - 2
    with pytest.raises(weather.TemperatureUnavailable):
        weather.parse_estimates([], "forest", NOW)


def test_fresh_nws_station_observation_is_measured():
    station = weather.OBSERVATION_STATIONS["forest"][0]
    timestamp = dt.datetime.fromtimestamp(NOW - 600, dt.timezone.utc).isoformat()
    point = weather.parse_station_observation({
        "properties": {
            "timestamp": timestamp,
            "temperature": {"value": 20.0, "unitCode": "wmoUnit:degC"},
        }
    }, station, NOW)
    assert point == {
        "name": "Chilao RAWS", "station_id": "CHOC1",
        "latitude": 34.33167, "longitude": -118.03028,
        "temperature_f": 68.0, "elevation_m": 1661.16,
        "valid_at": timestamp, "kind": "observation",
        "priority": False, "road": False,
    }


@pytest.mark.parametrize("temperature,timestamp,unit", [
    (None, NOW - 60, "wmoUnit:degC"),
    (20, NOW - 5401, "wmoUnit:degC"),
    (20, NOW - 60, "wmoUnit:degF"),
])
def test_invalid_nws_station_observation_is_omitted(temperature, timestamp, unit):
    iso = dt.datetime.fromtimestamp(timestamp, dt.timezone.utc).isoformat()
    payload = {"properties": {"timestamp": iso, "temperature": {
        "value": temperature, "unitCode": unit,
    }}}
    assert weather.parse_station_observation(
        payload, weather.OBSERVATION_STATIONS["forest"][0], NOW
    ) is None


def test_station_observations_augment_estimates(monkeypatch):
    observed = {
        "name": "Chilao RAWS", "station_id": "CHOC1",
        "latitude": 34.33167, "longitude": -118.03028,
        "temperature_f": 68.0, "elevation_m": 1661.16,
        "valid_at": dt.datetime.fromtimestamp(NOW - 60, dt.timezone.utc).isoformat(),
        "kind": "observation", "priority": False, "road": False,
    }
    monkeypatch.setattr(weather, "load_station_observations", lambda _region, _now: [observed])
    monkeypatch.setattr(weather, "urlopen", lambda *_args, **_kwargs: io.BytesIO(
        json.dumps(payload()).encode()
    ))
    result = weather.load_temperatures("forest")
    assert result["points"][0] == observed
    assert result["sources"] == ["NWS", "Open-Meteo"]


def test_cache_batches_and_key_stays_in_server_request(monkeypatch):
    requests = []
    def fetch(request, timeout):
        requests.append(request.full_url)
        return io.BytesIO(json.dumps(payload()).encode())
    monkeypatch.setattr(weather, "urlopen", fetch)
    monkeypatch.setenv("OPEN_METEO_API_KEY", "synthetic-test-key")
    first = weather.load_temperatures("forest")
    assert weather.load_temperatures("forest") == first
    assert len(requests) == 1
    url = urlsplit(requests[0])
    params = parse_qs(url.query)
    assert url.hostname == "customer-api.open-meteo.com"
    assert "elevation" not in params
    assert params["cell_selection"] == ["land"]
    assert len(params["latitude"][0].split(",")) == len(weather.SAMPLE_POINTS["forest"])
    assert "synthetic-test-key" not in json.dumps(first)
    monkeypatch.setattr(weather.time, "time", lambda: NOW + 901)
    weather.load_temperatures("forest")
    assert len(requests) == 2


def test_failure_backoff_and_no_secret_error(monkeypatch):
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        raise OSError("synthetic-secret-url")
    monkeypatch.setattr(weather, "urlopen", fail)
    for _ in range(2):
        with pytest.raises(weather.TemperatureUnavailable) as exc:
            weather.load_temperatures("forest")
        assert "secret" not in str(exc.value)
    assert len(calls) == 1


@pytest.mark.parametrize("region", ["forest", "malibu"])
def test_endpoint_and_local_render(tmp_path, monkeypatch, region):
    result = weather.parse_estimates(payload(region), region, NOW)
    monkeypatch.setattr(app, "load_temperatures", lambda requested: result if requested == region else None)
    with TestClient(create_app(WebSettings(database=tmp_path / "map.sqlite", base_path="/map"))) as client:
        response = client.get(f"/map/api/v1/temperature?region={region}")
        assert response.status_code == 200
        assert response.json() == result
        assert response.headers["Cache-Control"] == "public, max-age=60"
        assert client.head(f"/map/api/v1/temperature?region={region}").content == b""
        def fail(_):
            raise weather.TemperatureUnavailable()
        monkeypatch.setattr(app, "load_temperatures", fail)
        assert client.get("/api/v1/temperature").status_code == 503
    rendered = build_html([], "2026-09-04T12:00:00-07:00", 72, region=region, base_path="/map")
    assert 'const temperatureEndpoint = "/map/api/v1/temperature"' in rendered
    assert "__TEMPERATURE_ENDPOINT__" not in rendered
    assert "temperature-label" in rendered
    assert "orderedPoints" in rendered
    assert "displayRank" in rendered
    assert "map.getZoom() < 11" in rendered
    assert "nearbyIncident && !point.road" in rendered
    assert "point.priority ? 6 : 8" in rendered
    assert "is-above" in rendered
    assert "is-below" in rendered
    assert "Measured + estimated" in rendered
    assert "National Weather Service" in rendered
    assert "point.priority ? 12 : 32" in rendered
    assert '" is-left"' in rendered
    assert "Temperature estimates:" not in rendered


def test_cached_points_expire_by_model_time(monkeypatch):
    data = payload()
    for row in data:
        row["current"]["time"] = NOW - 3590
    monkeypatch.setattr(weather, "urlopen", lambda *a, **k: io.BytesIO(json.dumps(data).encode()))
    weather.load_temperatures("forest")
    monkeypatch.setattr(weather.time, "time", lambda: NOW + 20)
    with pytest.raises(weather.TemperatureUnavailable):
        weather.load_temperatures("forest")
