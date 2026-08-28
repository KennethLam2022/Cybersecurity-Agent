FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ENV=production \
    APP_HOST=0.0.0.0 \
    APP_PORT=8000 \
    UVICORN_WORKERS=1

WORKDIR /app

# Basic PDF/OCR runtime libraries. .doc conversion through Microsoft Word COM
# remains Windows-only; Linux deployments should upload .docx or convert .doc
# before ingestion.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgomp1 \
        libglib2.0-0 \
        libgl1 \
        libsm6 \
        libxext6 \
        libxrender1 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-docker.txt ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements-docker.txt

COPY packages ./packages
COPY scripts ./scripts
COPY pytest.ini package.json pnpm-workspace.yaml ./

RUN mkdir -p /app/agent_data /app/RAG_DATA /app/upload_staging /app/logs \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin securenexus \
    && chown -R securenexus:securenexus /app

USER securenexus
WORKDIR /app/packages/agent/src

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/stats/health', timeout=3)"

CMD ["python", "main.py"]
