up:            ; docker compose up -d --build
down:          ; docker compose down
migrate:       ; docker compose run --rm api alembic upgrade head
seed:          ; docker compose run --rm api python -m scripts.seed
reset:         ; docker compose down -v && make up migrate seed
logs-agent:    ; docker compose logs -f agent-worker
console:       ; API_INTERNAL_URL=http://localhost:8000 CONSOLE_CALL_ID=$(ARGS) PYTHONPATH=.:./packages .venv/bin/python -m services.agent.main console     # runs on host, uses mic
test:          ; docker compose run --rm api pytest tests/unit
