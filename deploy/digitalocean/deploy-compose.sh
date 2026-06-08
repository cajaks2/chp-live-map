#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

docker compose pull web scrape
docker compose up -d --no-deps web scrape
docker compose up -d --no-deps postgres-backup
docker compose ps
