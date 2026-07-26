.PHONY: up down logs ps build test test-queue lint shell mongo redis clean check-home

COMPOSE := docker compose

up: check-home ## Build and start the whole stack
	$(COMPOSE) up -d --build
	@echo ""
	@echo "  UI   http://localhost:5173"
	@echo "  API  http://localhost:8000/docs"
	@echo ""
	@echo "Waiting for readiness..."
	@for i in $$(seq 1 30); do \
	  if curl -fsS http://localhost:8000/readyz >/dev/null 2>&1; then echo "READY"; exit 0; fi; \
	  sleep 2; \
	done; \
	echo "NOT READY - check 'make logs'"; curl -sS http://localhost:8000/readyz || true; exit 1

down: ## Stop the stack (volumes preserved)
	$(COMPOSE) down

logs: ## Tail logs from all services
	$(COMPOSE) logs -f --tail=100

ps:
	$(COMPOSE) ps

build:
	$(COMPOSE) build

test: ## Run the backend test suite
	$(COMPOSE) exec -T api pytest -v

test-queue: ## Run only the queue tests
	$(COMPOSE) exec -T api pytest tests/queue -v

lint:
	$(COMPOSE) exec -T api ruff check app

shell:
	$(COMPOSE) exec api bash

mongo:
	$(COMPOSE) exec mongo mongosh biopipe

redis:
	$(COMPOSE) exec redis redis-cli

clean: ## Stop and DELETE the mongo/redis volumes. Does not touch BIOINFO_HOME.
	$(COMPOSE) down -v

check-home: ## Verify BIOINFO_HOME exists and is writable on the host
	@test -f .env || (echo "ERROR: no .env file. Run: cp .env.example .env"; exit 1)
	@HOME_DIR=$$(grep -E '^BIOINFO_HOME=' .env | cut -d= -f2-); \
	PARENT=$$(dirname "$$HOME_DIR"); \
	if [ ! -d "$$PARENT" ]; then \
	  echo "ERROR: $$PARENT does not exist. Is the drive mounted?"; exit 1; fi; \
	if [ ! -w "$$PARENT" ]; then \
	  echo "ERROR: $$PARENT is not writable."; exit 1; fi; \
	echo "BIOINFO_HOME parent OK: $$PARENT"
