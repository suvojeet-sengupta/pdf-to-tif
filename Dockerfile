# ---- build stage: resolve wheels into a self-contained virtualenv ----------
FROM python:3.12-slim AS build

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt

# ---- runtime stage --------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    DATA_DIR=/data \
    PORT=9000

COPY --from=build /opt/venv /opt/venv

WORKDIR /srv
COPY app ./app

# Run unprivileged; /data holds uploads, rendered TIFFs and finished archives.
RUN useradd --system --uid 10001 --create-home appuser \
 && mkdir -p /data \
 && chown -R appuser:appuser /data /srv
USER appuser

VOLUME ["/data"]
EXPOSE 9000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ['PORT']+'/healthz', timeout=4).status==200 else 1)"

# One uvicorn worker on purpose: job state lives in memory and the heavy
# rendering already fans out across a pool of subprocesses.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1 --timeout-keep-alive 75 --proxy-headers --forwarded-allow-ips '*'"]
