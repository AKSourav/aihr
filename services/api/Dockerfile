FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:0.5.5 /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --group dev

COPY packages/ packages/
COPY services/api/ services/api/
COPY migrations/ migrations/
COPY alembic.ini .
COPY scripts/ scripts/
COPY tests/ tests/

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH=/app:/app/packages

CMD ["uvicorn", "services.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
