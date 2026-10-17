# SmartHealth — one command per thing you actually need to do.
#
# Everything runs inside Docker. Nothing here assumes Python, Postgres or
# any dependency is installed on the host: a mentor with only Docker
# installed can run every target in this file.
#
# .PHONY tells make these are command names, not files to build. Without
# it, a target would silently do nothing if a file or directory of the
# same name existed (a `test/` directory would break `make test`).
.PHONY: help up down logs migrate seed eval test test-cov lint typecheck fmt shell psql reset

# The default target when you type plain `make`. Prints every target with
# its `##` comment, so this list can never drift from the targets below.
help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

# The whole stack, not just Postgres, Redis and the API: since Week 4 the
# seed publishes every service through Temporal, so `make seed` after an
# `up` that left Temporal and its worker stopped crashed on connect.
up: ## Start the whole stack (detached), then migrate
	docker compose up -d
	$(MAKE) migrate

down: ## Stop everything, keeping database volumes
	docker compose down

logs: ## Tail the API logs
	docker compose logs -f api

migrate: ## Apply Alembic migrations up to head
	docker compose exec api alembic upgrade head

seed: ## Populate the synthetic demo dataset (safe to re-run)
	docker compose exec api python -m scripts.seed

eval: ## Report search hit rates and the threshold sweep (needs a seeded stack)
	docker compose exec api python -m scripts.eval_retrieval

test: ## Run the test suite from cold — no running API needed
	docker compose run --rm test

test-cov: ## Run the suite with a coverage report; fails under 80%
	docker compose run --rm test pytest --cov=app --cov-report=term-missing --cov-fail-under=80

lint: ## Check formatting and lint rules without changing anything
	docker compose run --rm test ruff check app tests scripts
	docker compose run --rm test black --check app tests scripts

typecheck: ## Type-check app/models with mypy (see pyproject.toml for scope)
	docker compose run --rm test mypy app/models

fmt: ## Reformat the code in place
	docker compose run --rm test black app tests scripts
	docker compose run --rm test ruff check --fix app tests scripts

shell: ## Open a shell inside the API container
	docker compose exec api bash

psql: ## Open a psql session against the dev database
	docker compose exec postgres psql -U app -d app

reset: ## Destroy all data, rebuild the schema, reseed. Irreversible.
	docker compose down -v
	$(MAKE) up
	$(MAKE) seed
