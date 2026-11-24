# Developer entry points for the Agent Tokenomics Framework.
#
# This is a reference solution, so the lint/format/test setup is part of what we
# ship: `make lint` is the single green gate a partner can trust before a PR.
#
# Python tooling runs from the CDK virtualenv (infra/.venv, created by
# scripts/deploy-all.sh); the unit tests run from the app-plane venv
# (src/app-plane/.venv) which carries the Strands/AgentCore deps.

PY        := infra/.venv/bin/python
TEST_PY   := src/app-plane/.venv/bin/python
PY_PATHS  := src seed infra/stacks infra/app.py tests

.PHONY: lint lint-py lint-web format format-py format-web typecheck test synth lint-fix help

help:
	@echo "make lint       - lint everything (Python + both SPAs) -- the PR gate"
	@echo "make format     - auto-format everything (ruff + prettier)"
	@echo "make typecheck  - advisory mypy (not a gate; SDK types are noisy)"
	@echo "make test       - run the offline unit tests"
	@echo "make synth      - cdk synth all stacks"

# ---- lint (the gate) -------------------------------------------------------
lint: lint-py lint-web

lint-py:
	$(PY) -m ruff check $(PY_PATHS)
	$(PY) -m ruff format --check $(PY_PATHS)

lint-web:
	cd web/control-plane && npm run lint && npm run format:check
	cd web/app-plane && npm run lint && npm run format:check

lint-fix:
	$(PY) -m ruff check $(PY_PATHS) --fix
	cd web/control-plane && npx eslint . --fix
	cd web/app-plane && npx eslint . --fix

# ---- format ----------------------------------------------------------------
format: format-py format-web

format-py:
	$(PY) -m ruff format $(PY_PATHS)

format-web:
	cd web/control-plane && npm run format
	cd web/app-plane && npm run format

# ---- advisory type check (NOT part of `lint`; see pyproject.toml) ----------
typecheck:
	cd src/app-plane && MYPYPATH="$$PWD:$$PWD/../shared" ../../$(PY) -m mypy --explicit-package-bases . || true

# ---- tests + synth ---------------------------------------------------------
test:
	$(TEST_PY) -m pytest -q

synth:
	cd infra && ../$(PY) app.py >/dev/null && echo "synth OK (see cdk synth for templates)"
