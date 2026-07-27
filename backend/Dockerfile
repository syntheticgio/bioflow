FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Bioinformatics tools come from Debian rather than bioconda: the versions are
# current (fastp 0.24.0, FastQC 0.12.1) and it avoids carrying a conda
# installation for two binaries.
#
# fastqc needs a JRE, which is ~200MB of the image on its own. It earns that
# only because its HTML report is the artifact people expect to hand to a
# collaborator -- the before/after numbers the UI shows come from fastp's JSON
# in a single pass, so nothing on the default path depends on it.
#
# Versions are recorded at build time and surfaced through the API: a set of
# trimming parameters means nothing without the version of the tool that ran
# them, and that ends up in a methods section.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        fastp \
        fastqc \
        default-jre-headless \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first so the layer caches across source edits.
# Installed from pyproject.toml rather than a duplicated list: two copies of the
# dependency set drift, and the version that matters is the one in the image.
COPY pyproject.toml ./
RUN mkdir -p app && touch app/__init__.py \
    && pip install --no-cache-dir '.[dev]' \
    && pip uninstall -y biopipe-backend

COPY app ./app
COPY tests ./tests

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
