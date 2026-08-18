#!/usr/bin/env bash
# Run smoke tests inside a container where pymupdf is available.
# Uses podman if docker is not found.
set -euo pipefail

RUNTIME="${CONTAINER_RUNTIME:-}"
if [ -z "$RUNTIME" ]; then
  if command -v docker &>/dev/null; then RUNTIME=docker
  elif command -v podman &>/dev/null; then RUNTIME=podman
  else echo "Neither docker nor podman found."; exit 1; fi
fi

IMAGE=pdf-to-tif:smoke-test
CONTAINER=pagesmith-smoke
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== Building image ==="
$RUNTIME build -t "$IMAGE" "$PROJECT_DIR"

echo "=== Starting container ==="
$RUNTIME rm -f "$CONTAINER" 2>/dev/null || true
$RUNTIME run -d --name "$CONTAINER" \
  -e APP_PASSWORD=test \
  -e COOKIE_SECURE=false \
  -e MAX_PAGES=10000 \
  -e DEFAULT_DPI=100 \
  -e DATA_DIR=/data \
  -p 127.0.0.1:19000:9000 \
  "$IMAGE"

echo "=== Waiting for /healthz ==="
for i in $(seq 1 30); do
  if $RUNTIME exec "$CONTAINER" python3 -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/healthz', timeout=2)" \
    &>/dev/null; then
    echo "  Server is up (attempt $i)."
    break
  fi
  sleep 1
done

echo "=== Copying test script into container ==="
$RUNTIME cp "$SCRIPT_DIR/smoke_test.py" "$CONTAINER":/srv/smoke_test.py

echo "=== Running smoke tests ==="
$RUNTIME exec "$CONTAINER" python3 /srv/smoke_test.py \
  --base-url http://127.0.0.1:9000 \
  --password test \
  "$@"
EXIT_CODE=$?

echo "=== Stopping container ==="
$RUNTIME stop "$CONTAINER" 2>/dev/null || true
$RUNTIME rm -f "$CONTAINER" 2>/dev/null || true

exit $EXIT_CODE
