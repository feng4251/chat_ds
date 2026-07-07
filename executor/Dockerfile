FROM python:3.12-slim

WORKDIR /app
COPY server.py /app/server.py

# Install common scientific computing & data libraries so execute_code can
# run numpy/scipy/pandas/matplotlib/sklearn workloads out of the box.
# The executor has network_mode: none, so all dependencies must be baked in
# at build time.  Use the Tsinghua PyPI mirror for speed & reliability in CN.
RUN pip install --no-cache-dir \
    -i https://pypi.tuna.tsinghua.edu.cn/simple \
    numpy \
    scipy \
    pandas \
    matplotlib \
    scikit-learn \
    sympy \
    networkx \
    Pillow \
    openpyxl \
    xlsxwriter

RUN mkdir -p /run/chat-ds-executor && chmod 0777 /run/chat-ds-executor

# Matplotlib needs a writable config dir; the container root is read-only.
ENV MPLCONFIGDIR=/tmp/matplotlib
ENV PYTHONUNBUFFERED=1

CMD ["python", "-I", "/app/server.py"]
