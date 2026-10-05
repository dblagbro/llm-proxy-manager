.PHONY: install dev test lint build up down logs shell secret-scan install-hooks verify-ci

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

# v5.22.42 — alembic is CONFIGURED BUT HAS NO REVISIONS.
#
# `alembic/` contains only env.py and script.py.mako; there is no versions/
# directory and never has been. The schema is created by
# `Base.metadata.create_all` in `init_db()`, plus hand-written `ALTER TABLE`
# statements there for columns added to existing tables.
#
# So `alembic upgrade head` has nothing to apply, and
# `alembic revision --autogenerate` would produce the project's FIRST revision
# by diffing target_metadata against a live database — a single revision
# covering whatever happens to differ, while the other 40 tables stay
# unmanaged. That is worse than no migration history, and until v5.22.33 it
# would additionally have emitted `drop_table('model_pricing_catalog')`,
# because that table was missing from the metadata alembic reads (BUG-089).
#
# These targets now refuse and explain, rather than doing something
# surprising to a database. Adopting alembic properly means a baseline
# revision that stamps the existing schema on every node, which is real work
# and the operator's call — not a side effect of running `make`.
migrate migrate-new:
	@echo "REFUSED: alembic has no revisions in this project."
	@echo
	@echo "  The schema is managed by Base.metadata.create_all in init_db(),"
	@echo "  plus hand-written ALTER TABLE statements there."
	@echo
	@echo "  'alembic upgrade head' has nothing to apply."
	@echo "  'alembic revision --autogenerate' would create the project's first"
	@echo "  revision from a live-DB diff, covering some tables and not others."
	@echo
	@echo "  To adopt alembic properly you need a baseline revision stamped on"
	@echo "  every node. See docs/current-state.md."
	@exit 1

# --- v5.22.15 secret containment -------------------------------------------
# The repo is public. A committed credential cannot be un-published, so both
# of these exist to stop one being written in the first place.

secret-scan:  ## Scan every tracked file for credential shapes
	python3 tools/secret_scan.py

install-hooks:  ## Point git at .githooks/ (run once per clone)
	git config core.hooksPath .githooks
	@echo "hooks installed: $$(git config core.hooksPath)"
	@echo "pre-commit now blocks credentials and forbidden paths."

verify-ci:
	# Run the unit suite on CI's interpreter (Python 3.13, project image).
	# Catches the local/CI divergences that made v5.22.38 go red twice:
	# a newer resolved dependency, or 3.13-specific behaviour. Needs Docker.
	./tools/verify_like_ci.sh
