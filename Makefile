.PHONY: up down logs ps

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f $(s)

ps:
	docker compose ps
