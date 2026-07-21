FROM python:3.12-slim

WORKDIR /app

# Keep image builds reproducible on the same intranet path as the backend.
# Debian slim images use deb822 sources under sources.list.d.
RUN sed -i 's|http://deb.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g; s|http://security.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g' \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null || true

# Skill JavaScript entrypoints use a fixed interpreter selected by extension.
# The service still runs with network_mode: none; callers cannot select a binary.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install common scientific computing & data libraries so execute_code can
# run numpy/scipy/pandas/matplotlib/sklearn workloads out of the box.
# The executor has network_mode: none, so all dependencies must be baked in
# at build time.  Use the Tsinghua PyPI mirror for speed & reliability in CN.
RUN pip install --no-cache-dir \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    packaging \
    numpy \
    scipy \
    pandas \
    matplotlib \
    scikit-learn \
    sympy \
    networkx \
    Pillow \
    PyYAML \
    requests \
    httpx \
    statsmodels \
    pypdf \
    pdfplumber \
    reportlab \
    python-docx \
    python-pptx \
    openpyxl \
    xlsxwriter

COPY server.py /app/server.py

RUN mkdir -p /run/chat-ds-executor && chmod 0777 /run/chat-ds-executor

# Matplotlib needs a writable config dir; the container root is read-only.
ENV MPLCONFIGDIR=/tmp/matplotlib
ENV PYTHONUNBUFFERED=1

CMD ["python", "-I", "/app/server.py"]
