"""Process-local telemetry for cached weather provider refreshes."""

from collections import defaultdict
import threading


_lock = threading.Lock()
_refresh_total = defaultdict(int)
_cache_total = defaultdict(int)
_provider_total = defaultdict(int)
_last_duration = {}
_last_success = {}
_point_counts = {}


def record_cache(pipeline, region, outcome):
    with _lock:
        _cache_total[(pipeline, region, outcome)] += 1


def record_provider(pipeline, region, provider, outcome, count=1):
    with _lock:
        _provider_total[(pipeline, region, provider, outcome)] += count


def record_refresh(pipeline, region, outcome, duration_seconds, timestamp=None, counts=None):
    with _lock:
        _refresh_total[(pipeline, region, outcome)] += 1
        _last_duration[(pipeline, region)] = max(0.0, float(duration_seconds))
        if outcome == "success":
            _last_success[(pipeline, region)] = float(timestamp)
            for kind, value in (counts or {}).items():
                _point_counts[(pipeline, region, kind)] = max(0, int(value))


def snapshot():
    with _lock:
        return {
            "refresh_total": dict(_refresh_total),
            "cache_total": dict(_cache_total),
            "provider_total": dict(_provider_total),
            "last_duration": dict(_last_duration),
            "last_success": dict(_last_success),
            "point_counts": dict(_point_counts),
        }


def reset():
    """Clear telemetry for isolated tests."""
    with _lock:
        _refresh_total.clear()
        _cache_total.clear()
        _provider_total.clear()
        _last_duration.clear()
        _last_success.clear()
        _point_counts.clear()
