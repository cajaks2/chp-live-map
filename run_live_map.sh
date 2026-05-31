#!/bin/sh
set -eu

DATA_DIR="${DATA_DIR:-/mnt/data/chp_map}"
DATABASE="${DATABASE:-$DATA_DIR/chp_traffic.sqlite}"
OUTPUT="${OUTPUT:-$DATA_DIR/live_chp_map.html}"
INTERVAL_SECONDS="${INTERVAL_SECONDS:-60}"
MAP_HOURS="${MAP_HOURS:-24}"
HTTP_PORT="${HTTP_PORT:-8080}"

mkdir -p "$DATA_DIR"

run_cycle() {
  python3 /app/scrape_chp_traffic.py --database "$DATABASE" || true
  python3 /app/generate_live_map.py \
    --database "$DATABASE" \
    --output "$OUTPUT" \
    --hours "$MAP_HOURS" || true
}

python3 -m http.server "$HTTP_PORT" --directory "$DATA_DIR" &
server_pid="$!"

trap 'kill "$server_pid"; wait "$server_pid" 2>/dev/null || true' INT TERM

run_cycle

while true; do
  sleep "$INTERVAL_SECONDS" &
  wait "$!" || true
  run_cycle
done
