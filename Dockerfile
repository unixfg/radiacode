# syntax=docker/dockerfile:1.7

FROM node:24.6.0-alpine3.22 AS frontend
ARG SOURCE_REVISION=main
ARG SOURCE_URL=https://github.com/unixfg/radiacode
ENV VITE_SOURCE_REVISION=${SOURCE_REVISION} \
    VITE_SOURCE_URL=${SOURCE_URL}
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build
COPY --chmod=0644 THIRD_PARTY_NOTICES.md /build/frontend/dist/assets/THIRD_PARTY_NOTICES.md
COPY --chmod=0644 LICENSES/*.txt /build/frontend/dist/assets/LICENSES/

FROM python:3.13.7-slim-bookworm AS wheel
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /build
COPY --chmod=0644 pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY --chmod=0644 LICENSES/*.txt ./LICENSES/
COPY src/ ./src/
RUN python -m pip wheel --wheel-dir /wheels .

FROM python:3.13.7-slim-bookworm AS runtime
ARG SOURCE_REVISION=main
ARG SOURCE_URL=https://github.com/unixfg/radiacode
LABEL org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.url="${SOURCE_URL}/tree/${SOURCE_REVISION}"
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
COPY --chmod=0644 LICENSE SOURCE.md THIRD_PARTY_NOTICES.md /usr/share/licenses/radiacode/
COPY --chmod=0644 LICENSES/*.txt /usr/share/licenses/radiacode/LICENSES/
USER 65532:65532
EXPOSE 8080 9090
ENTRYPOINT ["radiacode"]
CMD ["web"]
