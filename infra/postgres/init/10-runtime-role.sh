#!/bin/sh
# Mirrors the Neon setup: the runtime role gets DML only on objects the migrator creates.
set -eu
: "${RUNTIME_DB_PASSWORD:?RUNTIME_DB_PASSWORD is required}"
psql -v ON_ERROR_STOP=1 -v runtime_password="$RUNTIME_DB_PASSWORD" \
  --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
CREATE ROLE agentping_runtime LOGIN PASSWORD :'runtime_password';
GRANT CONNECT ON DATABASE agentping TO agentping_runtime;
GRANT USAGE ON SCHEMA public TO agentping_runtime;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE agentping_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO agentping_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE agentping_migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO agentping_runtime;
SQL
