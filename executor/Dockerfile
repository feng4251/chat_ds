# Legacy development image only. Production Compose uses
# Dockerfile.browser-runtime:session-sandbox so every supported Skill runtime
# shares the same immutable dependency and isolation profile.
FROM python:3.12-slim

WORKDIR /app

# Keep image builds reproducible on the same intranet path as the backend.
# Debian slim images use deb822 sources under sources.list.d.
RUN sed -i 's|http://deb.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g; s|http://security.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g' \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null || true

# Skill JavaScript entrypoints use a fixed interpreter selected by extension.
# The service still runs with network_mode: none; callers cannot select a binary.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs util-linux \
    && rm -rf /var/lib/apt/lists/*

# Install the shared exact-Skill dependency manifest. Specialized runtime
# profiles resolve this same manifest into their immutable lock so browser and
# non-browser routes cannot silently expose different common Python surfaces.
COPY runtime/common-python-requirements.in /tmp/common-python-requirements.in
RUN pip install --no-cache-dir \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    -r /tmp/common-python-requirements.in

COPY server.py /app/server.py

RUN mkdir -p /run/chat-ds-executor && chmod 0755 /run/chat-ds-executor

# Matplotlib needs a writable config dir; the container root is read-only.
ENV MPLCONFIGDIR=/tmp/matplotlib
ENV PYTHONUNBUFFERED=1

CMD ["python", "-I", "/app/server.py"]
