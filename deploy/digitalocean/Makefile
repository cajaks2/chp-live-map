SHELL := /usr/bin/env bash

COMPOSE ?= docker compose
VERSION ?=
WEB_HEALTH_URL ?= http://127.0.0.1:8080/healthz
SCRAPER_HEALTH_URL ?= http://127.0.0.1:8081/healthz
PUBLIC_URL ?= https://crestmap.us/
ENV_FILE ?= .env

.PHONY: help init ps pull up deploy deploy-app deploy-full restart-web restart-scrape logs logs-web logs-scrape health verify metrics backup set-version

help:
	@printf '%s\n' \
		'DigitalOcean Compose targets:' \
		'  make init                 Create .env from .env.example if missing' \
		'  make ps                   Show compose service status' \
		'  make pull                 Pull web and scrape images' \
		'  make up                   Start the full stack' \
		'  make deploy               App-only deploy without restarting Postgres' \
		'  make deploy VERSION=x.y.z Set VERSION in .env, pull, app-only deploy, verify' \
		'  make deploy-full          Recreate the full stack, including dependencies' \
		'  make restart-web          Restart only web without dependencies' \
		'  make restart-scrape       Restart only scrape without dependencies' \
		'  make logs                 Tail all service logs' \
		'  make logs-web             Tail web logs' \
		'  make logs-scrape          Tail scraper logs' \
		'  make health               Check local web and scraper health endpoints' \
		'  make verify               Check health and public page' \
		'  make metrics              Print web and scraper metrics headers' \
		'  make backup               Run one manual Postgres backup into ./backups/postgres' \
		'  make set-version VERSION=x.y.z  Update VERSION in .env only'

init:
	@if [[ ! -f "$(ENV_FILE)" ]]; then cp .env.example "$(ENV_FILE)"; fi

ps:
	$(COMPOSE) ps

pull:
	$(COMPOSE) pull web scrape

up: init
	$(COMPOSE) up -d
	$(COMPOSE) ps

deploy: set-version pull deploy-app verify

deploy-app:
	$(COMPOSE) up -d --no-deps web scrape
	$(COMPOSE) up -d --no-deps postgres-backup
	$(COMPOSE) ps

deploy-full: init
	$(COMPOSE) pull
	$(COMPOSE) up -d
	$(COMPOSE) ps
	$(MAKE) verify

restart-web:
	$(COMPOSE) up -d --no-deps --force-recreate web
	$(MAKE) health

restart-scrape:
	$(COMPOSE) up -d --no-deps --force-recreate scrape
	$(MAKE) health

logs:
	$(COMPOSE) logs -f --tail=100

logs-web:
	$(COMPOSE) logs -f --tail=100 web

logs-scrape:
	$(COMPOSE) logs -f --tail=100 scrape

health:
	curl -fsS "$(WEB_HEALTH_URL)"
	@printf '\n'
	curl -fsS "$(SCRAPER_HEALTH_URL)"
	@printf '\n'

verify: health
	curl -fsS "$(PUBLIC_URL)" -o /tmp/chp-live-map-do-verify.html
	grep -E 'CHP Forest Incidents|View last updated|rel="icon"' /tmp/chp-live-map-do-verify.html

metrics:
	curl -fsS http://127.0.0.1:8080/metrics | head -40
	@printf '\n--- scraper ---\n'
	curl -fsS http://127.0.0.1:8081/metrics | head -40

backup:
	@set -a; source "$(ENV_FILE)"; set +a; \
	mkdir -p backups/postgres; \
	ts=$$(date -u +%Y%m%dT%H%M%SZ); \
	$(COMPOSE) exec -T postgres pg_dump -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" -Fc --clean --if-exists > "backups/postgres/$${POSTGRES_DB}-$${ts}.dump"; \
	ls -lh "backups/postgres/$${POSTGRES_DB}-$${ts}.dump"

set-version: init
	@if [[ -n "$(VERSION)" ]]; then \
		if grep -q '^VERSION=' "$(ENV_FILE)"; then \
			sed -i.bak 's/^VERSION=.*/VERSION=$(VERSION)/' "$(ENV_FILE)" && rm -f "$(ENV_FILE).bak"; \
		else \
			printf '\nVERSION=%s\n' "$(VERSION)" >> "$(ENV_FILE)"; \
		fi; \
		printf 'Set VERSION=%s in %s\n' "$(VERSION)" "$(ENV_FILE)"; \
	else \
		printf 'VERSION unchanged. Set VERSION=x.y.z to update %s.\n' "$(ENV_FILE)"; \
	fi
