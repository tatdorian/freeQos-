.PHONY: help install dev up down update reset-db logs test lint fmt typecheck hooks lock psql seed

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Installe l'app + les deps de dev dans le venv courant
	pip install -e ".[dev]"

up: ## Demarre TimescaleDB + l'app (lab)
	docker compose up -d --build

down: ## Arrete la stack (les donnees sont conservees)
	docker compose down

update: ## Recupere le code a jour et redemarre l'app (donnees conservees)
	git pull --ff-only
	docker compose up -d --build
	@echo "Application a jour. Videz le cache du navigateur (Ctrl+Maj+R) : app.js est mis en cache."

reset-db: ## EFFACE la base (mesures, inventaire, topologie) et redemarre a vide
	@echo "Cette commande EFFACE toutes les donnees : mesures, routeurs declares,"
	@echo "antennes, topologie, reglages et clients statiques. Ctrl-C pour annuler."
	@# printf plutot que 'read -p' : make execute ses recettes avec /bin/sh, qui
	@# est dash sur Debian et Ubuntu, et dash ignore l'option -p. L'invite ne
	@# s'affichait pas -- on ne voyait qu'un curseur qui attend sans rien dire.
	@printf "Taper 'oui' pour confirmer : "; read r; [ "$$r" = "oui" ] || \
		{ echo "Annule : rien n'a ete efface."; exit 1; }
	docker compose down -v
	docker compose up -d --build
	@echo "Base recreee a vide. La cle de chiffrement a ete regeneree : redeclarez"
	@echo "vos routeurs dans l'onglet Equipements."

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

typecheck: ## Verifie les types (mypy --strict sur app/, config dans pyproject)
	mypy

hooks: ## Installe les hooks git pre-commit (fmt + lint + typage avant commit)
	pre-commit install

lock: ## Regenere le verrou de dependances d'execution depuis pyproject.toml
	rm -rf /tmp/freeqos-lock && python -m venv /tmp/freeqos-lock && \
	/tmp/freeqos-lock/bin/pip install -q --upgrade pip && \
	/tmp/freeqos-lock/bin/pip install -q . && \
	/tmp/freeqos-lock/bin/pip freeze | grep -viE '^freeqos|@ file://|^-e ' | LC_ALL=C sort > requirements.lock

psql: ## Ouvre un psql sur la base de lab
	docker compose exec timescaledb psql -U $${POSTGRES_USER:-qos} -d $${POSTGRES_DB:-qos}
