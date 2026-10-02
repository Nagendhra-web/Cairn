# Minimal image for the Cairn API and workers.
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install ".[server,anthropic]" \
 && useradd --create-home --uid 10001 cairn \
 && mkdir -p /data /workspace && chown cairn:cairn /data /workspace

USER cairn
ENV CAIRN_DATA_DIR=/data
WORKDIR /workspace
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8787/health')"
CMD ["cairn", "serve", "--host", "0.0.0.0", "--port", "8787"]
