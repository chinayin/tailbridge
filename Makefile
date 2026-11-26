PYTHON ?= local/.venv/bin/python
export PYTHONDONTWRITEBYTECODE := 1

.DEFAULT_GOAL := help
.PHONY: help setup test lint check ensure-venv e2e-init e2e-up e2e-bootstrap e2e-ui e2e-test e2e-basic e2e-exit e2e-derp-service e2e-systemd e2e-status e2e-down

help: ## Show help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  make %-17s - %s\n", $$1, $$2}'
	@echo 'E2E settings: TB_CONFIG, TB_BASE_IMAGE, TB_INSTALL_PROXY; see docs/E2E_TEST_GUIDE.md'

setup: ## Install tooling into local/.venv
	@python3 -m venv local/.venv
	@PIP_CACHE_DIR=local/.cache/pip $(PYTHON) -m pip install -r tests/e2e/requirements.txt

ensure-venv:
	@command -v $(PYTHON) >/dev/null || { echo "Error: $(PYTHON) not found; run make setup" >&2; exit 2; }

test: ensure-venv ## Run offline regression tests
	@TB_PYTHON=$(PYTHON) bash test.sh
	@TB_PYTHON=$(PYTHON) bash tests/e2e/test.sh

lint: ensure-venv ## Check Python and shell syntax (requires shellcheck)
	@command -v shellcheck >/dev/null || { echo "Error: shellcheck is required" >&2; exit 2; }
	@$(PYTHON) -c 'import ast,pathlib; [ast.parse(p.read_text(), filename=str(p)) for d in ("tests", "skills") for p in pathlib.Path(d).rglob("*.py")]'
	@shellcheck install.sh test.sh tests/e2e/test.sh tests/e2e/start.sh tests/e2e/node.sh tests/e2e/httpbin-network.sh skills/tailbridge-deploy/assets/first-boot.sh

check: test lint ## Full local check (run before commit)

e2e-init: ## Create local E2E inputs if absent
	@mkdir -p local/e2e
	@test -f local/e2e/environment.yaml || (umask 077; cp tests/e2e/environment.example.yaml local/e2e/environment.yaml)

e2e-up: ensure-venv e2e-init ## Start E2E using existing identities
	@$(PYTHON) tests/e2e/e2e.py up

e2e-bootstrap: ensure-venv e2e-init ## Register simulated devices for the first time
	@$(PYTHON) tests/e2e/e2e.py --bootstrap up

e2e-ui: ensure-venv ## Start the optional WebUI
	@$(PYTHON) tests/e2e/e2e.py ui

e2e-test: ensure-venv ## Run full E2E, including temporary network faults
	@$(PYTHON) tests/e2e/verify.py

e2e-basic: ensure-venv ## Check running E2E without service fault injection
	@$(PYTHON) tests/e2e/verify.py --basic

e2e-exit: ensure-venv ## Check dual-stack exit HTTP, permissions and recovery
	@$(PYTHON) tests/e2e/verify.py --exit-only

e2e-derp-service: ensure-venv ## Check DERP systemd in an isolated container
	@$(PYTHON) tests/e2e/derp_service.py

e2e-systemd: e2e-derp-service ## Check all three roles with real systemd units
	@$(PYTHON) tests/e2e/control_node_service.py

e2e-status: ensure-venv ## Show E2E services
	@$(PYTHON) tests/e2e/e2e.py status

e2e-down: ensure-venv ## Stop E2E, preserving volumes and credentials
	@$(PYTHON) tests/e2e/e2e.py down
