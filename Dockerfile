# syntax=docker/dockerfile:1.7

FROM node:24.6.0-alpine3.22 AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.13.7-slim-bookworm AS wheel
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
RUN python -m pip wheel --wheel-dir /wheels .

FROM python:3.13.7-slim-bookworm AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
RUN apt-get update \
    && apt-get install --no-install-recommends --yes libusb-1.0-0 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=wheel /wheels /wheels
RUN python -m pip install /wheels/*.whl \
    && rm -rf /wheels
COPY --from=frontend /build/frontend/dist /opt/radiacode/static
USER 65532:65532
EXPOSE 8080 9090
ENTRYPOINT ["radiacode"]
CMD ["web"]
