# random local credentials, generated once and never committed (see .env.example)
env:
	@./scripts/gen_env.sh

.PHONY: env up down sync train retrain web e2e test test-int test-alerts dashboards health demo load urls clean

# build the React apps once (nginx serves web/dist)
web:
	@test -f web/dist/index.html || { cd web && npm ci && npm run build; }

up: env web
	docker compose up -d --build --wait --wait-timeout 300
	@./scripts/healthcheck.sh
	@$(MAKE) --no-print-directory urls

down: env
	docker compose down

clean: env
	docker compose down -v

health: env
	@./scripts/healthcheck.sh

# macOS marks uv's editable .pth files 'hidden', and Python then skips them -> ImportError
sync:
	uv sync
	@chflags -R nohidden .venv 2>/dev/null || true

test: sync
	uv run --no-sync pytest -q -m 'not integration'

test-alerts:  ## unit-test the SLO alert rules with promtool (needs Docker, not the stack)
	docker run --rm --entrypoint promtool -v "$$PWD/infra/prometheus:/p:ro" prom/prometheus:v2.54.1 check rules /p/rules/slo.yml
	docker run --rm --entrypoint promtool -v "$$PWD/infra/prometheus:/p:ro" prom/prometheus:v2.54.1 test rules /p/tests/slo_test.yml

dashboards:  ## regenerate the Grafana dashboards from scripts/gen_dashboards.py
	python3 scripts/gen_dashboards.py

test-int: sync
	uv run --no-sync pytest -q -m integration

# generate simulated trips, train the ETA model, log to MLflow, write docs/ml/eta-report.md
train: sync
	uv run --no-sync python -m darkstore_eta.train --mlflow-uri http://localhost:5001

# retrain on fresh trips; promotes to models/eta/current only if it beats the live model
retrain: sync
	uv run --no-sync python -m darkstore_eta.retrain --mlflow-uri http://localhost:5001

# Playwright against the running stack (make up first): customer journey + ops dashboard + screenshots
e2e:
	@cd web && OPS_TOKEN=$$(cd .. && uv run --no-sync python scripts/ops_token.py | sed 's/.*token=//') npx playwright test

demo: up
	@test -f models/eta/current/meta.json || $(MAKE) --no-print-directory train
	docker compose --profile demo up -d simulator
	@sleep 20
	@./scripts/demo_stats.sh
	@$(MAKE) --no-print-directory urls

load:
	@echo "load: Locust arrives in P6 (see PLAN.md)"

urls:
	@echo ""
	@echo "Redpanda Console  http://localhost:8080"
	@echo "Grafana           http://localhost:3000  (admin / GRAFANA_ADMIN_PASSWORD from .env)"
	@echo "MLflow            http://localhost:5001"
	@echo "Prometheus        http://localhost:9090"
	@echo "Customer UI       http://localhost:8088"
	@echo "Ops UI            $$(uv run --no-sync python scripts/ops_token.py 2>/dev/null || echo http://localhost:8088/ops/)"
