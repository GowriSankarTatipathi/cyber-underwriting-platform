#!/bin/sh
set -eu
psql --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" --set=ON_ERROR_STOP=1 <<'SQL'
\getenv app_password DB_PASSWORD
CREATE ROLE cyber LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;
ALTER DATABASE cyber_underwriting OWNER TO cyber;
ALTER SCHEMA public OWNER TO cyber;
SQL
