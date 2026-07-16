-- conpass — explicit service_role grants
--
-- Supabase normally auto-grants new public objects to anon/authenticated/service_role,
-- but that only fires for its own creation path. These migrations create tables directly
-- (postgres role via pooler), so `service_role` (the backend's secret-key role) ends up
-- without privileges. Grant it explicitly — the deliberate, auditable version of the
-- safety strategy: service_role (backend) = full DML; anon/authenticated = none.
--
-- NB: `GRANT ALL PRIVILEGES` behaves oddly for the Supabase `postgres` role (it conveys
-- only REFERENCES/TRIGGER/TRUNCATE, not the DML privileges), so name the DML privileges
-- explicitly.

grant usage on schema public to service_role;

grant select, insert, update, delete on all tables    in schema public to service_role;
grant usage, select, update         on all sequences in schema public to service_role;
grant execute                        on all functions in schema public to service_role;

-- Future objects created in this schema also go to service_role only.
alter default privileges in schema public
    grant select, insert, update, delete on tables to service_role;
alter default privileges in schema public
    grant usage, select, update on sequences to service_role;
alter default privileges in schema public
    grant execute on functions to service_role;

-- Ask PostgREST (the Data API) to reload its schema/permission cache.
notify pgrst, 'reload schema';
