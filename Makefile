PYTHON ?= python3

.PHONY: install test validate demo clean

install:
	$(PYTHON) -m pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest

validate:
	$(PYTHON) -m netops validate -c configs/lab.yaml

demo:
	$(PYTHON) scripts/demo.py

clean:
	rm -rf artifacts .pytest_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
