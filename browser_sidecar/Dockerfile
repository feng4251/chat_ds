FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp/browser-home \
    XDG_CACHE_HOME=/tmp/browser-cache \
    XDG_CONFIG_HOME=/tmp/browser-config

# Chromium and its fonts live only in the disposable browser boundary.  The
# Harness image contains the Playwright client but no locally executable
# browser, so a missing sidecar cannot silently fall back to an unsafe launch.
RUN sed -i \
    -e 's|http://deb.debian.org/debian-security|https://mirrors.tuna.tsinghua.edu.cn/debian-security|g' \
    -e 's|http://deb.debian.org/debian|https://mirrors.tuna.tsinghua.edu.cn/debian|g' \
    /etc/apt/sources.list.d/debian.sources && \
    apt-get update && apt-get install -y --no-install-recommends \
      chromium chromium-sandbox ca-certificates fonts-noto-cjk && \
    rm -rf /var/lib/apt/lists/* && \
    groupadd --gid 65532 browser && \
    useradd --uid 65532 --gid 65532 --no-create-home \
      --home-dir /nonexistent --shell /usr/sbin/nologin browser && \
    install -d -o 65532 -g 65532 -m 0700 /app && \
    install -d -o 65532 -g 65532 -m 0700 /run/chat-ds-browser

COPY --chown=65532:65532 server.py /app/server.py

USER 65532:65532

ENTRYPOINT ["python", "-I", "/app/server.py"]
