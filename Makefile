# random local credentials, generated once and never committed (see .env.example)
env:
	@./scripts/gen_env.sh

.PHONY: env dirs up down sync train retrain web e2e test test-int test-clean test-alerts dashboards chaos health demo load urls clean

# Running the stack needs only Docker + make: the React apps are built inside the `web` image.
# `make web` is for front-end development on the host (needs Node): it fills web/dist for `npm run preview`.
web:
	@test -f web/dist/index.html || { cd web && npm ci && npm run build; }

# bind-mount sources must exist, or Docker creates them root-owned
dirs:
	@mkdir -p models/eta data/eta docs/ml

up: env dirs
	docker compose up -d --build --wait --wait-timeout 600
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

test-clean:  ## list testcontainers left behind by an interrupted integration run (they hold ~250 MB each)
	@docker ps -a --filter label=org.testcontainers=true --format '{{.Names}}  {{.Image}}  {{.Status}}'; echo "remove with: docker rm -f <names above>"

test-int: sync
	uv run --no-sync pytest -q -m integration

# ETA training runs in the `trainer` container (no host Python needed); files are written as the host user.
# It starts MLflow if needed, and works with or without the rest of the stack running.
TRAINER = docker compose --profile tools run --rm --user $$(id -u):$$(id -g) trainer python -m

# generate simulated trips, train the ETA model, log to MLflow, write docs/ml/eta-report.md
train: env dirs
	$(TRAINER) darkstore_eta.train

# retrain on fresh trips; promotes to models/eta/current only if it beats the live model
retrain: env dirs
	$(TRAINER) darkstore_eta.retrain

# Playwright against the running stack (make up first): customer journey + ops dashboard + screenshots
e2e:
	@cd web && OPS_TOKEN=$$(cd .. && uv run --no-sync python scripts/ops_token.py | sed 's/.*token=//') npx playwright test

demo: up
	@test -f models/eta/current/meta.json || $(MAKE) --no-print-directory train
	docker compose --profile demo up -d simulator
	@sleep 20
	@./scripts/demo_stats.sh
	@$(MAKE) --no-print-directory urls

load: sync  ## ~9 min step ramp through nginx; needs the stack up and quiet (no simulator). Results: docs/load/
	uv run --no-sync locust -f tests/load/locustfile.py --headless --host http://localhost:8088 --only-summary
	uv run --no-sync python -m tests.load.report

chaos:  ## make chaos SCENARIO=kill-picking|kill-dispatch|kill-order|kill-gateway|broker-restart|broker-crash
	uv run --no-sync python -m tests.chaos.run $(SCENARIO)

urls:
	@echo ""
	@echo "Redpanda Console  http://localhost:8080"
	@echo "Grafana           http://localhost:3000  (admin / GRAFANA_ADMIN_PASSWORD from .env)"
	@echo "MLflow            http://localhost:5001"
	@echo "Prometheus        http://localhost:9090"
	@echo "Customer UI       http://localhost:8088"
	@echo "Ops UI            $$(docker compose exec -T order python - < scripts/ops_token.py 2>/dev/null || echo http://localhost:8088/ops/)"
