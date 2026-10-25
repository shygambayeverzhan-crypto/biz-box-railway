#!/bin/sh
set -eu

mkdir -p /app/data
chown -R appuser:appuser /app/data
exec su -s /bin/sh appuser -c 'python -m arbscanner'
