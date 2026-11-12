#!/usr/bin/env bash
# Create .env with random local credentials, or append any keys it is missing.
# Existing values are never changed (so volumes keep working).
set -eu
umask 077
touch .env
add() { grep -q "^$1=" .env || { echo "$1=$2" >> .env; echo "  .env: added $1"; }; }
add POSTGRES_USER darkstore
add POSTGRES_DB darkstore
add POSTGRES_PASSWORD "$(openssl rand -hex 16)"
add GRAFANA_ADMIN_PASSWORD "$(openssl rand -hex 16)"
add REDIS_PASSWORD "$(openssl rand -hex 16)"
