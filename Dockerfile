# syntax=docker/dockerfile:1

FROM python:3.13-slim

ARG PUID=1001
ARG PGID=1001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

RUN groupadd --gid "${PGID}" discordarr \
    && useradd --uid "${PUID}" --gid "${PGID}" --create-home \
        --home-dir /home/discordarr --shell /usr/sbin/nologin discordarr

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY src ./src
COPY LICENSE README.md pyproject.toml ./

RUN python -m pip install --no-cache-dir --no-deps . \
    && chown --recursive discordarr:discordarr /app

USER discordarr

ENTRYPOINT []
# Healthy only while the bot is connected to Discord: it touches a heartbeat
# file every 30s while its gateway connection is live (heartbeat.py). A
# running container with a dead or stuck bot would otherwise look fine.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD ["discordarr-healthcheck"]
CMD ["discordarr-bot"]
