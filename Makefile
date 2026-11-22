PYTHON ?= python
DB ?= traces.db
OUT ?= reports/offline

.PHONY: test lint check-golden eval-offline report

test:
	$(PYTHON) -m pytest

lint:
	$(PYTHON) -m ruff check .

check-golden:
	PYTHONPATH=src $(PYTHON) -m agentreplay.cli check --golden tests/golden --agent examples.minimal_agent:run_agent

eval-offline:
	PYTHONPATH=src $(PYTHON) -m agentreplay.cli eval run --db $(DB) --config examples/eval-offline.yaml

report:
	PYTHONPATH=src $(PYTHON) -m agentreplay.cli report --db $(DB) --out $(OUT)

.PHONY: eval-live live-plan
live-plan:
	PYTHON=$(PYTHON) scripts/run_live.sh plan

eval-live:
	PYTHON=$(PYTHON) scripts/run_live.sh run --db $(DB)
