# Windows without make: run the commands by hand, e.g.  .venv\Scripts\python -m pytest tests\unit\test_config.py -q
.PHONY: help setup infra-up infra-down db-create db-migrate topics dev test lint clean

help:
	@echo "organisation-service (port 8010)"
	@echo "  make setup                - copy .env.example to .env (if missing) and install dependencies with uv"
	@echo "  make infra-up             - start the shared dev infra (../docker-compose.yml)"
	@echo "  make infra-down           - stop the shared dev infra"
	@echo "  make db-create            - create organisation_service_db in the shared Postgres (idempotent)"
	@echo "  make db-migrate           - apply Alembic migrations (none until Phase 2)"
	@echo "  make topics               - create the Kafka topics this service produces (safe to run twice)"
	@echo "  make dev                  - run the API on 127.0.0.1:8010 with reload"
	@echo "  make test FILE=tests/unit/test_config.py - run ONE test file"
	@echo "  make lint                 - ruff check"
	@echo "  make clean                - remove caches"

setup:
	@python -c "import os, shutil; os.path.exists('.env') or shutil.copy('.env.example', '.env')"
	uv sync

infra-up:
	docker compose -f ../docker-compose.yml up -d

infra-down:
	docker compose -f ../docker-compose.yml down

db-create:
	uv run python -m scripts.create_db

db-migrate:
	uv run alembic upgrade head

topics:
	uv run python -m scripts.create_topics

dev:
	uv run uvicorn app.main:app --host 127.0.0.1 --port 8010 --reload --proxy-headers --forwarded-allow-ips "$${FORWARDED_ALLOW_IPS:-127.0.0.1}"

# One file at a time; the full suite is run by the owner.
test:
	@test -n "$(FILE)" || (echo "usage: make test FILE=tests/unit/test_config.py" && exit 1)
	uv run python -m pytest $(FILE) -q

lint:
	uv run ruff check app tests scripts

clean:
	@python -c "import shutil, glob; [shutil.rmtree(p, ignore_errors=True) for p in glob.glob('**/__pycache__', recursive=True) + ['.pytest_cache', '.ruff_cache']]"
