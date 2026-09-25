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
CMD ["discordarr-bot"]
