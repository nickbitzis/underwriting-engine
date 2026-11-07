PORT ?= 8782
CURRENT ?= rules/uw-rules-2026.11.0.yaml
CANDIDATE ?= rules/candidate/uw-rules-2027.01.0.yaml

.PHONY: install test lint format run diff-rules fair-lending samples screenshot docker-build docker-run clean

install:
	uv sync --python 3.12

test:
	uv run --python 3.12 --group dev pytest -q

lint:
	uvx ruff check .
	uvx ruff format --check .

format:
	uvx ruff format .
	uvx ruff check . --fix

run:
	PORT=$(PORT) uv run uwengine serve

diff-rules:  ## impact of the candidate rules on the seeded portfolio plus the sample files
	uv run uwengine diff-rules --current $(CURRENT) --candidate $(CANDIDATE)

fair-lending:
	uv run uwengine fair-lending

samples:  ## regenerate data/samples from the seeded portfolio
	uv run uwengine make-samples

screenshot:  ## needs the server running on $(PORT) and Google Chrome on macOS
	"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --disable-gpu --hide-scrollbars \
		--force-device-scale-factor=1 --window-size=1400,1180 --virtual-time-budget=6000 \
		--screenshot=docs/screenshot.png "http://127.0.0.1:$(PORT)/?sample=KRL-00025"

docker-build:
	docker build -t underwriting-engine .

docker-run:
	docker run --rm -p $(PORT):8782 underwriting-engine

clean:
	rm -rf .pytest_cache .ruff_cache var
