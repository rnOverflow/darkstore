#!/usr/bin/env bash
set -eu
umask 077
{
  echo "POSTGRES_USER=darkstore"
  echo "POSTGRES_DB=darkstore"
  echo "POSTGRES_PASSWORD=$(openssl rand -hex 16)"
  echo "GRAFANA_ADMIN_PASSWORD=$(openssl rand -hex 16)"
} > .env
echo "generated .env"
