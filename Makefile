PYTHON ?= python

.PHONY: test frontend-build check

test:
	$(PYTHON) -m unittest discover -s tests -p "test_*.py"

frontend-build:
	cd frontend && bun run build

check: test frontend-build
