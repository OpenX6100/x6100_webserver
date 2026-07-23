#!/bin/bash
# Deploy views (and optionally static) to device. No rebuild needed.
# Usage:
#   ./deploy-views.sh [user@]host [remote_path]
# Example:
#   ./deploy-views.sh root@192.168.44.1
#   ./deploy-views.sh root@x6100.local /usr/lib/python3.11/site-packages/x6100_webserver
#
# If Python version differs on device, find path with:
#   ssh root@host "python3 -c \"import x6100_webserver; print(x6100_webserver.__path__[0])\""

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC="${SCRIPT_DIR}/src/x6100_webserver"
HOST="${1:?Usage: $0 user@host [remote_path]}"
REMOTE_PATH="${2:-/usr/lib/python3.11/site-packages/x6100_webserver}"

echo "Deploying views from $SRC/views/ to $HOST:$REMOTE_PATH/views/"
rsync -avz --progress "${SRC}/views/" "${HOST}:${REMOTE_PATH}/views/"

echo "Done. Optional: deploy static (css/js) with:"
echo "  rsync -avz $SRC/static/ $HOST:${REMOTE_PATH}/static/"
