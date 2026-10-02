FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.15 /uv /bin/uv

# The venv lives outside /app: in dev, /app is bind-mounted and would hide it.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    PATH="/opt/venv/bin:$PATH"

# GeoDjango needs the GDAL and GEOS shared libraries (gdal-bin pulls in both).
# The -dev packages are not needed: nothing is compiled against them.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gdal-bin \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --frozen: install exactly what uv.lock says, fail if it is out of date.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen

COPY . .

CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000"]
