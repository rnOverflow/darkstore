.PHONY: up down test test-int health demo load urls clean

up:
	docker compose up -d --wait --wait-timeout 240
	@./scripts/healthcheck.sh
	@$(MAKE) --no-print-directory urls

down:
	docker compose down

clean:
	docker compose down -v

health:
	@./scripts/healthcheck.sh

test:
	uv run pytest -q

test-int:
	uv run pytest -q -m integration

demo: up
	@echo "demo: simulator arrives in P2 (see PLAN.md)"; $(MAKE) --no-print-directory urls

load:
	@echo "load: Locust arrives in P6 (see PLAN.md)"

urls:
	@echo ""
	@echo "Redpanda Console  http://localhost:8080"
	@echo "Grafana           http://localhost:3000  (admin / \$$GRAFANA_ADMIN_PASSWORD, default darkstore-local)"
	@echo "MLflow            http://localhost:5001"
	@echo "Prometheus        http://localhost:9090"
	@echo "Customer UI / Ops UI: added in P4"
