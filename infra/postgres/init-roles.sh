#!/bin/sh
# One login role per service, each owning ONLY its own schema (least privilege: a compromised service
# cannot read or alter another service's tables). The superuser (POSTGRES_USER) stays for operators only.
# ops-api gets read-only access to the schemas its dashboards query. Runs once, on an empty data volume.
set -eu

role() {   # role() <schema> <env password var>
  eval "pw=\${$2:?$2 is not set (run make env)}"
  psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
       -v role="svc_$1" -v schema="$1" -v pw="$pw" <<'SQL'
CREATE ROLE :"role" LOGIN PASSWORD :'pw' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE SCHEMA :"schema" AUTHORIZATION :"role";
SQL
}

role orders PG_PASSWORD_ORDER
role inventory PG_PASSWORD_INVENTORY
role picking PG_PASSWORD_PICKING
role dispatch PG_PASSWORD_DISPATCH
role rider_sim PG_PASSWORD_RIDER_SIM
role eta PG_PASSWORD_ETA
role gateway PG_PASSWORD_GATEWAY

psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v pw="${PG_PASSWORD_OPS_API:?PG_PASSWORD_OPS_API is not set}" <<'SQL'
CREATE ROLE svc_ops_api LOGIN PASSWORD :'pw' NOSUPERUSER NOCREATEDB NOCREATEROLE;
GRANT USAGE ON SCHEMA orders, picking, eta TO svc_ops_api;
-- tables are created later by each owning service: grant SELECT on whatever it creates
ALTER DEFAULT PRIVILEGES FOR ROLE svc_orders  IN SCHEMA orders  GRANT SELECT ON TABLES TO svc_ops_api;
ALTER DEFAULT PRIVILEGES FOR ROLE svc_picking IN SCHEMA picking GRANT SELECT ON TABLES TO svc_ops_api;
ALTER DEFAULT PRIVILEGES FOR ROLE svc_eta     IN SCHEMA eta     GRANT SELECT ON TABLES TO svc_ops_api;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
SQL
