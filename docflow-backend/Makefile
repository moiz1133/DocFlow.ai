.PHONY: install run test lint format typecheck up down db-up migrate-up migrate-down seed

install:
	uv sync

run:
	uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

db-up:
	docker compose up -d db
	@echo "Waiting for Postgres to become healthy..."
	@until [ "$$(docker inspect -f '{{.State.Health.Status}}' docflow-backend-db-1 2>/dev/null)" = "healthy" ]; do sleep 1; done

test: db-up
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

up:
	docker compose up --build

down:
	docker compose down
