.PHONY: up down logs ps migrate test

up:
	docker compose up -d --build

migrate:
	docker compose exec django python manage.py migrate

test:
	docker compose exec -e DJANGO_SETTINGS_MODULE=config.settings.test django pytest

down:
	docker compose down

logs:
	docker compose logs -f $(s)

ps:
	docker compose ps
