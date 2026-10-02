.PHONY: up down logs ps migrate test lint

# Keep in sync with the ruff version in .github/workflows/ci.yml.
RUFF_VERSION := 0.16.10
RUFF := docker run --rm -v "$(CURDIR)":/io -w /io ghcr.io/astral-sh/ruff:$(RUFF_VERSION)

up:
	docker compose up -d --build

migrate:
	docker compose exec django python manage.py migrate

test:
	docker compose exec -e DJANGO_SETTINGS_MODULE=config.settings.test django pytest
	docker compose exec realtime pytest

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
