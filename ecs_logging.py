import datetime as dt
import json
import os
import sys
import traceback


SERVICE_NAME = os.environ.get("SERVICE_NAME", "chp-live-map")
SERVICE_VERSION = os.environ.get("SERVICE_VERSION", "0.1.1")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "production")


def utc_now():
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def log_event(level, message, **fields):
    event = {
        "@timestamp": utc_now(),
        "ecs.version": "8.11.0",
        "log.level": level,
        "message": message,
        "service.name": SERVICE_NAME,
        "service.version": SERVICE_VERSION,
        "event.dataset": SERVICE_NAME,
        "event.module": "chp",
        "event.kind": "event",
        "labels.environment": ENVIRONMENT,
        "process.pid": os.getpid(),
    }
    event.update({key: value for key, value in fields.items() if value is not None})
    print(json.dumps(event, separators=(",", ":"), sort_keys=True), flush=True)


def log_exception(message, exc, **fields):
    log_event(
        "error",
        message,
        **fields,
        **{
            "event.kind": "alert",
            "error.type": exc.__class__.__name__,
            "error.message": str(exc),
            "error.stack_trace": "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            ),
        },
    )


def run_main(main_func):
    try:
        main_func()
    except KeyboardInterrupt:
        log_event("info", "Shutdown requested", **{"event.action": "shutdown"})
        raise SystemExit(130)
    except Exception as exc:
        log_exception("Unhandled application error", exc, **{"event.action": "error"})
        raise SystemExit(1)
