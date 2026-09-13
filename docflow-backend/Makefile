.PHONY: install run test lint format typecheck up down db-up redis-up migrate-up migrate-down seed rotate-key

install:
	uv sync

run:
	uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

db-up:
	docker compose up -d db
	@echo "Waiting for Postgres to become healthy..."
	@until [ "$$(docker inspect -f '{{.State.Health.Status}}' docflow-backend-db-1 2>/dev/null)" = "healthy" ]; do sleep 1; done

redis-up:
	docker compose up -d redis
	@echo "Waiting for Redis to become healthy..."
	@until [ "$$(docker inspect -f '{{.State.Health.Status}}' docflow-backend-redis-1 2>/dev/null)" = "healthy" ]; do sleep 1; done

test: db-up redis-up
	uv run pytest

lint:
	uv run ruff check .

format:
	uv run ruff format .

typecheck:
	uv run mypy app alembic scripts

migrate-up: db-up
	uv run alembic upgrade head

migrate-down: db-up
	uv run alembic downgrade base

seed: db-up
	uv run python scripts/seed.py

# Requires ROTATE_OLD_KEY_VERSION / ROTATE_OLD_LOCAL_ENCRYPTION_KEY /
# ROTATE_NEW_KEY_VERSION / ROTATE_NEW_LOCAL_ENCRYPTION_KEY in the
# environment — see scripts/rotate_encryption_key.py's module docstring.
# Pass ARGS=--dry-run to preview without writing.
rotate-key: db-up
	uv run python scripts/rotate_encryption_key.py $(ARGS)

up:
	docker compose up --build

down:
	docker compose down
