.PHONY: install codegen lint test check db-apply offline deploy

VENV = .venv/bin

install:
	python3 -m venv .venv
	$(VENV)/pip install -U pip
	$(VENV)/pip install -r layers/common/requirements.txt -r requirements-dev.txt

codegen:                 ## regenerate models from contracts/openapi.yaml
	$(VENV)/bash scripts/codegen.sh || bash scripts/codegen.sh

lint:
	$(VENV)/ruff check .

test:
	$(VENV)/pytest

check: lint test        ## what CI runs

db-apply:                ## apply migrations to Supabase (direct or IPv4 pooler)
	$(VENV)/python scripts/apply_migrations.py

offline:                 ## run the API locally (needs `npm i` for serverless plugins)
	npx serverless offline --stage local

deploy:                  ## deploy all Lambdas (needs AWS creds)
	npx serverless deploy --stage $(or $(STAGE),dev)
