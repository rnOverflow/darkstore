# random local credentials, generated once and never committed (see .env.example)
env:
	@./scripts/gen_env.sh

.PHONY: env up down sync test test-int health demo load urls clean

up: env
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

test-int: sync
	uv run --no-sync pytest -q -m integration

demo: up
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
	@echo "Customer UI / Ops UI: added in P4"
