.PHONY: help install dev up down logs test lint fmt psql seed

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Installe l'app + les deps de dev dans le venv courant
	pip install -e ".[dev]"

up: ## Demarre TimescaleDB + l'app (lab)
	docker compose up -d --build

down: ## Arrete la stack
	docker compose down

logs: ## Suit les logs de l'app
	docker compose logs -f app

dev: ## Lance l'API en rechargement a chaud (DB doit tourner)
	uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

test: ## Lance la suite pytest (aucune DB ni routeur requis)
	pytest -q

lint: ## Verifie le style
	ruff check app tests

fmt: ## Formate / corrige automatiquement
	ruff check --fix app tests && ruff format app tests

psql: ## Ouvre un psql sur la base de lab
	docker compose exec timescaledb psql -U $${POSTGRES_USER:-qos} -d $${POSTGRES_DB:-qos}
