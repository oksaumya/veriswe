# VeriSWE - standard evaluation interface
#
#   export AI_API_KEY="<key>"
#   make setup
#   make run                                  # interactive TUI (paste issue URL/text)
#   make run ISSUE=<github-issue-url>         # start solving immediately
#   make run ISSUE="<text>" REPO=/path/to/repo HEADLESS=1
#   make test
#
# The API key is only ever read from the AI_API_KEY environment variable.

SHELL  := /bin/bash
PYTHON ?= python3
VENV   := .venv
BIN    := $(VENV)/bin

.DEFAULT_GOAL := help
.PHONY: help setup run test test-all demo clean

help:
	@echo "VeriSWE targets:"
	@echo "  make setup   - create .venv and install all dependencies"
	@echo "  make run     - launch the harness (TUI; or ISSUE=... REPO=... HEADLESS=1)"
	@echo "  make test    - offline test suite (no API key needed)"
	@echo "  make demo    - solve the bundled demo bug with the live model"
	@echo "  make clean   - remove generated artefacts"

setup:
	@echo "==> Setting up VeriSWE"
	@$(PYTHON) -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "Python >= 3.10 is required (set PYTHON=/path/to/python3.x)")'
	@command -v git >/dev/null || { echo "git is required"; exit 1; }
	@if command -v uv >/dev/null 2>&1; then \
		uv venv -q --allow-existing --python $(PYTHON) $(VENV) && \
		uv pip install -q --python $(BIN)/python -e . pytest ; \
	else \
		$(PYTHON) -m venv $(VENV) && \
		$(BIN)/python -m pip install -q --disable-pip-version-check --upgrade pip && \
		$(BIN)/python -m pip install -q --disable-pip-version-check -e . pytest ; \
	fi
	@$(BIN)/python -c "import veriswe.cli, litellm; print('==> setup OK')"
	@[ -n "$$AI_API_KEY" ] || echo "NOTE: AI_API_KEY is not set yet. Run: export AI_API_KEY=\"<key>\""

$(BIN)/veriswe:
	@$(MAKE) --no-print-directory setup

run: $(BIN)/veriswe
	@[ -n "$$AI_API_KEY" ] || { echo "ERROR: AI_API_KEY is not set. Run: export AI_API_KEY=\"<key>\""; exit 2; }
	@$(BIN)/veriswe \
		$(if $(ISSUE),--issue "$(ISSUE)") \
		$(if $(REPO),--repo "$(REPO)") \
		$(if $(MODEL),--model "$(MODEL)") \
		$(if $(STEP_LIMIT),--step-limit $(STEP_LIMIT)) \
		$(if $(filter 1 true yes,$(HEADLESS)),--headless)

test: $(BIN)/veriswe
	@$(BIN)/python -m pytest tests/veriswe -q -p no:cacheprovider -W ignore::pytest.PytestConfigWarning

test-all: $(BIN)/veriswe
	@$(BIN)/python -m pytest tests -q -p no:cacheprovider -x --ignore=tests/test_data

demo: $(BIN)/veriswe
	@rm -rf workspace/demo_calc && mkdir -p workspace && cp -R tests/veriswe/fixtures/calc workspace/demo_calc
	@$(BIN)/veriswe --headless --repo workspace/demo_calc --issue @tests/veriswe/fixtures/calc_issue.md

clean:
	rm -rf $(VENV) runs workspace build dist *.egg-info src/*.egg-info .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
