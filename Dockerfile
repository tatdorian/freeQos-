# Image de base surchargeable (PYTHON_IMAGE dans .env) : si Docker Hub est
# injoignable depuis le serveur (timeout sur registry-1.docker.io), pointer vers
# un miroir, ex. mirror.gcr.io/library/python:3.11-slim.
ARG PYTHON_IMAGE=python:3.11-slim
FROM ${PYTHON_IMAGE} AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependances d'abord : couche de cache stable, versions EPINGLEES par le
# fichier de verrou (genere depuis pyproject.toml). Plus de liste recopiee a la
# main qui derive de pyproject.
COPY requirements.lock ./
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.lock

# Puis l'application elle-meme, installee DEPUIS pyproject (--no-deps : les
# dependances viennent du verrou ci-dessus, pas d'une resolution non reproductible).
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir --no-deps .

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
