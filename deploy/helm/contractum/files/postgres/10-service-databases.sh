#!/usr/bin/env bash
# One database and one login role per service (PLAT-05). Runs only on an empty data directory,
# as the postgres user, with the per-service passwords mounted at /run/secrets.
# Passwords reach SQL only as psql variables (:'pw'), never through string interpolation.
set -euo pipefail

for svc in identity contracts validation audit; do
  pw_file="/run/secrets/${svc}_db_password"
  [ -s "$pw_file" ] || { echo "init: missing ${pw_file}" >&2; exit 1; }
  pw="$(cat "$pw_file")"
  psql -v ON_ERROR_STOP=1 -v pw="$pw" -v svc="$svc" --username postgres --dbname postgres <<'SQL'
CREATE ROLE :"svc" LOGIN PASSWORD :'pw';
CREATE DATABASE :"svc" OWNER :"svc";
REVOKE ALL ON DATABASE :"svc" FROM PUBLIC;
SQL
done

# ltree is created here as superuser so no trusted-extension rule can block identity's migration (A7).
psql -v ON_ERROR_STOP=1 --username postgres --dbname identity -c 'CREATE EXTENSION IF NOT EXISTS ltree'
