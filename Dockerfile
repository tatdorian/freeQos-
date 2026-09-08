FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependances d'abord : couche de cache stable
COPY pyproject.toml README.md ./
RUN pip install --upgrade pip && \
    pip install "fastapi>=0.115" "uvicorn[standard]>=0.30" "pydantic>=2.7" \
                "pydantic-settings>=2.3" "asyncpg>=0.29" "httpx>=0.27" \
                "librouteros>=3.2" "jinja2>=3.1" "pyyaml>=6.0" \
                "cryptography>=42.0"

COPY app ./app

# L'app est hors-bande et purement cliente : aucun privilege requis.
# /app/data recoit la cle de chiffrement generee au premier demarrage.
RUN mkdir -p /app/data && \
    useradd --create-home --uid 10001 qos && \
    chown -R qos:qos /app
USER qos

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
