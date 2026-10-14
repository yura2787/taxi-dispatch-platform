.PHONY: up down logs ps migrate test lint osrm seed-drivers sim loadtest check-osrm-map

# Keep in sync with the ruff version in .github/workflows/ci.yml.
RUFF_VERSION := 0.16.10
RUFF := docker run --rm -v "$(CURDIR)":/io -w /io ghcr.io/astral-sh/ruff:$(RUFF_VERSION)

# Written by infra/osrm/prepare.sh once the graph is complete.
OSRM_STAMP := infra/osrm/data/chernivtsi.stamp

# --renew-anon-volumes: the frontend's anonymous node_modules volume would otherwise
# survive a rebuild and hide newly installed npm packages. Named volumes (data) are kept.
# Without a graph, osrm is not started at all (it would only exit with an error).
up:
	@if [ -f $(OSRM_STAMP) ]; then \
		docker compose up -d --build --renew-anon-volumes; \
	else \
		printf '\033[33m%s\033[0m\n' "No OSRM map - run 'make osrm'. The app will work without routes."; \
		docker compose up -d --build --renew-anon-volumes --scale osrm=0; \
	fi

# Downloads the pinned map and builds the routing graph; does nothing if it is up to date.
osrm:
	./infra/osrm/prepare.sh

migrate:
	docker compose exec django python manage.py migrate

# Dev driver profiles first..first+n-1 in Redis, for connecting with dev tokens:
# make seed-drivers n=500 first=1001
n ?= 100
first ?= 1
seed-drivers:
	docker compose exec realtime python -m app.devtools seed-drivers --count $(n) --first-id $(first)

# A live city: driver bots on Chernivtsi streets. Runs until Ctrl+C unless duration
# is given: make sim drivers=200 [duration=300] [flaky=0.2] [first=1]
drivers ?= 100
duration ?=
flaky ?= 0.05
# Built on its own: `run --build` would also rebuild and recreate the services the
# simulator depends on (and recreate realtime without the load test settings).
sim: check-osrm-map
	$(MAKE) seed-drivers n=$(drivers) first=$(first)
	docker compose --profile sim build simulator
	docker compose --profile sim run --rm simulator python -m sim run \
		--drivers $(drivers) --first-id $(first) --flaky-share $(flaky) \
		$(if $(duration),--duration $(duration))

# How much the location intake takes: make loadtest [drivers=1000] [duration=300]
# realtime runs without --reload for the run (docker-compose.loadtest.yml) and goes
# back to dev mode afterwards, also when the run fails or is interrupted. No flaky
# bots by default: their silences end in refused points, which are not the load.
# The summary goes into the description of the pull request.
LOAD_COMPOSE := docker compose -f docker-compose.yml -f docker-compose.loadtest.yml
# Own defaults unless given (on the command line or in the environment), not the ones
# of `make sim`; an empty value falls back to the default too.
given = $(filter command line environment,$(origin $(1)))
load_drivers = $(or $(if $(call given,drivers),$(drivers)),1000)
load_duration = $(or $(if $(call given,duration),$(duration)),300)
load_flaky = $(or $(if $(call given,flaky),$(flaky)),0)
# The trap comes first, so dev realtime is restored whatever fails or is interrupted
# from then on, and exactly once (INT/TERM only exit, EXIT restores). nginx is
# started here: --no-deps below starts nothing, and a dependency started by `run`
# would come from docker-compose.yml alone, i.e. realtime with --reload again.
loadtest: check-osrm-map
	docker compose --profile sim build simulator
	@trap 'docker compose up -d --wait realtime' EXIT; trap 'exit 130' INT; trap 'exit 143' TERM; \
	$(LOAD_COMPOSE) up -d --wait realtime nginx && \
	docker compose exec realtime python -m app.devtools seed-drivers \
		--count $(load_drivers) --first-id $(first) && \
	docker compose --profile sim run --rm --no-deps simulator python -m sim run \
		--drivers $(load_drivers) --first-id $(first) --flaky-share $(load_flaky) \
		--duration $(load_duration)

check-osrm-map:
	@if [ ! -f $(OSRM_STAMP) ]; then \
		printf '\033[31m%s\033[0m\n' "No OSRM map - run 'make osrm' first: the bots drive along its roads."; \
		exit 1; \
	fi

# Realtime tests use a separate Redis DB so they never touch dev data in DB 0.
test:
	docker compose exec -e DJANGO_SETTINGS_MODULE=config.settings.test django pytest
	docker compose exec -e REDIS_URL=redis://redis:6379/15 realtime pytest
	docker compose --profile sim run --rm --no-deps --build simulator pytest

# `run` (not `exec`) gives a fresh container, so node_modules always matches package.json.
lint:
	$(RUFF) check core realtime simulator
	$(RUFF) format --check core realtime simulator
	docker compose run --rm --no-deps --build frontend sh -c "npm run lint && npm run format:check"

down:
	docker compose down

logs:
	docker compose logs -f $(s)

ps:
	docker compose ps
