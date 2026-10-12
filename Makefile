# Bedrock API Key Self-Service Portal (PoC)
CONFIG ?= portal.config.json
STACK ?= BedrockApiKeyPortal

.PHONY: install test lint requirements build synth diff deploy destroy cleanup-dry-run

install:
	cd backend && uv sync
	cd infra && npm ci
	cd frontend && npm ci

lint:
	cd backend && uv run ruff check src tests ../scripts && uv run ruff format --check src tests ../scripts
	cd infra && npx tsc --noEmit
	cd frontend && npx tsc --noEmit

test:
	cd backend && uv run pytest
	cd infra && npx vitest run
	cd frontend && npx vitest run

# Regenerate the pinned Lambda dependencies after changing backend/pyproject.toml.
requirements:
	cd backend && uv lock && uv export --no-dev --no-hashes --frozen --no-emit-project -o requirements.txt

build:
	cd frontend && npm run build

synth: build
	cd infra && npx cdk synth -c config=$(CONFIG)

diff: build
	cd infra && npx cdk diff --all -c config=$(CONFIG)

deploy: build
	cd infra && npx cdk deploy $(STACK) -c config=$(CONFIG)

destroy:
	cd infra && npx cdk destroy $(STACK) -c config=$(CONFIG)

cleanup-dry-run:
	cd backend && uv run python ../scripts/cleanup.py
