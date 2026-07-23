FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

LABEL org.opencontainers.image.title="ChatDS Skill egress policy proxy" \
      org.opencontainers.image.description="Fail-closed HTTP CONNECT boundary for isolated Skill browser workers"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 65531 skill-egress \
    && useradd --uid 65531 --gid 65531 --no-create-home \
      --home-dir /nonexistent --shell /usr/sbin/nologin skill-egress \
    && install -d -o 65531 -g 65531 -m 0500 /app

COPY --chown=65531:65531 server.py /app/server.py

USER 65531:65531

ENTRYPOINT ["/usr/local/bin/python", "-I", "/app/server.py"]
