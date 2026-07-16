-- conpass — Data API safety strategy
--
-- Context: app tables live in `public`, which Supabase exposes through the Data API
-- (PostgREST). Supabase's default privileges GRANT new public tables to `anon` and
-- `authenticated`, so RLS is the only thing standing between a leaked publishable key
-- and the data. Our architecture does not need direct client table access at all:
--   * Frontend (publishable key) talks to Supabase ONLY for Auth.
--   * All business reads/writes go through the backend Lambdas, which use the SECRET
--     key (service_role, BYPASSRLS) via the Data API.
-- So we lock the Data API to clients entirely (defense in depth on top of RLS).
--
-- To later expose a specific table for direct authenticated reads:
--     grant select on <table> to authenticated;   -- rows still gated by its RLS policy
--
-- service_role grants are intentionally left intact (backend needs them).

do $$
declare r record;
begin
  -- 1) FORCE RLS so even a table owner is subject to policies (service_role still
  --    bypasses via its BYPASSRLS attribute — that is the backend path).
  for r in
    select tablename from pg_tables where schemaname = 'public'
  loop
    execute format('alter table public.%I force row level security', r.tablename);
    -- 2) Revoke all client access; clients must go through the backend API.
    execute format('revoke all on public.%I from anon, authenticated', r.tablename);
  end loop;

  -- 3) Views (redemptions_view, merchant_active_passes) are also Data-API exposed.
  for r in
    select viewname as relname from pg_views where schemaname = 'public'
  loop
    execute format('revoke all on public.%I from anon, authenticated', r.relname);
  end loop;

  -- 4) Sequences (identity/serials) — no client access.
  for r in
    select sequencename as relname from pg_sequences where schemaname = 'public'
  loop
    execute format('revoke all on public.%I from anon, authenticated', r.relname);
  end loop;
end $$;

-- 5) Deny future default privileges to clients in public (belt-and-suspenders for any
--    table a later migration forgets to lock).
alter default privileges in schema public revoke all on tables from anon, authenticated;
alter default privileges in schema public revoke all on sequences from anon, authenticated;

-- 6) Keep RLS *enabled* on every table (0002 did this) so that if a GRANT is ever added,
--    access is still row-gated by policy rather than wide open.
