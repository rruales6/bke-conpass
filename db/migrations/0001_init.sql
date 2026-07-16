-- conpass — core schema
-- Backend is the authority for balances; the wallet pass is display-only.
-- Multi-tenant: each merchant is a tenant. See 0002_rls.sql for isolation.

create extension if not exists pgcrypto;
create extension if not exists citext;

-- ---------------------------------------------------------------------------
-- Enums (mirror backend/contracts/openapi.yaml)
-- ---------------------------------------------------------------------------
create type tier as enum ('starter', 'growth', 'pro', 'enterprise');
create type program_type as enum ('loyalty_stamps', 'loyalty_points', 'membership_pass');
create type reward_mechanic as enum ('stamps', 'points');
create type membership_validity as enum ('monthly', 'quarterly', 'annual', 'per_event');
create type payment_status as enum ('pending', 'paid', 'overdue', 'suspended');
create type app_role as enum ('platform_admin', 'merchant_owner', 'operation_user', 'customer');
create type station as enum ('caja_1', 'caja_2', 'door_access');
create type transaction_kind as enum ('accrue_stamps', 'accrue_points', 'redeem', 'validate_access', 'birthday_issue');
create type benefit_type as enum ('discount', 'gift');
create type birthday_status as enum ('pending', 'issued', 'redeemed', 'expired');
create type wallet_provider as enum ('google', 'apple');

-- ---------------------------------------------------------------------------
-- Tenancy
-- ---------------------------------------------------------------------------
create table merchants (
    id             uuid primary key default gen_random_uuid(),
    business_name  text not null,
    ruc            text,
    category       text,
    city           text,
    contact_name   text,
    contact_email  citext,
    logo_storage_key text,
    created_at     timestamptz not null default now()
);

create table subscriptions (
    merchant_id          uuid primary key references merchants(id) on delete cascade,
    tier                 tier not null default 'starter',
    payment_status       payment_status not null default 'pending',
    mrr_usd              numeric(10,2) not null default 0,
    active_pass_limit    integer,           -- tier cap; null = unlimited
    program_limit        integer,
    operation_user_limit integer,
    next_charge_at       date,
    last_payment_at      date,
    updated_at           timestamptz not null default now()
);

-- Authenticated users (owners, operators, platform admins). Keyed to Supabase auth.users.
-- Customers are NOT here (data minimization — they enroll without accounts).
create table profiles (
    user_id     uuid primary key,          -- = auth.uid()
    email       citext,
    merchant_id uuid references merchants(id) on delete cascade,  -- null for platform_admin
    role        app_role not null,
    station     station,                    -- for operation_user
    name        text,
    created_at  timestamptz not null default now()
);
create index on profiles (merchant_id);

-- ---------------------------------------------------------------------------
-- Programs
-- ---------------------------------------------------------------------------
create table programs (
    id                 uuid primary key default gen_random_uuid(),
    merchant_id        uuid not null references merchants(id) on delete cascade,
    type               program_type not null,
    name               text not null,
    mechanic           reward_mechanic,
    stamps_for_reward  integer,
    points_for_reward  integer,
    points_per_dollar  numeric(10,2),
    reward             text,
    membership_validity membership_validity,
    membership_includes text,
    welcome_bonus      integer not null default 0,
    expiry_days        integer,
    color              text,
    icon_storage_key   text,
    background_storage_key text,
    wallets            text[] not null default '{}',  -- {'google','apple'}
    active             boolean not null default true,
    created_at         timestamptz not null default now()
);
create index on programs (merchant_id);

-- ---------------------------------------------------------------------------
-- Customers & cards
-- ---------------------------------------------------------------------------
create table customers (
    id          uuid primary key default gen_random_uuid(),
    merchant_id uuid not null references merchants(id) on delete cascade,
    full_name   text,
    email       citext,
    phone       text,
    birthday    date,
    consent     boolean not null default false,
    created_at  timestamptz not null default now()
);
create index on customers (merchant_id);

create table cards (
    id                uuid primary key default gen_random_uuid(),
    program_id        uuid not null references programs(id) on delete cascade,
    merchant_id       uuid not null references merchants(id) on delete cascade,
    customer_id       uuid references customers(id) on delete set null,
    type              program_type not null,
    opaque_token      text not null unique,   -- carried in the QR
    -- Backend-authoritative balance:
    stamps            integer not null default 0,
    points            integer not null default 0,
    rewards_available integer not null default 0,
    membership_active_until date,
    holder_name       text,
    active            boolean not null default true,
    wallet_installed  boolean not null default false,  -- drives active-pass billing metric
    dedupe_key        text,                   -- optional per-device enrollment dedupe
    created_at        timestamptz not null default now()
);
create index on cards (program_id);
create index on cards (merchant_id);
create index on cards (customer_id);
-- Dedupe repeated enrollments from the same device for the same program.
create unique index cards_program_dedupe_uidx
    on cards (program_id, dedupe_key) where dedupe_key is not null;

-- ---------------------------------------------------------------------------
-- Transactions & idempotency
-- ---------------------------------------------------------------------------
create table transactions (
    id                uuid primary key default gen_random_uuid(),
    card_id           uuid not null references cards(id) on delete cascade,
    merchant_id       uuid not null references merchants(id) on delete cascade,
    kind              transaction_kind not null,
    stamps_delta      integer not null default 0,
    points_delta      integer not null default 0,
    operation_user_id uuid references profiles(user_id),
    idempotency_key   uuid,
    occurred_at       timestamptz,            -- client event time (offline replay)
    created_at        timestamptz not null default now()
);
create index on transactions (card_id);
create index on transactions (merchant_id, created_at);

-- Idempotency store: makes mutating ops safe to replay after offline queueing.
-- Stores the serialized response so a replay returns the identical result.
create table idempotency_records (
    idempotency_key uuid primary key,
    endpoint        text not null,
    request_hash    text not null,
    response_status integer not null,
    response_body   jsonb not null,
    created_at      timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Birthday automation & issued birthday cards
-- ---------------------------------------------------------------------------
create table birthday_automations (
    program_id      uuid primary key references programs(id) on delete cascade,
    enabled         boolean not null default false,
    benefit_type    benefit_type not null,
    discount_percent integer,
    gift            text,
    validity_days   integer not null default 7,
    updated_at      timestamptz not null default now()
);

create table birthday_cards (
    id           uuid primary key default gen_random_uuid(),
    program_id   uuid not null references programs(id) on delete cascade,
    merchant_id  uuid not null references merchants(id) on delete cascade,
    customer_id  uuid not null references customers(id) on delete cascade,
    code         text not null unique,       -- e.g. CUMPLE-3F9A
    benefit      text,
    valid_until  date,
    status       birthday_status not null default 'pending',
    created_at   timestamptz not null default now()
);
create index on birthday_cards (merchant_id);

-- ---------------------------------------------------------------------------
-- Wallet objects — one row per (card, provider). Keeps the WalletProvider
-- abstraction persistent and Apple-ready (no schema change needed for Apple).
-- ---------------------------------------------------------------------------
create table wallet_objects (
    id                 uuid primary key default gen_random_uuid(),
    card_id            uuid not null references cards(id) on delete cascade,
    provider           wallet_provider not null,
    provider_class_id  text,
    provider_object_id text,
    state              text not null default 'pending',  -- pending|active|revoked
    last_synced_at     timestamptz,
    created_at         timestamptz not null default now(),
    unique (card_id, provider)
);
