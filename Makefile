.PHONY: up down logs ps migrate test lint

# Keep in sync with the ruff version in .github/workflows/ci.yml.
RUFF_VERSION := 0.16.10
RUFF := docker run --rm -v "$(CURDIR)":/io -w /io ghcr.io/astral-sh/ruff:$(RUFF_VERSION)

# --renew-anon-volumes: the frontend's anonymous node_modules volume would otherwise
# survive a rebuild and hide newly installed npm packages. Named volumes (data) are kept.
up:
	docker compose up -d --build --renew-anon-volumes

migrate:
	docker compose exec django python manage.py migrate

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
