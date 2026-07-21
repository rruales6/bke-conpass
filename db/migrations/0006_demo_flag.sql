-- conpass — demo tenant flag
--
-- Marks a merchant (and, by the tenant relationship, its programs / profiles / cards)
-- as demo data. Powers the public self-serve sandbox: `GET /demo` discovers the most
-- recent demo merchant so the unauthenticated /demo journey can silently sign in, and
-- the reset job can safely wipe only demo activity. Backend-only visibility — merchants
-- has RLS forced with no client grants, so only the service_role (the backend) reads it.

alter table merchants add column if not exists is_demo boolean not null default false;

-- Supports the "most recent demo merchant" lookup cheaply.
create index if not exists merchants_is_demo_idx
    on merchants (created_at desc) where is_demo;

-- Ask PostgREST (the Data API) to reload its schema cache so the new column is visible.
notify pgrst, 'reload schema';
