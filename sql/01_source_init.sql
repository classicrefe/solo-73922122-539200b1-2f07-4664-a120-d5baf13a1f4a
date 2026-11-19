-- Source database initialization:
--   1. load the demo cyclic schema (no row data!)
--   2. create a read-only login used by the API; it has no privilege to
--      modify anything and cannot even read table rows (only catalogs, which
--      are visible to every role by default).
-- Runs once on first container start via /docker-entrypoint-initdb.d.

\set ON_ERROR_STOP on

\i /sql/00_demo_source_schema.sql

-- Dedicated least-privilege role for structure introspection.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_ro') THEN
        CREATE ROLE app_ro LOGIN PASSWORD 'app_ro_pwd';
    END IF;
END
$$;

-- Connect to the application database and lock the role down.
\connect appdb

REVOKE ALL ON SCHEMA public FROM app_ro;
GRANT USAGE ON SCHEMA public TO app_ro;
-- No table-level privileges: cannot SELECT rows, cannot write.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM app_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM app_ro;

-- Explicitly deny writes at role level as defence in depth (the app also
-- SETs default_transaction_read_only = on on every source connection).
ALTER ROLE app_ro SET default_transaction_read_only = on;
