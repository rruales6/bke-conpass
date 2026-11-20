-- conpass — Phase 18: console redesign + business model v3.0 (D21)
--
-- Brief: docs/design/PHASE-18-CONSOLE.md (docs repo). Owner decisions O1–O4:
--   O1  business model v3.0 in full. Prices stay in conpass_common/plans.py (D17); the DB
--       keeps the billing period, the annual price lock, the payment ledger and plan-change
--       requests.
--   O2  a card unused for 90 days PAUSES. It stops being billed and notified, KEEPS its
--       balance, and a scan reactivates it. Balance expiry stops zeroing balances.
--   O3  cashier undo, while the "Listo" screen still shows. A `reversal` transaction
--       points at the original, and the original is stamped `reversed_at` so every
--       activity / metric read can skip both.
--   O4  plan change = merchant request + admin confirmation (upgrade), or a scheduled
--       downgrade on the next charge date. Applied by the notifier's daily job.
--
-- NOTE: the whole file runs as one implicit transaction, so enum values added here
-- (payment_status 'rejected', transaction_kind 'reversal' / 'birthday_redeem') are NOT
-- used in this file. Only the application code uses them, after commit.

-- --- Enums ------------------------------------------------------------------------------
alter type payment_status   add value if not exists 'rejected';
alter type transaction_kind add value if not exists 'reversal';
alter type transaction_kind add value if not exists 'birthday_redeem';

-- --- Platform settings (singleton) ------------------------------------------------------
-- pricing_phase: 'launch' charges the launch price; 'list' (once Apple Wallet ships,
-- announced 30 days ahead) charges the list price. Annual terms keep their locked price.
create table if not exists platform_settings (
    id                    boolean primary key default true check (id),
    pricing_phase         text not null default 'launch'
                          check (pricing_phase in ('launch', 'list')),
    list_price_effective_at date,       -- announced switch date (merchants get 30 days' notice)
    updated_at            timestamptz not null default now()
);
insert into platform_settings (id) values (true) on conflict (id) do nothing;

-- --- Merchants / subscriptions ----------------------------------------------------------
alter table merchants
    add column if not exists contact_phone text;          -- WhatsApp, optional at signup

alter table subscriptions
    add column if not exists billing_period text not null default 'monthly'
        check (billing_period in ('monthly', 'annual')),
    -- Price paid for the current annual term (IVA incl.). It overrides the catalog until
    -- next_charge_at (renewal), so an annual merchant keeps its price when the list
    -- price starts. NULL = follow the catalog.
    add column if not exists locked_price_usd numeric(10,2)
        check (locked_price_usd is null or locked_price_usd >= 0),
    add column if not exists rejection_reason text;       -- set with payment_status 'rejected'

-- Payment ledger. Replaces the single subscriptions.payment_proof_key; that column stays
-- readable for one release and is backfilled below.
create table if not exists payments (
    id                uuid primary key default gen_random_uuid(),
    merchant_id       uuid not null references merchants(id) on delete cascade,
    kind              text not null check (kind in ('signup', 'renewal', 'plan_change', 'overage', 'other')),
    tier              tier,
    billing_period    text check (billing_period in ('monthly', 'annual')),
    amount_usd        numeric(10,2) not null check (amount_usd >= 0),
    method            text not null check (method in ('manual_transfer', 'deuna', 'card', 'cash', 'other')),
    status            text not null default 'pending'
                      check (status in ('pending', 'confirmed', 'rejected')),
    proof_storage_key text,                 -- private payment-proofs bucket
    period_start      date,
    period_end        date,
    paid_at           date,                 -- when the money arrived (admin-entered)
    note              text check (note is null or char_length(note) <= 500),
    rejection_reason  text check (rejection_reason is null or char_length(rejection_reason) <= 500),
    confirmed_by      uuid,
    confirmed_at      timestamptz,
    created_at        timestamptz not null default now()
);
create index if not exists payments_merchant_created_idx on payments (merchant_id, created_at desc);
create index if not exists payments_pending_idx on payments (created_at) where status = 'pending';

insert into payments (merchant_id, kind, tier, billing_period, amount_usd, method, status,
                      proof_storage_key, created_at)
select s.merchant_id, 'signup', s.tier, 'monthly', 0, 'manual_transfer',
       case when s.payment_status = 'paid' then 'confirmed' else 'pending' end,
       s.payment_proof_key, coalesce(s.payment_proof_uploaded_at, now())
from subscriptions s
where s.payment_proof_key is not null
  and not exists (select 1 from payments p where p.merchant_id = s.merchant_id);

-- Plan changes (O4). At most one open request per merchant.
--   upgrade:   awaiting_payment -> pending_review (proof uploaded) -> applied | rejected
--   downgrade: scheduled (effective_at = next charge date) -> applied | cancelled
create table if not exists plan_change_requests (
    id              uuid primary key default gen_random_uuid(),
    merchant_id     uuid not null references merchants(id) on delete cascade,
    from_tier       tier not null,
    to_tier         tier not null,
    from_period     text not null check (from_period in ('monthly', 'annual')),
    to_period       text not null check (to_period in ('monthly', 'annual')),
    direction       text not null check (direction in ('upgrade', 'downgrade')),
    status          text not null
                    check (status in ('awaiting_payment', 'pending_review', 'scheduled',
                                      'applied', 'cancelled', 'rejected')),
    amount_usd      numeric(10,2) check (amount_usd is null or amount_usd >= 0),
    payment_id      uuid references payments(id) on delete set null,
    effective_at    date,
    rejection_reason text,
    requested_by    uuid,
    resolved_by     uuid,
    created_at      timestamptz not null default now(),
    resolved_at     timestamptz,
    constraint plan_change_differs_chk check (from_tier <> to_tier or from_period <> to_period)
);
create unique index if not exists plan_change_one_open_uidx
    on plan_change_requests (merchant_id)
    where status in ('awaiting_payment', 'pending_review', 'scheduled');
create index if not exists plan_change_scheduled_idx
    on plan_change_requests (effective_at) where status = 'scheduled';

-- Activity log for the platform's merchant file (board 18). Written by the backend only.
create table if not exists merchant_activity (
    id            uuid primary key default gen_random_uuid(),
    merchant_id   uuid not null references merchants(id) on delete cascade,
    actor_user_id uuid,
    actor_role    text check (actor_role in ('platform_admin', 'merchant_owner', 'operation_user', 'system')),
    action        text not null check (char_length(action) between 1 and 60),
    details       jsonb not null default '{}'::jsonb,
    created_at    timestamptz not null default now()
);
create index if not exists merchant_activity_merchant_idx on merchant_activity (merchant_id, created_at desc);

-- --- Team -------------------------------------------------------------------------------
-- Operation users get a staff role. 'manager' (Pro) also sees reports for their
-- branches and manages cashiers there. NULL for non-operation profiles.
alter table profiles
    add column if not exists staff_role text check (staff_role in ('cashier', 'manager'));
update profiles set staff_role = 'cashier' where role = 'operation_user' and staff_role is null;

-- --- Password reset by code -------------------------------------------------------------
create table if not exists password_reset_codes (
    id          uuid primary key default gen_random_uuid(),
    email       citext not null,
    user_id     uuid not null,
    code_hash   text not null,              -- sha256(salt || code); never the code itself
    attempts    integer not null default 0 check (attempts between 0 and 10),
    expires_at  timestamptz not null,
    used_at     timestamptz,
    created_at  timestamptz not null default now()
);
create index if not exists password_reset_codes_email_idx on password_reset_codes (email, created_at desc);

-- --- Cards: pause, level, wallet saved --------------------------------------------------
-- paused_at: set by the notifier after 90 days without use (O2); cleared by a scan.
-- level: 1 + redemptions, persisted, never drops (model v3.0 decision 12). Only an undo
-- of the redemption that raised it takes it back. Meaningful for loyalty cards only.
-- wallet_installed (0001) now has a writer: the wallet poller marks a card saved once
-- Google reports the object has users. Never-saved cards are neither billed nor notified.
alter table cards
    add column if not exists paused_at timestamptz,
    add column if not exists level integer not null default 1 check (level >= 1),
    -- Last time the wallet poller asked Google whether this pass was saved (rotation key).
    add column if not exists wallet_checked_at timestamptz;

update cards set level = 1 + rewards_redeemed
 where type in ('loyalty_stamps', 'loyalty_points') and level <> 1 + rewards_redeemed;

create index if not exists cards_merchant_created_idx on cards (merchant_id, created_at);
create index if not exists cards_enrolled_location_idx
    on cards (enrolled_location_id) where enrolled_location_id is not null;
create index if not exists cards_unsaved_idx
    on cards (wallet_checked_at nulls first, created_at desc) where active and not wallet_installed;

-- --- Transactions: undo (O3) ------------------------------------------------------------
alter table transactions
    add column if not exists reverses_id uuid references transactions(id) on delete cascade,
    add column if not exists reversed_at timestamptz;
-- One reversal per original operation.
create unique index if not exists transactions_reverses_uidx
    on transactions (reverses_id) where reverses_id is not null;

-- --- Customers: birthday without a year -------------------------------------------------
alter table customers
    add column if not exists birthday_day   smallint check (birthday_day between 1 and 31),
    add column if not exists birthday_month smallint check (birthday_month between 1 and 12);
update customers
   set birthday_day = extract(day from birthday)::smallint,
       birthday_month = extract(month from birthday)::smallint
 where birthday is not null and birthday_day is null;
-- Data minimisation (D15): the year is never needed, so the full date goes.
alter table customers drop column if exists birthday;
create index if not exists customers_birthday_idx
    on customers (birthday_month, birthday_day) where birthday_month is not null;

-- Birthday rewards: one per customer per year, attached to their most recently used
-- active loyalty card, redeemed at the till like a reward (kind 'birthday_redeem').
alter table birthday_cards
    add column if not exists card_id uuid references cards(id) on delete cascade,
    add column if not exists year    smallint,
    add column if not exists redeemed_at timestamptz;
create unique index if not exists birthday_cards_customer_year_uidx
    on birthday_cards (customer_id, year) where year is not null;
create index if not exists birthday_cards_card_idx on birthday_cards (card_id) where card_id is not null;

-- --- Notification settings v3.0 ---------------------------------------------------------
alter table merchant_notification_settings
    -- Pro: the merchant's own wording per automation. Keys: ready, near, inactive,
    -- level_up, birthday. Missing key = the fixed template.
    add column if not exists custom_texts jsonb not null default '{}'::jsonb,
    -- Pro: up to 3 reminders by days without a visit, each with its own text. While on,
    -- it supersedes the single "Te extrañamos" reminder. Stops when the customer returns.
    add column if not exists sequence_on    boolean not null default false,
    add column if not exists sequence_steps jsonb not null
        default '[{"day":30,"text":""},{"day":45,"text":""},{"day":60,"text":""}]'::jsonb,
    add column if not exists level_up_on    boolean not null default false,   -- Pro
    add column if not exists birthday_on    boolean not null default false,   -- Pro
    add column if not exists birthday_benefit text not null default 'gift'
        check (birthday_benefit in ('gift', 'discount')),
    add column if not exists birthday_gift  text check (birthday_gift is null or char_length(birthday_gift) <= 80),
    add column if not exists birthday_discount_percent integer
        check (birthday_discount_percent is null or birthday_discount_percent between 1 and 100),
    add column if not exists birthday_validity_days integer not null default 7
        check (birthday_validity_days between 1 and 30);

-- "Te extrañamos" is on every plan now (v3.0 decision 5) and is sent at N days without a
-- visit, which must come before the 90-day pause.
alter table merchant_notification_settings
    drop constraint if exists merchant_notification_settings_inactive_days_check;
alter table merchant_notification_settings
    add constraint merchant_notification_settings_inactive_days_check
        check (inactive_days between 7 and 89);
alter table merchant_notification_settings alter column inactive_days set default 30;
alter table merchant_notification_settings alter column inactive_on set default true;

-- The balance notice is off by default (v3.0 decision 2), for existing merchants too.
alter table merchant_notification_settings alter column balance_on set default false;
update merchant_notification_settings set balance_on = false where balance_on;

-- O2: balances never expire any more. Milestones are not part of the v3.0 automation set.
-- The columns stay, inert, so the change is reversible.
alter table merchant_notification_settings alter column milestone_on set default false;
alter table merchant_notification_settings alter column expiry_on set default false;
update merchant_notification_settings
   set balance_expiry_days = null, expiry_on = false, milestone_on = false
 where balance_expiry_days is not null or expiry_on or milestone_on;

alter table notifications drop constraint if exists notifications_type_check;
alter table notifications add constraint notifications_type_check
    check (type in ('general', 'event', 'flash', 'balance', 'ready', 'near', 'milestone',
                    'expiry', 'inactive', 'sequence', 'level_up', 'birthday'));

-- --- Views ------------------------------------------------------------------------------
-- Effective operations: a reversed operation and its reversal both drop out.
create or replace view effective_transactions as
    select t.* from transactions t
    where t.reversed_at is null and t.reverses_id is null;

create or replace view card_last_activity as
    select c.id as card_id, c.merchant_id,
           greatest(c.created_at,
                    coalesce(max(t.created_at) filter (
                        where t.kind in ('accrue_stamps', 'accrue_points', 'redeem')
                          and t.reversed_at is null and t.reverses_id is null),
                        c.created_at)) as last_at
    from cards c
    left join transactions t on t.card_id = c.id
    group by c.id, c.merchant_id, c.created_at;

-- Notifiable: as 0015, minus paused and never-saved cards; level is the persisted one.
create or replace view notifiable_cards as
    select c.id as card_id, c.merchant_id, c.program_id, p.type as program_type,
           c.stamps, c.points, c.rewards_available, c.rewards_redeemed,
           p.stamps_for_reward, p.points_for_reward, c.enrolled_location_id,
           case when p.type in ('loyalty_stamps', 'loyalty_points') then c.level end as level
    from cards c
    join programs p on p.id = c.program_id
    where c.active and p.active and c.paused_at is null and c.wallet_installed
      and (p.type in ('loyalty_stamps', 'loyalty_points')
           or (p.type = 'seasonal_discount'
               and (now() at time zone 'America/Guayaquil')::date
                   between p.valid_from and p.valid_until));

-- Billable active cards (model v3.0 §4): active, not paused, saved to a wallet. Discount
-- cards are never billed.
create or replace view merchant_active_passes as
    select c.merchant_id,
           count(*) filter (
               where c.active and c.paused_at is null and c.wallet_installed
                 and p.type <> 'seasonal_discount'
           ) as active_pass_count
    from cards c
    join programs p on p.id = c.program_id
    group by c.merchant_id;

-- --- Lock-down (0004/0005 pattern) ------------------------------------------------------
do $$
declare t text;
begin
    foreach t in array array['platform_settings', 'payments', 'plan_change_requests',
                             'merchant_activity', 'password_reset_codes']
    loop
        execute format('alter table %I enable row level security', t);
        execute format('alter table %I force row level security', t);
        execute format('revoke all on %I from anon, authenticated', t);
        execute format('grant select, insert, update, delete on %I to service_role', t);
    end loop;
end $$;

revoke all on effective_transactions, card_last_activity, notifiable_cards, merchant_active_passes
    from anon, authenticated;
grant select on effective_transactions, card_last_activity, notifiable_cards, merchant_active_passes
    to service_role;

notify pgrst, 'reload schema';
