PYTHON ?= local/.venv/bin/python
export PYTHONDONTWRITEBYTECODE := 1

.DEFAULT_GOAL := help
.PHONY: help setup test lint e2e-init e2e-up e2e-bootstrap e2e-ui e2e-test e2e-basic e2e-exit e2e-derp-service e2e-status e2e-down

help:
	@printf '%s\n' 'setup          Install tooling into local/.venv' 'test           Run offline regression tests' 'lint           Check Python and shell syntax (requires shellcheck)' 'e2e-init       Create local E2E inputs if absent' 'e2e-bootstrap  Register simulated devices for the first time' 'e2e-up         Start E2E using existing identities' 'e2e-ui         Start the optional WebUI' 'e2e-basic      Check running E2E without service fault injection' 'e2e-exit       Check dual-stack exit HTTP, permissions and recovery' 'e2e-test       Run full E2E, including temporary network faults' 'e2e-derp-service Check DERP systemd in an isolated container' 'e2e-status     Show E2E services' 'e2e-down       Stop E2E, preserving volumes and credentials'

setup:
	python3 -m venv local/.venv
	PIP_CACHE_DIR=local/.cache/pip $(PYTHON) -m pip install -r tests/e2e/requirements.txt

test:
	TB_PYTHON=$(PYTHON) bash test.sh
	TB_PYTHON=$(PYTHON) bash tests/e2e/test.sh

lint:
	$(PYTHON) -c 'import ast,pathlib; [ast.parse(p.read_text(), filename=str(p)) for p in pathlib.Path("tests").rglob("*.py")]'
	shellcheck install.sh test.sh tests/e2e/test.sh tests/e2e/start.sh tests/e2e/node.sh tests/e2e/httpbin-network.sh

e2e-init:
	@mkdir -p local/e2e
	@test -f local/e2e/environment.yaml || (umask 077; cp tests/e2e/environment.example.yaml local/e2e/environment.yaml)

e2e-up: e2e-init
	$(PYTHON) tests/e2e/e2e.py up

e2e-bootstrap: e2e-init
	$(PYTHON) tests/e2e/e2e.py --bootstrap up

e2e-ui:
	$(PYTHON) tests/e2e/e2e.py ui

e2e-test:
	$(PYTHON) tests/e2e/verify.py

e2e-basic:
	$(PYTHON) tests/e2e/verify.py --basic

e2e-exit:
	$(PYTHON) tests/e2e/verify.py --exit-only

e2e-derp-service:
	$(PYTHON) tests/e2e/derp_service.py

e2e-status:
	$(PYTHON) tests/e2e/e2e.py status

e2e-down:
	$(PYTHON) tests/e2e/e2e.py down

