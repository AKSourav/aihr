FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:0.5.5 /uv /uvx /bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project

COPY packages/ packages/
COPY services/agent/ services/agent/

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH=/app:/app/packages

CMD ["python", "-m", "services.agent.main", "start"]
