-- conpass — Row Level Security
-- Tenant isolation: a user sees only their merchant's rows; platform_admin sees all.
-- Backend Lambdas mutate with the service_role key (bypasses RLS); these policies
-- protect any direct-from-client (anon/authenticated) access and are defense-in-depth.

-- --- Helper functions (SECURITY DEFINER, read profiles for the current auth.uid) ---
create or replace function auth_role() returns app_role
    language sql stable security definer set search_path = public as $$
    select role from profiles where user_id = auth.uid()
$$;

create or replace function auth_merchant_id() returns uuid
    language sql stable security definer set search_path = public as $$
    select merchant_id from profiles where user_id = auth.uid()
$$;

create or replace function is_platform_admin() returns boolean
    language sql stable security definer set search_path = public as $$
    select coalesce((select role = 'platform_admin' from profiles where user_id = auth.uid()), false)
$$;

-- Convenience: true if the row's merchant belongs to the caller (or caller is admin).
create or replace function owns_merchant(m uuid) returns boolean
    language sql stable security definer set search_path = public as $$
    select is_platform_admin() or m = auth_merchant_id()
$$;

-- --- Enable RLS ---
alter table merchants            enable row level security;
alter table subscriptions        enable row level security;
alter table profiles             enable row level security;
alter table programs             enable row level security;
alter table customers            enable row level security;
alter table cards                enable row level security;
alter table transactions         enable row level security;
alter table birthday_automations enable row level security;
alter table birthday_cards       enable row level security;
alter table wallet_objects       enable row level security;
-- idempotency_records is backend-only (service_role); RLS on, no policies = deny all clients.
alter table idempotency_records  enable row level security;

-- --- Policies (read scope for authenticated clients; writes go through the backend) ---
create policy merchants_read on merchants for select
    using (owns_merchant(id));

create policy subscriptions_read on subscriptions for select
    using (owns_merchant(merchant_id));

create policy profiles_self_or_tenant on profiles for select
    using (user_id = auth.uid() or owns_merchant(merchant_id) or is_platform_admin());

create policy programs_tenant on programs for select
    using (owns_merchant(merchant_id));

create policy customers_tenant on customers for select
    using (owns_merchant(merchant_id));

create policy cards_tenant on cards for select
    using (owns_merchant(merchant_id));

create policy transactions_tenant on transactions for select
    using (owns_merchant(merchant_id));

create policy birthday_automations_tenant on birthday_automations for select
    using (owns_merchant((select merchant_id from programs p where p.id = program_id)));

create policy birthday_cards_tenant on birthday_cards for select
    using (owns_merchant(merchant_id));

create policy wallet_objects_tenant on wallet_objects for select
    using (owns_merchant((select merchant_id from cards c where c.id = card_id)));

-- Platform-admin gets full read across every tenant table via owns_merchant()/is_platform_admin().
-- Note: a merchant_owner can be granted narrow write policies later; for now all writes
-- are performed by the backend service role, which bypasses RLS entirely.
