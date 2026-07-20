FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/
COPY docs/ docs/
COPY scripts/migrations/ scripts/migrations/

RUN pip install --no-cache-dir -e ".[all]" pgvector psycopg2-binary python-multipart && \
    apt-get remove -y build-essential && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

ENV PYTHONPATH=/app/src

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

CMD ["uvicorn", "paper_search.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
