SHELL := /bin/bash

IMAGE_REPO ?= cajaks2/chp-live-map
VERSION ?= 0.1.72
PLATFORM ?= linux/amd64
MANIFEST ?= k8s/chp-live-map.yaml
NAMESPACE ?= chp-live-map
DEPLOYMENT ?= chp-live-map-web
PUBLIC_URL ?= https://crestmap.us/
PYTHON ?= python3
VENV ?= .venv

.PHONY: help venv test coverage build push update-manifest apply rollout deploy verify k8s-status

help:
	@printf '%s\n' \
		'Targets:' \
		'  make venv                         Install local development dependencies' \
		'  make test                         Run unit tests' \
		'  make coverage                     Run tests with coverage report' \
		'  make build VERSION=0.1.72         Build and push linux/amd64 image' \
		'  make update-manifest VERSION=0.1.72 Update image tags and SERVICE_VERSION in k8s manifest' \
		'  make apply                        kubectl apply manifest' \
		'  make rollout                      Wait for web deployment rollout' \
		'  make deploy VERSION=0.1.72        Build, push, update manifest, apply, and wait' \
		'  make verify                       Check public URL and recent pod logs' \
		'  make k8s-status                   Show pods, ingress, cronjob, and service'

venv:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/python -m pip install -r requirements-dev.txt

test:
	$(VENV)/bin/python -m pytest -q

coverage:
	$(VENV)/bin/python -m pytest \
		--cov=scrape_chp_traffic \
		--cov=generate_live_map \
		--cov=serve_live_map \
		--cov=ecs_logging \
		--cov-report=term-missing

build push:
	docker buildx build --platform $(PLATFORM) -t $(IMAGE_REPO):$(VERSION) --push .

update-manifest:
	perl -0pi -e 's|image: $(IMAGE_REPO):[0-9]+\.[0-9]+\.[0-9]+|image: $(IMAGE_REPO):$(VERSION)|g; s|value: "[0-9]+\.[0-9]+\.[0-9]+"|value: "$(VERSION)"|g' $(MANIFEST)

apply:
	kubectl apply -f $(MANIFEST)

rollout:
	kubectl -n $(NAMESPACE) rollout status deployment/$(DEPLOYMENT) --timeout=120s

deploy: test build update-manifest apply rollout verify

verify:
	curl -k -fsS $(PUBLIC_URL) -o /tmp/chp-live-map-verify.html
	rg -n 'CHP Forest Incidents|last 72h|setView' /tmp/chp-live-map-verify.html
	rg -n 'Last checked <time id="generated-at"' /tmp/chp-live-map-verify.html
	kubectl -n $(NAMESPACE) logs -l app=$(DEPLOYMENT) --tail=10 --since=5m

k8s-status:
	kubectl -n $(NAMESPACE) get pods,ingress,cronjob,svc
