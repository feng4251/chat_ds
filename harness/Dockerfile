FROM python:3.12-slim
WORKDIR /app

# TUNA mirrors (faster from China intranet).  The default Debian CDN is slow
# enough on the production network to make immutable browser-image builds
# unreliable, so route both the main and security suites explicitly.
RUN sed -i \
    -e 's|http://deb.debian.org/debian-security|https://mirrors.tuna.tsinghua.edu.cn/debian-security|g' \
    -e 's|http://deb.debian.org/debian|https://mirrors.tuna.tsinghua.edu.cn/debian|g' \
    /etc/apt/sources.list.d/debian.sources

RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

# trafilatura needs libxml2 / libxslt at runtime.  Chromium is deliberately
# absent: browser execution lives in the locked-down browser sidecar and the
# Harness is only a Playwright CDP client.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libxml2-dev libxslt1-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Clean up build deps (keep runtime libs)
RUN apt-get purge -y --auto-remove build-essential libxml2-dev libxslt1-dev && \
    rm -rf /var/lib/apt/lists/*

COPY . .

EXPOSE 8020

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8020"]
