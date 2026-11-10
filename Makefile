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

GATEWAY_IMAGE ?= llm-proxy2:latest
GATEWAY_DOCKERFILE ?= Dockerfile
GATEWAY_BASE_IMAGE ?= llm-proxy2:latest
build:
	sudo docker build --build-arg BASE_IMAGE=$(GATEWAY_BASE_IMAGE) -f $(GATEWAY_DOCKERFILE) -t $(GATEWAY_IMAGE) .

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

# Independent voice-sidecar release; does not rebuild or recreate the gateway.
WHISPER_IMAGE ?= tmr-security/whisper-bridge:4.3.1-20261109
.PHONY: build-whisper test-whisper
build-whisper:
	docker build -t $(WHISPER_IMAGE) whisper_bridge/

test-whisper:
	docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges \
	  --cpus 2 --memory 3g --tmpfs /tmp:rw,size=256m \
	  -e PYTHONPATH=/app -e PYTHONDONTWRITEBYTECODE=1 \
	  -v $(CURDIR)/whisper_bridge/test_runtime.py:/checks/test_runtime.py:ro \
	  --entrypoint python $(WHISPER_IMAGE) /checks/test_runtime.py
	docker run --rm --network none -v $(CURDIR)/whisper_bridge:/checks:ro \
	  --entrypoint python $(WHISPER_IMAGE) /checks/verify_assets.py

.PHONY: lint-whisper
lint-whisper:
	ruff check whisper_bridge/test_runtime.py whisper_bridge/verify_assets.py
	ruff format --check whisper_bridge/test_runtime.py whisper_bridge/verify_assets.py

# Grok sidecar: these targets do not build or deploy the gateway.
GROK_IMAGE ?= llm-proxy2-grok-bridge:latest
GROK_PYTHON ?= python
.PHONY: build-grok test-grok lint-grok
build-grok:
	sudo docker build -t $(GROK_IMAGE) grok_bridge
test-grok:
	$(GROK_PYTHON) -m pytest grok_bridge/test_*.py -q
lint-grok:
	ruff check grok_bridge/bridge_security.py grok_bridge/test_*.py
	ruff format --check grok_bridge/bridge_security.py grok_bridge/test_*.py

# Isolated synthetic HTTPS/noVNC acceptance, using the reviewed exact images.
# Requires an empty, explicitly chosen local test directory and the storage guard.
GROK_TEST_DIR ?=
GROK_CAPACITY_GUARD ?=
.PHONY: lint-grok-browser test-grok-browser
lint-grok-browser:
	ruff check grok_bridge/testing/browser/*.py
	ruff format --check grok_bridge/testing/browser/*.py

test-grok-browser:
	test -n "$(GROK_TEST_DIR)" && test -n "$(GROK_CAPACITY_GUARD)"
	test ! -e "$(GROK_TEST_DIR)"
	sudo test -f "$(GROK_CAPACITY_GUARD)"
	mkdir -m 700 "$(GROK_TEST_DIR)"
	cp grok_bridge/testing/browser/*.py grok_bridge/testing/browser/nginx.conf "$(GROK_TEST_DIR)/"
	cp grok_bridge/deployment/nginx.grok-bridge.conf "$(GROK_TEST_DIR)/grok-bridge.conf"
	sudo python3 "$(GROK_CAPACITY_GUARD)" --peak-gib 3 -- python3 "$(GROK_TEST_DIR)/run_browser_rehearsal.py"
