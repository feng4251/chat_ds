FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

LABEL org.opencontainers.image.title="ChatDS typed public market-data gateway" \
      org.opencontainers.image.description="Fixed-upstream, read-only quote broker for isolated agent Turns"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 65532 market-data \
    && useradd --uid 65532 --gid 65532 --no-create-home \
      --home-dir /nonexistent --shell /usr/sbin/nologin market-data \
    && install -d -o 65532 -g 65532 -m 0500 /app

COPY --chown=65532:65532 server.py /app/server.py

RUN chmod 0444 /app/server.py \
    && find / -xdev -type f -perm /6000 -exec chmod a-s {} + \
    && test -z "$(find / -xdev -type f -perm /6000 -print -quit)"

USER 65532:65532
EXPOSE 8090
ENTRYPOINT ["/usr/local/bin/python", "-I", "/app/server.py"]
