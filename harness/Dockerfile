FROM python:3.12-slim
WORKDIR /app

# TUNA mirrors (faster from China intranet)
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

# trafilatura needs libxml2 / libxslt at runtime
# Playwright needs additional system deps for headless Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libxml2-dev libxslt1-dev \
    libnss3 libnspr4 libatk-bridge2.0-0 libatk1.0-0 libcups2 libdrm2 \
    libdbus-1-3 libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2t64 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium for Playwright (system deps only, reuses the chromium bundle)
# RUN python -m playwright install chromium --with-deps  # disabled: network issues

# Clean up build deps (keep runtime libs)
RUN apt-get purge -y --auto-remove build-essential libxml2-dev libxslt1-dev && \
    rm -rf /var/lib/apt/lists/*

COPY . .

EXPOSE 8020

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8020"]