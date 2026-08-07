-- conpass — Phase 10: platform payment settings + manual-transfer proof
--
-- Función 02 (B2B signup) shows the account a merchant transfers into — bank, account
-- number, beneficiary and the DEUNA QR — and requires a receipt before the account can
-- be activated. Those details must be editable from Función 04 (platform admin) without
-- a redeploy, so they live in the DB rather than in config.
--
-- `platform_payment_settings` is a SINGLETON: the boolean primary key with a
-- `check (id)` constraint admits exactly one row (id = true), so an accidental second
-- insert fails loudly instead of silently forking the details shown on signup.

create table platform_payment_settings (
    id                 boolean primary key default true check (id),
    bank_name          text,
    account_type       text check (account_type in ('savings', 'checking')),
    account_number     text,
    beneficiary_name   text,
    beneficiary_tax_id text,
    contact_email      text,
    instructions       text,
    qr_storage_key     text,          -- key in the PUBLIC program-assets bucket
    updated_at         timestamptz not null default now()
);

insert into platform_payment_settings (id) values (true) on conflict do nothing;

-- Receipt uploaded at signup, held in the PRIVATE payment-proofs bucket. Only
-- platform-admin reads it back (presigned GET) before marking the subscription paid.
alter table subscriptions
    add column if not exists payment_proof_key         text,
    add column if not exists payment_proof_uploaded_at timestamptz;

-- Same Data-API safety strategy as migration 0004/0005: RLS forced, clients locked out,
-- backend (service_role) granted explicit DML. The table carries no tenant column — it is
-- platform-level — so there are no per-tenant policies to write; access is backend-only.
alter table platform_payment_settings enable row level security;
alter table platform_payment_settings force row level security;
revoke all on platform_payment_settings from anon, authenticated;
grant select, insert, update, delete on platform_payment_settings to service_role;

-- New columns on an existing table need PostgREST to reload before the Data API sees them.
notify pgrst, 'reload schema';
