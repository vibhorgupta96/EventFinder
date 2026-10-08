.DEFAULT_GOAL := help

.PHONY: help install start stop restart status logs smoke smoke-live demo test lint migrate

help:
	@printf "Targets: install start stop restart status logs smoke smoke-live demo test lint migrate\\n"

install:
	uv sync --extra dev

start stop restart status logs:
	uv run eventfinderctl $@

migrate:
	uv run alembic upgrade head

smoke:
	uv run python -m eventfinder.smoke

smoke-live:
	uv run python -m eventfinder.live_smoke

demo:
	uv run python -m eventfinder.demo

test:
	uv run pytest -q

lint:
	uv run ruff check eventfinder tests
