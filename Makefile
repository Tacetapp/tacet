.PHONY: help setup app dev test build
.DEFAULT_GOAL := help

help:
	@echo "Tacet — available targets:"
	@echo ""
	@echo "  make setup   One-time setup: venv, deps, frontend, Swift binary, models"
	@echo "  make app     Run the desktop app  [THE product — end users start here]"
	@echo ""
	@echo "  Contributor targets:"
	@echo "  make dev     Run the dev server at http://localhost:8000 (config via .env)"
	@echo "  make test    Run backend tests"
	@echo "  make build   Rebuild the frontend bundle"

setup:
	./setup.sh

app:
	cd electron && npm install --silent && npm start

dev:
	./start.sh

test:
	.venv/bin/python -m pytest web/backend/tests/ -q

build:
	cd web/frontend && npm run build
