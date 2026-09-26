# VeriSWE - standard evaluation interface
#
#   export AI_API_KEY="<key>"
#   make setup
#   make run                                  # interactive TUI (paste issue URL/text)
#   make run ISSUE=<github-issue-url>         # start solving immediately
#   make run ISSUE="<text>" REPO=/path/to/repo HEADLESS=1
#   make run TASK="Add a --json flag to the CLI" REPO=/path/to/repo   # any software-engineering task
#   make test
#
# The API key is only ever read from the AI_API_KEY environment variable
# (`export AI_API_KEY=...`, or `make run AI_API_KEY=...`; a local git-ignored .env also works).

SHELL  := /bin/bash
PYTHON_OVERRIDE ?= $(PYTHON)
VENV   := .venv
BIN    := $(VENV)/bin

.DEFAULT_GOAL := help
.PHONY: help setup run test test-all demo demo-recovery clean

help:
	@echo "VeriSWE targets:"
	@echo "  make setup   - create .venv and install all dependencies"
	@echo "  make run     - launch the harness (TUI; or ISSUE=... REPO=... HEADLESS=1)"
	@echo "  make test    - offline test suite (no API key needed)"
	@echo "  make test-all - our suite + the full upstream mini-swe-agent suite"
	@echo "  make demo    - solve the bundled demo bug with the live model"
	@echo "  make demo-recovery - offline replay: failure -> recovery -> verified (no API key)"
	@echo "  make clean   - remove generated artefacts"

setup:
	@echo "==> Setting up VeriSWE"
	@PYTHON="$(PYTHON_OVERRIDE)" VENV="$(VENV)" bash scripts/setup.sh
	@[ -n "$$AI_API_KEY" ] || [ -f .env ] || echo "NOTE: AI_API_KEY is not set yet. Run: export AI_API_KEY=\"<key>\""

$(BIN)/veriswe:
	@$(MAKE) --no-print-directory setup

run: $(BIN)/veriswe
	@[ -n "$$AI_API_KEY" ] || grep -qE '^AI_API_KEY=.+' .env 2>/dev/null || { echo "ERROR: AI_API_KEY is not set. Run: export AI_API_KEY=\"<key>\""; exit 2; }
	@$(BIN)/veriswe \
		$(if $(ISSUE),--issue "$(ISSUE)") \
		$(if $(TASK),--task "$(TASK)") \
		$(if $(REPO),--repo "$(REPO)") \
		$(if $(MODEL),--model "$(MODEL)") \
		$(if $(STEP_LIMIT),--step-limit $(STEP_LIMIT)) \
		$(if $(filter 1 true yes,$(HEADLESS)),--headless)

test: $(BIN)/veriswe
	@$(BIN)/python -m pytest tests/veriswe -q -p no:cacheprovider -W ignore::pytest.PytestConfigWarning

test-all: $(BIN)/veriswe  ## our suite + the full upstream mini-swe-agent suite
	@$(BIN)/python -m pip install -q --disable-pip-version-check pytest-asyncio portkey-ai datasets 2>/dev/null || \
		uv pip install -q --python $(BIN)/python pytest-asyncio portkey-ai datasets
	@PATH="$(CURDIR)/$(BIN):$$PATH" $(BIN)/python -m pytest tests -q -p no:cacheprovider -W ignore::pytest.PytestConfigWarning \
		--ignore=tests/test_data --ignore=tests/environments/extra

demo: $(BIN)/veriswe
	@rm -rf workspace/demo_calc && mkdir -p workspace && cp -R tests/veriswe/fixtures/calc workspace/demo_calc
	@$(BIN)/veriswe --headless --repo workspace/demo_calc --issue @tests/veriswe/fixtures/calc_issue.md

demo-recovery: $(BIN)/veriswe  ## offline: failure -> recovery -> verification (scripted decisions, real harness)
	@$(BIN)/veriswe --demo-replay $(if $(filter 1 true yes,$(HEADLESS)),--headless)

clean:
	rm -rf $(VENV) runs workspace build dist *.egg-info src/*.egg-info .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
