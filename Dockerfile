# Stage 1: build del frontend (Fase 8, docs/SPEC.md §10-11) - Node resta
# solo in questo stage, mai nell'immagine finale (nessun runtime Node in
# produzione, solo i file statici prodotti da `vite build`).
# --platform=$BUILDPLATFORM: il frontend sono solo file statici, identici per
# ogni architettura. Si costruisce una volta sola, nativo sulla macchina di
# build, e lo stesso dist va in tutte le immagini: prima l'arm64 lo ricostruiva
# emulato (QEMU), ~3 minuti in più a ogni build.
FROM --platform=$BUILDPLATFORM node:22-slim AS frontend-build

WORKDIR /frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend .
RUN npm run build

# Stage 2: backend Python + frontend statico servito dallo stesso
# container (app/frontend.py) - un solo container con supervisord (CLAUDE.md).
FROM python:3.14-slim

# mediainfo: fornisce sia la CLI che libmediainfo, usate per calcolare
# l'Unique ID (vedi docs/SPEC.md sezione 6/11 e CLAUDE.md).
# ffmpeg: usato per gli screenshot nel modulo Upload (docs/SPEC.md sezione 9).
RUN apt-get update \
    && apt-get install -y --no-install-recommends mediainfo ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app app
COPY docs docs
COPY docker docker
COPY config.example.yaml .
COPY --from=frontend-build /frontend/dist frontend/dist
RUN chmod +x docker/entrypoint.sh

# /app/config va montato come cartella (mai un file), vedi docker/entrypoint.sh:
# se manca config.yaml al suo interno viene seminato da config.example.yaml.
ENV CONFIG_PATH=/app/config/config.yaml

# Versione e commit decisi dalla CI (.github/workflows/docker-publish.yml,
# app/version.py): vuoti in una build locale, che si mostra come "-dev".
ARG NAZGARR_VERSION=""
ARG NAZGARR_COMMIT=""
ENV NAZGARR_VERSION=${NAZGARR_VERSION} \
    NAZGARR_COMMIT=${NAZGARR_COMMIT}

EXPOSE 8080

# /api/health non richiede il login. Python c'è già: niente curl da aggiungere.
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=5)"

ENTRYPOINT ["docker/entrypoint.sh"]
CMD ["supervisord", "-c", "docker/supervisord.conf"]
