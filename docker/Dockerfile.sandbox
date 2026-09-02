# EDGEVERDICT_SANDBOX_DOCKERFILE_V3_BUILDDEPS
# Python version is a build arg so repos that pin requires-python (e.g.
# PostHog's ==3.13.13) get a matching sandbox without editing this file:
#   docker build -f docker/Dockerfile.sandbox \
#     --build-arg PYTHON_VERSION=3.13.13 \
#     -t edgeverdict-sandbox:py3.13 .
# Then run with EDGEVERDICT_SANDBOX_IMAGE=edgeverdict-sandbox:py3.13.
# The default build stays byte-for-byte the image already verified 9/9.
ARG PYTHON_VERSION=3.12.11
FROM python:${PYTHON_VERSION}-slim-bookworm

ARG NODE_MAJOR=22
# Compile-from-source deps for python repos whose lock pins packages with no
# wheel for this platform (zstd, polyleven, pywatchman on arm64) or that must
# link against system libraries (xmlsec against libxml2, psycopg2 against
# libpq). The list is PostHog's own build stage, taken verbatim from their
# Dockerfile; it is the common denominator for Django monorepos, and a
# generic image without it fails installs one package at a time.
ARG BUILD_DEPS="build-essential libpq-dev libxmlsec1 libxmlsec1-dev libffi-dev zlib1g-dev pkg-config"
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git gnupg tini ${BUILD_DEPS} \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
       | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
       > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && python -m pip install --no-cache-dir pytest \
    && corepack enable \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_INPUT=1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "--version"]
