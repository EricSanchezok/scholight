\set ON_ERROR_STOP on

-- SanchezCloud database privilege bootstrap for Scholight.
-- Required existing LOGIN roles:
--   auth_migrator_role       owns only auth.*
--   product_migrator_role    owns only scholight.*
--   app_role                 runs the Scholight API
--
-- Run as the database owner before sanchezcloud-identity migration, after sanchezcloud-identity
-- migration, and after Scholight migration. Re-running is safe.

SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'app_role') \gexec
SELECT format(
  'GRANT CONNECT ON DATABASE %I TO %I',
  current_database(),
  :'auth_migrator_role'
) \gexec
SELECT format(
  'GRANT CONNECT ON DATABASE %I TO %I',
  current_database(),
  :'product_migrator_role'
) \gexec

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM :"app_role";
REVOKE ALL ON SCHEMA public FROM :"auth_migrator_role";
REVOKE ALL ON SCHEMA public FROM :"product_migrator_role";

SELECT format(
  'CREATE SCHEMA IF NOT EXISTS auth AUTHORIZATION %I',
  :'auth_migrator_role'
) \gexec
SELECT format('ALTER SCHEMA auth OWNER TO %I', :'auth_migrator_role') \gexec
SELECT format(
  'CREATE SCHEMA IF NOT EXISTS scholight AUTHORIZATION %I',
  :'product_migrator_role'
) \gexec
SELECT format('ALTER SCHEMA scholight OWNER TO %I', :'product_migrator_role') \gexec

REVOKE CREATE ON SCHEMA auth FROM PUBLIC;
REVOKE CREATE ON SCHEMA scholight FROM PUBLIC;
GRANT USAGE ON SCHEMA auth TO :"app_role", :"product_migrator_role";
GRANT USAGE ON SCHEMA scholight TO :"app_role";
GRANT USAGE, CREATE ON SCHEMA auth TO :"auth_migrator_role";
GRANT USAGE, CREATE ON SCHEMA scholight TO :"product_migrator_role";

-- These grants become available after the independent sanchezcloud-identity baseline.
SELECT format(
  'GRANT SELECT, INSERT, UPDATE ON TABLE auth.users, '
  'auth.refresh_tokens TO %I',
  :'app_role'
)
WHERE to_regclass('auth.users') IS NOT NULL
  AND to_regclass('auth.refresh_tokens') IS NOT NULL \gexec
SELECT format(
  'GRANT SELECT, INSERT, UPDATE ON TABLE auth.user_clients TO %I',
  :'app_role'
)
WHERE to_regclass('auth.user_clients') IS NOT NULL \gexec
SELECT format(
  'GRANT SELECT ON TABLE auth.schema_migrations TO %I',
  :'product_migrator_role'
)
WHERE to_regclass('auth.schema_migrations') IS NOT NULL \gexec
SELECT format(
  'GRANT REFERENCES ON TABLE auth.users TO %I',
  :'product_migrator_role'
)
WHERE to_regclass('auth.users') IS NOT NULL \gexec

SELECT format(
  'GRANT USAGE, SELECT ON SEQUENCE %I.%I TO %I',
  sequence_schema,
  sequence_name,
  :'app_role'
)
FROM information_schema.sequences
WHERE sequence_schema = 'auth'
  AND sequence_name IN ('users_id_seq', 'refresh_tokens_id_seq')
ORDER BY sequence_name \gexec

-- These grants become available after the Scholight baseline.
SELECT format(
  'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %I.%I TO %I',
  schemaname,
  tablename,
  :'app_role'
)
FROM pg_tables
WHERE schemaname = 'scholight'
  AND tablename <> 'schema_migrations'
ORDER BY tablename \gexec

SELECT format(
  'GRANT USAGE, SELECT ON SEQUENCE %I.%I TO %I',
  sequence_schema,
  sequence_name,
  :'app_role'
)
FROM information_schema.sequences
WHERE sequence_schema = 'scholight'
ORDER BY sequence_name \gexec

ALTER DEFAULT PRIVILEGES FOR ROLE :"product_migrator_role" IN SCHEMA scholight
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app_role";
ALTER DEFAULT PRIVILEGES FOR ROLE :"product_migrator_role" IN SCHEMA scholight
  GRANT USAGE, SELECT ON SEQUENCES TO :"app_role";

SELECT format(
  'REVOKE ALL ON TABLE auth.schema_migrations FROM %I',
  :'app_role'
)
WHERE to_regclass('auth.schema_migrations') IS NOT NULL \gexec
SELECT format(
  'REVOKE ALL ON TABLE scholight.schema_migrations FROM %I',
  :'app_role'
)
WHERE to_regclass('scholight.schema_migrations') IS NOT NULL \gexec

REVOKE CREATE ON SCHEMA auth FROM :"app_role", :"product_migrator_role";
REVOKE CREATE ON SCHEMA scholight FROM :"app_role", :"auth_migrator_role";
SELECT format('REVOKE CREATE ON DATABASE %I FROM %I', current_database(), :'app_role') \gexec
SELECT format(
  'REVOKE CREATE ON DATABASE %I FROM %I',
  current_database(),
  :'auth_migrator_role'
) \gexec
SELECT format(
  'REVOKE CREATE ON DATABASE %I FROM %I',
  current_database(),
  :'product_migrator_role'
) \gexec
