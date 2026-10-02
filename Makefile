.PHONY: install lint typecheck test examples bench check demo serve docker

install:
	pip install -e ".[dev,anthropic]"

lint:
	python scripts/check_no_em_dash.py
	ruff check src tests examples benchmarks scripts

typecheck:
	MYPYPATH=src python -m mypy --explicit-package-bases src/cairn

test:
	pytest -q

examples:
	python scripts/run_examples.py

bench:
	python benchmarks/injection_suite.py
	python benchmarks/recovery.py
	python benchmarks/retrieval_quality.py
	python benchmarks/runtime_overhead.py

check: lint typecheck test

demo:
	cairn demo all

serve:
	cairn serve

docker:
	docker build -t cairn-runtime .
