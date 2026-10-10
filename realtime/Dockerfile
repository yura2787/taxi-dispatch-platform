FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.15 /uv /bin/uv

# The venv lives outside /app: in dev, /app is bind-mounted and would hide it.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# --frozen: install exactly what uv.lock says, fail if it is out of date.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen

COPY . .

# --ws-max-size: keep in sync with WS_MAX_MESSAGE_BYTES in app/config.py.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--ws-max-size", "4096"]
