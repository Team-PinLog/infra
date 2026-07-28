\set ON_ERROR_STOP on

-- Required input: psql --set=dev_role=<approved_identifier> ...
-- The role is deliberately created NOLOGIN. Credential owners must provision
-- LOGIN/password later through an approved secret channel; this file never does.
\if :{?dev_role}
\else
  \echo 'ERROR: dev_role is required'
  \quit 3
\endif

-- Fail closed before dynamic SQL; all identifier interpolation below uses %I.
SELECT 1 / ((:'dev_role' ~ '^[a-z_][a-z0-9_]{0,62}$')::int)
  AS approved_role_identifier;

SELECT format('CREATE ROLE %I NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS', :'dev_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'dev_role')
\gexec

-- Existing roles are accepted only when they retain the dedicated least-privilege shape.
SELECT 1 / ((EXISTS (
    SELECT 1
    FROM pg_roles r
    WHERE r.rolname = :'dev_role'
      AND r.rolsuper = false
      AND r.rolcreatedb = false
      AND r.rolcreaterole = false
      AND r.rolreplication = false
      AND r.rolbypassrls = false
      AND NOT EXISTS (
        SELECT 1 FROM pg_auth_members m
        WHERE m.roleid = r.oid OR m.member = r.oid
      )
  ))::int) AS role_is_dedicated_and_unprivileged;

SELECT format('CREATE DATABASE pinlog_dev OWNER %I', :'dev_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'pinlog_dev')
\gexec

-- Refuse to repurpose an existing database owned by another role.
SELECT 1 / ((EXISTS (
    SELECT 1
    FROM pg_database d
    JOIN pg_roles r ON r.oid = d.datdba
    WHERE d.datname = 'pinlog_dev' AND r.rolname = :'dev_role'
  ))::int) AS database_owner_matches;

REVOKE ALL ON DATABASE pinlog_dev FROM PUBLIC;
SELECT format('GRANT CONNECT, TEMPORARY ON DATABASE pinlog_dev TO %I', :'dev_role')
\gexec

-- Refuse any residual database ACL granted to a third party.
SELECT 1 / ((NOT EXISTS (
    SELECT 1
    FROM pg_database d
    CROSS JOIN LATERAL aclexplode(
      COALESCE(d.datacl, acldefault('d', d.datdba))
    ) acl
    WHERE d.datname = 'pinlog_dev' AND acl.grantee <> d.datdba
  ))::int) AS database_acl_is_exclusive;

\connect pinlog_dev
CREATE EXTENSION IF NOT EXISTS vector;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
SELECT format('GRANT USAGE, CREATE ON SCHEMA public TO %I', :'dev_role')
\gexec

-- Keep only the schema owner and approved role in explicit ACLs.
SELECT 1 / ((NOT EXISTS (
    SELECT 1
    FROM pg_namespace n
    CROSS JOIN LATERAL aclexplode(
      COALESCE(n.nspacl, acldefault('n', n.nspowner))
    ) acl
    WHERE n.nspname = 'public'
      AND acl.grantee NOT IN (
        n.nspowner,
        (SELECT oid FROM pg_roles WHERE rolname = :'dev_role')
      )
  ))::int) AS schema_acl_is_exclusive;
