.PHONY: up down logs ps build containers test test-serial test-fast test-queue lint shell mongo redis clean check-home release release-launcher backup restore backup-verify

COMPOSE := docker compose

# Workers for the parallel test phase. Override per-invocation:
#   make test PYTEST_WORKERS=4
#
# 8, not `auto`: `auto` is one worker per core, and the Docker VM this runs in
# reports 24 CPUs against 12.4 GB of RAM, so `auto` sizes the run by the
# resource that is not scarce. Measured on the full suite (6299 tests) --
# 4: 56s, 8: 39s, 12: 36s, 16: 35s -- so 8 takes about 90% of the available
# speedup, and everything past it buys seconds while multiplying the memory a
# second agent's concurrent run has to fit alongside. Peak api-container
# memory at 8 workers was 2.4 GB.
PYTEST_WORKERS ?= 8

# The parallel phase excludes `heavy`; the sequential phase runs only those,
# after it, with nothing else alive. `|| [ $$? -eq 5 ]` tolerates pytest's
# exit 5 ("no tests collected"), which is what the heavy phase returns while
# the marker has no members -- deliberately, so the split costs nothing until
# a test earns the mark.
PYTEST_PAR := -m "not heavy" -n $(PYTEST_WORKERS) --dist loadgroup

containers: ## Rebuild and restart api/web/worker, then restart worker (picks up handler code)
	$(COMPOSE) up -d --build api web worker
	$(COMPOSE) restart worker

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

test: ## Run the backend test suite (parallel; PYTEST_WORKERS=N to change)
	$(COMPOSE) exec -T api pytest $(PYTEST_PAR) --tb=short
	$(COMPOSE) exec -T api pytest -m heavy --tb=short || [ $$? -eq 5 ]

test-serial: ## Run the backend test suite one test at a time
	$(COMPOSE) exec -T api pytest -v

test-fast: ## Run fast unit tests (skipping slow tests)
	$(COMPOSE) exec -T api pytest -m "not slow and not heavy" -n $(PYTEST_WORKERS) --dist loadgroup --tb=short
	$(COMPOSE) exec -T api pytest -m "heavy and not slow" --tb=short || [ $$? -eq 5 ]

test-queue: ## Run only the queue tests
	$(COMPOSE) exec -T api pytest tests/queue $(PYTEST_PAR) --tb=short
	$(COMPOSE) exec -T api pytest tests/queue -m heavy --tb=short || [ $$? -eq 5 ]

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

backup: ## Back up the Mongo database and enumerate /data (BACKUP_DIR= to redirect)
	./ops/backup.sh backup

restore: ## Restore a backup. Overwrites the database. BACKUP=<dir> required.
	@test -n "$(BACKUP)" || (echo "ERROR: set BACKUP=<dir>, e.g. make restore BACKUP=backups/2026-08-17T134502Z"; exit 1)
	./ops/backup.sh restore "$(BACKUP)"

backup-verify: ## Check /data against a backup's manifest. BACKUP=<dir> required.
	@test -n "$(BACKUP)" || (echo "ERROR: set BACKUP=<dir>"; exit 1)
	./ops/backup.sh verify "$(BACKUP)"

check-home: ## Verify BIOINFO_HOME exists and is writable on the host
	@test -f .env || (echo "ERROR: no .env file. Run: cp .env.example .env"; exit 1)
	@HOME_DIR=$$(grep -E '^BIOINFO_HOME=' .env | cut -d= -f2-); \
	PARENT=$$(dirname "$$HOME_DIR"); \
	if [ ! -d "$$PARENT" ]; then \
	  echo "ERROR: $$PARENT does not exist. Is the drive mounted?"; exit 1; fi; \
	if [ ! -w "$$PARENT" ]; then \
	  echo "ERROR: $$PARENT is not writable."; exit 1; fi; \
	echo "BIOINFO_HOME parent OK: $$PARENT"

release: ## Cut a release (images + launcher): make release VERSION=0.2.0 (also -alpha, -beta)
	@test -n "$(VERSION)" || (echo "usage: make release VERSION=0.2.0"; exit 2)
	./ops/release.sh app $(VERSION)

release-launcher: ## Launcher-only release; must exceed VERSION and be a production version
	@test -n "$(VERSION)" || (echo "usage: make release-launcher VERSION=0.1.1"; exit 2)
	./ops/release.sh launcher $(VERSION)
