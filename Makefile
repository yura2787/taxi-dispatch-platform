.PHONY: up down logs ps migrate test lint osrm seed-drivers

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

# Dev driver profiles 1..n in Redis, for connecting with dev tokens: make seed-drivers n=500
n ?= 100
seed-drivers:
	docker compose exec realtime python -m app.devtools seed-drivers --count $(n)

# Realtime tests use a separate Redis DB so they never touch dev data in DB 0.
test:
	docker compose exec -e DJANGO_SETTINGS_MODULE=config.settings.test django pytest
	docker compose exec -e REDIS_URL=redis://redis:6379/15 realtime pytest

# `run` (not `exec`) gives a fresh container, so node_modules always matches package.json.
lint:
	$(RUFF) check core realtime
	$(RUFF) format --check core realtime
	docker compose run --rm --no-deps --build frontend sh -c "npm run lint && npm run format:check"

down:
	docker compose down

logs:
	docker compose logs -f $(s)

ps:
	docker compose ps
