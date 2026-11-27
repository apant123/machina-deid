# De-identification pipeline image (Stage 1-4). Reproduces the EC2 run on AWS Batch.
#   docker build -f infra/Dockerfile -t machina-deid .
#   docker run --rm -e AWS_REGION=us-east-2 machina-deid python -m deid.rewrite_run run --workers 14
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 UV_LINK_MODE=copy
RUN apt-get update && apt-get install -y --no-install-recommends \
        p7zip-full 7zip 7zip-rar ncompress xvfb fonts-dejavu-core \
        libopengl0 libegl1 libfontconfig1 libxkbcommon-x11-0 libxcb-cursor0 libgl1 libglib2.0-0 \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# ODA File Converter (free; licence accepted by the operator). Download the .deb from
# https://www.opendesign.com/guestfiles/oda_file_converter and place it next to this Dockerfile.
COPY infra/ODAFileConverter*.deb /tmp/oda.deb
RUN apt-get update && apt-get install -y /tmp/oda.deb && rm -f /tmp/oda.deb && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY deid ./deid
COPY config ./config
COPY tools ./tools

# Neutral account: ODA stamps $LASTSAVEDBY with the OS user running it.
RUN useradd --create-home --uid 1000 deid && mkdir -p /data && chown deid /data
USER deid
ENV PATH="/app/.venv/bin:$PATH" DEID_DATA_DIR=/data HF_HOME=/data/hf
# Models are fetched once into the data volume (GLiNER for Stage 2, OWLv2 for the logo pass).
ENTRYPOINT ["/bin/sh", "-c", "Xvfb :99 -screen 0 1024x768x24 -nolisten tcp & export DISPLAY=:99; exec \"$@\"", "--"]
CMD ["python", "-m", "deid.rewrite_run", "--help"]
