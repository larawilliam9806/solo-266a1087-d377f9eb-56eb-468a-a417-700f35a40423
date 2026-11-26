FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

# Install dependencies first for better layer caching.
# Fall back to a nearby PyPI mirror if the default index is unreachable.
COPY requirements.txt ./
RUN pip install --timeout 30 --retries 2 -r requirements.txt \
    || pip install --timeout 30 -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# Copy application code.
COPY app ./app
COPY main.py ./main.py
COPY server.py ./server.py

# Persistent location for sessions / temporary extraction files.
RUN mkdir -p /data \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /data /app
USER appuser

VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=4s --start-period=6s --retries=6 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

# Shell form so $HOST / $PORT are expanded.
CMD sh -c "uvicorn app.main:app --host ${HOST} --port ${PORT}"
