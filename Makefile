up:            ; docker compose up -d --build
down:          ; docker compose down
migrate:       ; docker compose run --rm api alembic upgrade head
seed:          ; docker compose run --rm api python -m scripts.seed
reset:         ; docker compose down -v && make up migrate seed
logs-agent:    ; docker compose logs -f agent-worker
console:       ; cd services/agent && python main.py console     # runs on host, uses mic
test:          ; docker compose run --rm api pytest tests/unit
