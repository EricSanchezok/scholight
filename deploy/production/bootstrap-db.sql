\set ON_ERROR_STOP on

-- Scholight production database privilege bootstrap.
-- Required psql variables:
--   app_role       existing LOGIN role used by the API
--   migrator_role  existing LOGIN role used only by `scholight store migrate`
--
-- Run as the database owner before the first migration, then run it again after
-- the migration. Re-running is safe and grants privileges on newly created or
-- legacy objects without storing role passwords in this file.

SELECT format('GRANT CONNECT ON DATABASE %I TO %I', current_database(), :'app_role') \gexec
SELECT format('GRANT CONNECT, CREATE ON DATABASE %I TO %I', current_database(), :'migrator_role') \gexec

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO :"app_role";
GRANT USAGE, CREATE ON SCHEMA public TO :"migrator_role";

-- Create auth under the intended owner before cloud-auth migrations. For a
-- legacy schema, the ALTER transfers ownership while run as the database owner.
SELECT format('CREATE SCHEMA IF NOT EXISTS auth AUTHORIZATION %I', :'migrator_role') \gexec
SELECT format('ALTER SCHEMA auth OWNER TO %I', :'migrator_role') \gexec
REVOKE CREATE ON SCHEMA auth FROM PUBLIC;
GRANT USAGE ON SCHEMA auth TO :"app_role";
GRANT USAGE, CREATE ON SCHEMA auth TO :"migrator_role";

-- Existing application objects may have been created by an old owner. Transfer
-- them to the dedicated migrator before it needs to ALTER them in a later release.
SELECT format('ALTER TABLE %I.%I OWNER TO %I', schemaname, tablename, :'migrator_role')
FROM pg_tables
WHERE schemaname IN ('auth', 'public')
  AND tablename IN (
    '_cloud_auth_migrations',
    '_migrations',
    'anonymous_daily_search_usage',
    'daily_usage',
    'refresh_tokens',
    'search_history',
    'user_quotas',
    'users'
  )
ORDER BY schemaname, tablename \gexec

SELECT format('ALTER TYPE %I OWNER TO %I', type_name, :'migrator_role')
FROM (VALUES ('account_status'), ('operation_type')) AS known_types(type_name)
WHERE to_regtype(type_name) IS NOT NULL
ORDER BY type_name \gexec

SELECT format('ALTER SEQUENCE %I.%I OWNER TO %I', sequence_schema, sequence_name, :'migrator_role')
FROM information_schema.sequences
WHERE sequence_schema IN ('auth', 'public')
  AND sequence_name IN (
    'daily_usage_id_seq',
    'refresh_tokens_id_seq',
    'search_history_id_seq',
    'user_quotas_id_seq',
    'users_id_seq'
  )
ORDER BY sequence_schema, sequence_name \gexec

SELECT format(
  'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE %I.%I TO %I',
  schemaname,
  tablename,
  :'app_role'
)
FROM pg_tables
WHERE (schemaname = 'public' AND tablename IN ('anonymous_daily_search_usage', 'search_history'))
   OR (schemaname = 'auth' AND tablename IN ('daily_usage', 'refresh_tokens', 'user_quotas', 'users'))
ORDER BY schemaname, tablename \gexec

SELECT format(
  'GRANT USAGE, SELECT ON SEQUENCE %I.%I TO %I',
  sequence_schema,
  sequence_name,
  :'app_role'
)
FROM information_schema.sequences
WHERE sequence_schema IN ('auth', 'public')
  AND sequence_name IN (
    'daily_usage_id_seq',
    'refresh_tokens_id_seq',
    'search_history_id_seq',
    'user_quotas_id_seq',
    'users_id_seq'
  )
ORDER BY sequence_schema, sequence_name \gexec

SELECT format('GRANT USAGE ON TYPE %I TO %I', type_name, :'app_role')
FROM (VALUES ('account_status'), ('operation_type')) AS known_types(type_name)
WHERE to_regtype(type_name) IS NOT NULL
ORDER BY type_name \gexec

ALTER DEFAULT PRIVILEGES FOR ROLE :"migrator_role" IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO :"app_role";
ALTER DEFAULT PRIVILEGES FOR ROLE :"migrator_role" IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO :"app_role";
SELECT format(
  'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA auth '
  'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I',
  :'migrator_role',
  :'app_role'
)
WHERE to_regnamespace('auth') IS NOT NULL \gexec
SELECT format(
  'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA auth '
  'GRANT USAGE, SELECT ON SEQUENCES TO %I',
  :'migrator_role',
  :'app_role'
)
WHERE to_regnamespace('auth') IS NOT NULL \gexec

SELECT format('REVOKE ALL ON TABLE public.%I FROM %I', ledger_name, :'app_role')
FROM (VALUES ('_cloud_auth_migrations'), ('_migrations')) AS ledgers(ledger_name)
WHERE to_regclass(format('public.%I', ledger_name)) IS NOT NULL
ORDER BY ledger_name \gexec

REVOKE CREATE ON SCHEMA public FROM :"app_role";
REVOKE CREATE ON SCHEMA auth FROM :"app_role";
SELECT format('REVOKE CREATE ON DATABASE %I FROM %I', current_database(), :'app_role') \gexec
