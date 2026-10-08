# Stage 1: build del frontend (Fase 8, docs/SPEC.md §10-11) - Node resta
# solo in questo stage, mai nell'immagine finale (nessun runtime Node in
# produzione, solo i file statici prodotti da `vite build`).
# --platform=$BUILDPLATFORM: il frontend sono solo file statici, identici per
# ogni architettura. Si costruisce una volta sola, nativo sulla macchina di
# build, e lo stesso dist va in tutte le immagini: prima l'arm64 lo ricostruiva
# emulato (QEMU), ~3 minuti in più a ogni build.
# Immagini base fissate per digest: una ricostruzione completa solo quando
# Dependabot propone il nuovo digest e la PR viene approvata, mai a sorpresa
# (e una build riproducibile).
FROM --platform=$BUILDPLATFORM node:25-slim@sha256:81db02c4b671288a03915da9534dbd54f96d0e7c24d80ccc54f5b36b2e684370 AS frontend-build

WORKDIR /frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend .
RUN npm run build

# Stage 2: backend Python + frontend statico servito dallo stesso
# container (nazgarr/web/frontend.py) - un solo container con supervisord, un solo processo (CLAUDE.md).
FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016

# ffmpeg (solo per gli screenshot del modulo Upload, docs/SPEC.md sezione 9):
# un binario statico invece del pacchetto Debian, che si portava dietro ~170 MB
# compressi di librerie (encoder, audio di sistema, grafica) mai usate. Questo
# ha i decoder, zscale/tonemap per l'HDR e signalstats, per amd64 e arm64.
# ffprobe serve al ripiego per il Dolby Vision che il contenitore non
# dichiara (nazgarr/upload/dovi_probe.py). Fissato per digest come le immagini base.
# MediaInfo non serve dal sistema: pymediainfo porta con sé libmediainfo
# nelle sue wheel Linux (amd64 e arm64), e la CLI non viene mai chiamata.
COPY --from=mwader/static-ffmpeg:7.1@sha256:a8090df5f5608daef387e1b2e93b98aaacb4d92153ad904e7d715c725724fca4 \
    /ffmpeg /ffprobe /usr/local/bin/

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY nazgarr nazgarr
COPY docs docs
COPY docker docker
COPY config.example.yaml .
COPY --from=frontend-build /frontend/dist frontend/dist
# `nazgarr` anche dentro il container (docs/CLI.md): docker exec -it nazgarr nazgarr status
RUN chmod +x docker/entrypoint.sh \
    && printf '#!/bin/sh\nexec python -m nazgarr.cli "$@"\n' > /usr/local/bin/nazgarr \
    && chmod +x /usr/local/bin/nazgarr
# Il CLI dentro il container parla con il server dello stesso container, e
# ricorda la sua API key nella cartella di configurazione (persistente).
ENV NAZGARR_URL=http://127.0.0.1:3019 \
    NAZGARR_CLI_CONFIG=/app/config/cli.toml

# /app/config va montato come cartella (mai un file), vedi docker/entrypoint.sh:
# se manca config.yaml al suo interno viene seminato da config.example.yaml.
ENV CONFIG_PATH=/app/config/config.yaml

# Dentro un container il confine dei dischi sono le cartelle montate
# (nazgarr/core/mounts.py): niente disk_scan_root da scrivere.
ENV NAZGARR_CONTAINER=1

# Versione e commit decisi dalla CI (.github/workflows/docker-publish.yml,
# nazgarr/core/version.py): vuoti in una build locale, che si mostra come "-dev".
ARG NAZGARR_VERSION=""
ARG NAZGARR_COMMIT=""
ENV NAZGARR_VERSION=${NAZGARR_VERSION} \
    NAZGARR_COMMIT=${NAZGARR_COMMIT}

EXPOSE 3019

# /api/health non richiede il login. Python c'è già: niente curl da aggiungere.
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3019/api/health', timeout=5)"

ENTRYPOINT ["docker/entrypoint.sh"]
CMD ["supervisord", "-c", "docker/supervisord.conf"]
