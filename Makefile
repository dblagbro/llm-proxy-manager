.PHONY: install dev test lint build up down logs shell

install:
	pip install -e ".[dev]"

dev:
	uvicorn app.main:app --reload --port 3000

test:
	pytest tests/unit -v

test-all:
	pytest tests/ -v

lint:
	ruff check app/ tests/
	ruff format --check app/ tests/

format:
	ruff format app/ tests/

build:
	sudo docker build -t llm-proxy2:latest .

up:
	sudo docker compose up -d llm-proxy2

down-container:
	sudo docker stop llm-proxy2 && sudo docker rm llm-proxy2

logs:
	sudo docker logs -f llm-proxy2

shell:
	sudo docker exec -it llm-proxy2 /bin/sh

migrate:
	alembic upgrade head

migrate-new:
	alembic revision --autogenerate -m "$(MSG)"
