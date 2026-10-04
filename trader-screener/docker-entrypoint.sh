#!/bin/sh
# Seed the mounted data directory from the image on first start.
#
# The snapshot is no longer baked into the running path: it lives in a mounted
# directory so a cron can refresh it without a rebuild. But a brand-new host
# has an empty directory, and the server exits without a snapshot -- so the
# image still carries one, and it is copied in exactly once, when the mount is
# empty. An existing (possibly much fresher) snapshot is never overwritten.
set -eu

DATA_DIR="${SCREENER_DATA_DIR:-/app/data}"
mkdir -p "$DATA_DIR"

if [ ! -f "$DATA_DIR/dataset.json" ]; then
  echo "  seeding $DATA_DIR from the image's bundled snapshot"
  cp /app/seed-data/dataset.json /app/seed-data/smi.json "$DATA_DIR/"
fi

exec "$@"
