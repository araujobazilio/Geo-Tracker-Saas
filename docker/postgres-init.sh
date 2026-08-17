#!/bin/bash
# PostgreSQL init script: create the dedicated test database.
# Mounted into /docker-entrypoint-initdb.d/ by docker-compose.
# Only runs on first initialization of the data volume.
set -e

echo "Creating test database: geo_tracker_test"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE DATABASE geo_tracker_test;
EOSQL
echo "Test database created."
