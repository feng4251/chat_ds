FROM python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7

ARG DEBIAN_SNAPSHOT=20260720T000000Z

LABEL org.opencontainers.image.title="ChatDS Skill egress policy proxy" \
      org.opencontainers.image.description="Fail-closed exact-method and URL-prefix HTTP/TLS boundary for isolated Skill workers" \
      org.opencontainers.image.chatds.egress-policy="signed-route-idle-v2" \
      org.opencontainers.image.chatds.debian-snapshot="${DEBIAN_SNAPSHOT}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN rm -f /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' \
      "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/${DEBIAN_SNAPSHOT}/ bookworm main" \
      "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/${DEBIAN_SNAPSHOT}/ bookworm-security main" \
      > /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends \
      ca-certificates \
      libcap2-bin \
      openssl \
    && test -x /usr/bin/openssl \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 65531 skill-egress \
    && useradd --uid 65531 --gid 65531 --no-create-home \
      --home-dir /nonexistent --shell /usr/sbin/nologin skill-egress \
    && install -d -o 65531 -g 65531 -m 0500 /app

COPY --chown=65531:65531 server.py /app/server.py

RUN test "$(python -c 'import runpy; print(runpy.run_path("/app/server.py")["EGRESS_POLICY_RUNTIME_VERSION"])')" = "signed-route-idle-v2" \
    && find / -xdev -type f -perm /6000 -exec chmod a-s {} + \
    && getcap -r / 2>/dev/null | cut -d ' ' -f 1 | xargs -r setcap -r \
    && test -z "$(find / -xdev -type f -perm /6000 -print -quit)" \
    && test -z "$(getcap -r / 2>/dev/null)"

LABEL org.opencontainers.image.chatds.setid-stripped="true"

USER 65531:65531

ENTRYPOINT ["/usr/local/bin/python", "-I", "/app/server.py"]
