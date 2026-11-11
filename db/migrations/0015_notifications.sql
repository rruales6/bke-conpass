-- conpass — wallet notifications, locales and balance expiry (Phase 16)
--
-- Design: docs/design/NOTIFICATIONS.md (docs repo). In short:
--   * merchant_locations      the merchant's stores (name + lat/lng). Up to 10 ACTIVE ones
--                             go to every program's Google class as `merchantLocations`
--                             ("Estás cerca", Growth+). Also what a cashier picks at scan.
--   * operation_user_locations  which locales each cashier may work at (owner decision).
--   * transactions.location_id  where a scan happened; cards.enrolled_location_id where
--                             the customer signed up (enrol link `?local=`).
--   * merchant_notification_settings  automations on/off + thresholds + the merchant-wide
--                             balance-expiry window (owner: expiry is by INACTIVITY and is
--                             a general merchant parameter). Missing row = defaults.
--   * platform_notification_rules  singleton set by platform-admin: max notifications per
--                             card per day (1..3 — Google allows 3/24h) + quiet hours.
--   * notifications           one row per manual send, and one per (automation, day).
--   * notification_deliveries the outbox: one row per card. The dispatcher Lambda claims
--                             due rows, applies quiet hours + the daily cap, and calls the
--                             wallet. Sent rows are also what the back of the pass shows
--                             (the wallet object is PUT whole, so messages must be
--                             re-derived from here on every update) and what is billed.
--   * balance_expirations     audit of balances zeroed by inactivity. Deliberately NOT a
--                             transaction kind: every "last activity" calculation (metrics,
--                             abandonment, inactivity) reads transactions, and an expiry
--                             must not count as the customer coming back.

-- --- Locales ---------------------------------------------------------------------------
create table if not exists merchant_locations (
    id          uuid primary key default gen_random_uuid(),
    merchant_id uuid not null references merchants(id) on delete cascade,
    name        text not null check (char_length(btrim(name)) between 1 and 80),
    latitude    numeric(9,6) not null check (latitude between -90 and 90),
    longitude   numeric(9,6) not null check (longitude between -180 and 180),
    active      boolean not null default true,
    created_at  timestamptz not null default now()
);
create index if not exists merchant_locations_merchant_idx on merchant_locations (merchant_id);

create table if not exists operation_user_locations (
    user_id     uuid not null references profiles(user_id) on delete cascade,
    location_id uuid not null references merchant_locations(id) on delete cascade,
    primary key (user_id, location_id)
);
create index if not exists operation_user_locations_location_idx
    on operation_user_locations (location_id);

alter table transactions
    add column if not exists location_id uuid references merchant_locations(id) on delete set null;
create index if not exists transactions_location_idx
    on transactions (location_id) where location_id is not null;
-- "last activity" lookups per card (inactivity, expiry, visits after a send).
create index if not exists transactions_card_created_idx on transactions (card_id, created_at);

alter table cards
    add column if not exists enrolled_location_id uuid
        references merchant_locations(id) on delete set null;

-- --- Settings --------------------------------------------------------------------------
create table if not exists merchant_notification_settings (
    merchant_id          uuid primary key references merchants(id) on delete cascade,
    balance_on           boolean not null default true,
    ready_on             boolean not null default true,
    near_on              boolean not null default true,
    near_stamps          integer not null default 1  check (near_stamps between 1 and 4),
    near_points          integer not null default 10 check (near_points between 5 and 50
                                                            and near_points % 5 = 0),
    milestone_on         boolean not null default true,
    milestone_every      integer not null default 10 check (milestone_every between 5 and 50
                                                            and milestone_every % 5 = 0),
    expiry_on            boolean not null default true,
    expiry_warn_days     integer not null default 7  check (expiry_warn_days between 1 and 30),
    inactive_on          boolean not null default false,
    inactive_days        integer not null default 28 check (inactive_days between 7 and 90
                                                            and inactive_days % 7 = 0),
    -- Merchant-wide: stamps/points expire after this many days without a visit.
    -- NULL = balances never expire (the default; nothing changes until the owner opts in).
    balance_expiry_days  integer check (balance_expiry_days is null
                                        or balance_expiry_days between 7 and 365),
    card_locations_enrolled boolean not null default true,
    card_locations_top      boolean not null default false,
    updated_at           timestamptz not null default now(),
    constraint expiry_warn_inside_window_chk
        check (balance_expiry_days is null or expiry_warn_days < balance_expiry_days)
);

create table if not exists platform_notification_rules (
    id                  boolean primary key default true check (id),
    cap_per_day         integer not null default 2 check (cap_per_day between 1 and 3),
    quiet_hours_enabled boolean not null default true,
    updated_at          timestamptz not null default now()
);
insert into platform_notification_rules (id) values (true) on conflict (id) do nothing;

-- --- Notifications + outbox ------------------------------------------------------------
create table if not exists notifications (
    id           uuid primary key default gen_random_uuid(),
    merchant_id  uuid not null references merchants(id) on delete cascade,
    source       text not null check (source in ('manual', 'automation')),
    type         text not null check (type in ('general', 'event', 'flash',
                                               'balance', 'ready', 'near', 'milestone',
                                               'expiry', 'inactive')),
    audience     text not null check (audience in ('all', 'near', 'inactive', 'level',
                                                   'location', 'automatic')),
    level_min    integer check (level_min is null or level_min between 2 and 50),
    location_id  uuid references merchant_locations(id) on delete set null,
    title        text check (title is null or char_length(title) <= 60),
    body         text not null check (char_length(body) between 1 and 300),
    url          text check (url is null or url ~ '^https://'),
    event_at     timestamptz,
    flash_hours  integer check (flash_hours is null or flash_hours between 1 and 72),
    status       text not null default 'queued'
                 check (status in ('scheduled', 'queued', 'sent', 'cancelled')),
    scheduled_at timestamptz,
    day          date,          -- automations: one row per (merchant, type, Ecuador day)
    recipients   integer not null default 0,
    created_by   uuid,
    created_at   timestamptz not null default now(),
    sent_at      timestamptz,
    constraint notifications_source_shape_chk check (
        (source = 'manual' and type in ('general', 'event', 'flash') and day is null)
        or (source = 'automation' and audience = 'automatic' and day is not null))
);
create unique index if not exists notifications_automation_day_uidx
    on notifications (merchant_id, type, day) where source = 'automation';
create index if not exists notifications_merchant_created_idx
    on notifications (merchant_id, created_at desc);
create index if not exists notifications_scheduled_idx
    on notifications (scheduled_at) where status = 'scheduled';

create table if not exists notification_deliveries (
    id              uuid primary key default gen_random_uuid(),
    notification_id uuid not null references notifications(id) on delete cascade,
    merchant_id     uuid not null references merchants(id) on delete cascade,
    card_id         uuid not null references cards(id) on delete cascade,
    header          text not null,
    body            text not null,
    url             text,
    display_until   timestamptz,          -- flash / event: Google hides the message after
    status          text not null default 'pending'
                    check (status in ('pending', 'sending', 'sent', 'sent_silent', 'failed',
                                      'skipped', 'superseded', 'cancelled')),
    not_before      timestamptz not null default now(),
    attempts        integer not null default 0,
    claimed_at      timestamptz,
    sent_at         timestamptz,
    error           text,
    dedupe_key      text unique,          -- time-based automations: card + trigger epoch
    created_at      timestamptz not null default now()
);
create index if not exists notification_deliveries_due_idx
    on notification_deliveries (not_before) where status = 'pending';
create index if not exists notification_deliveries_card_sent_idx
    on notification_deliveries (card_id, sent_at) where sent_at is not null;
create index if not exists notification_deliveries_merchant_sent_idx
    on notification_deliveries (merchant_id, sent_at) where status = 'sent';
create index if not exists notification_deliveries_notification_idx
    on notification_deliveries (notification_id);

create table if not exists balance_expirations (
    id               uuid primary key default gen_random_uuid(),
    card_id          uuid not null references cards(id) on delete cascade,
    merchant_id      uuid not null references merchants(id) on delete cascade,
    stamps_expired   integer not null default 0,
    points_expired   integer not null default 0,
    last_activity_at timestamptz not null,
    expired_at       timestamptz not null default now()
);
create index if not exists balance_expirations_card_idx on balance_expirations (card_id);

-- --- Derived views ---------------------------------------------------------------------
-- When each card was last USED (accrual or redemption), enrolment if never.
create or replace view card_last_activity as
    select c.id as card_id, c.merchant_id,
           greatest(c.created_at,
                    coalesce(max(t.created_at) filter (
                        where t.kind in ('accrue_stamps', 'accrue_points', 'redeem')),
                        c.created_at)) as last_at
    from cards c
    left join transactions t on t.card_id = c.id
    group by c.id, c.merchant_id, c.created_at;

-- Cards a merchant can notify: active cards of active loyalty campaigns, plus seasonal
-- campaigns that are running today (Ecuador). Membership passes are parked (owner).
create or replace view notifiable_cards as
    select c.id as card_id, c.merchant_id, c.program_id, p.type as program_type,
           c.stamps, c.points, c.rewards_available, c.rewards_redeemed,
           p.stamps_for_reward, p.points_for_reward, c.enrolled_location_id,
           case when p.type in ('loyalty_stamps', 'loyalty_points')
                then 1 + c.rewards_available + c.rewards_redeemed end as level
    from cards c
    join programs p on p.id = c.program_id
    where c.active and p.active
      and (p.type in ('loyalty_stamps', 'loyalty_points')
           or (p.type = 'seasonal_discount'
               and (now() at time zone 'America/Guayaquil')::date
                   between p.valid_from and p.valid_until));

-- History row per notification: delivered = notify-sent cards; visits_48h = recipients
-- that came back (accrual or redemption) within 48 h of their own delivery.
create or replace view notification_history_view as
    select n.*,
           -- Automation batches grow all day, so their recipient count is the live count.
           case when n.source = 'automation' then coalesce(d.total, 0)
                else n.recipients end  as recipient_count,
           coalesce(d.delivered, 0)  as delivered,
           coalesce(d.visits_48h, 0) as visits_48h
    from notifications n
    left join lateral (
        select count(*) filter (where dd.status not in ('superseded', 'cancelled')) as total,
               count(*) filter (where dd.status in ('sent', 'sent_silent')) as delivered,
               count(*) filter (where dd.status in ('sent', 'sent_silent') and exists (
                   select 1 from transactions t
                   where t.card_id = dd.card_id
                     and t.kind in ('accrue_stamps', 'accrue_points', 'redeem')
                     and t.created_at > dd.sent_at
                     and t.created_at <= dd.sent_at + interval '48 hours')) as visits_48h
        from notification_deliveries dd
        where dd.notification_id = n.id
    ) d on true;

-- --- Functions (service_role only) -----------------------------------------------------
-- The audience of a manual send. `near` uses the merchant's own thresholds (the same
-- "umbral general" as the near-goal automation); `inactive` is fixed at 30 days.
create or replace function notification_audience(
    p_merchant uuid, p_audience text, p_level_min integer default null,
    p_location uuid default null)
returns table (card_id uuid)
language sql stable security invoker set search_path = public as $$
    with s as (
        select coalesce(max(near_stamps), 1) as near_stamps,
               coalesce(max(near_points), 10) as near_points
        from merchant_notification_settings where merchant_id = p_merchant
    )
    select nc.card_id
    from notifiable_cards nc, s
    where nc.merchant_id = p_merchant
      and case p_audience
          when 'all' then true
          when 'near' then
               (nc.program_type = 'loyalty_stamps' and nc.stamps_for_reward > 0
                and nc.stamps_for_reward - nc.stamps between 1 and s.near_stamps)
            or (nc.program_type = 'loyalty_points' and nc.points_for_reward > 0
                and nc.points_for_reward - nc.points between 1 and s.near_points)
          when 'inactive' then exists (
               select 1 from card_last_activity la
               where la.card_id = nc.card_id and la.last_at < now() - interval '30 days')
          when 'level' then nc.level >= coalesce(p_level_min, 2)
          when 'location' then nc.enrolled_location_id = p_location or exists (
               select 1 from transactions t
               where t.card_id = nc.card_id and t.location_id = p_location)
          else false
      end
$$;

-- Expand a manual notification into its outbox rows (one round trip from the Lambda).
-- Returns the recipient count and stamps it on the notification.
create or replace function enqueue_manual_notification(p_notification uuid)
returns integer
language plpgsql security invoker set search_path = public as $$
declare
    n notifications%rowtype;
    v_count integer;
begin
    select * into n from notifications where id = p_notification for update;
    if not found or n.source <> 'manual' or n.status not in ('queued', 'scheduled') then
        return 0;
    end if;
    insert into notification_deliveries
        (notification_id, merchant_id, card_id, header, body, url, display_until, not_before)
    select n.id, n.merchant_id, a.card_id,
           coalesce(nullif(btrim(n.title), ''), m.business_name), n.body, n.url,
           case when n.type = 'flash'
                    then coalesce(n.scheduled_at, now()) + make_interval(hours => n.flash_hours)
                when n.type = 'event' and n.event_at is not null
                    then n.event_at + interval '6 hours' end,
           coalesce(n.scheduled_at, now())
    from notification_audience(n.merchant_id, n.audience, n.level_min, n.location_id) a
    join merchants m on m.id = n.merchant_id;
    get diagnostics v_count = row_count;
    update notifications set recipients = v_count, status = 'queued' where id = n.id;
    return v_count;
end
$$;

-- Claim up to p_limit due outbox rows for one dispatcher run. SKIP LOCKED makes two
-- overlapping runs safe; rows stuck in 'sending' for 10 min (a crashed run) are retried.
create or replace function claim_notification_deliveries(p_limit integer)
returns setof notification_deliveries
language sql volatile security invoker set search_path = public as $$
    update notification_deliveries d
    set status = 'sending', attempts = d.attempts + 1, claimed_at = now()
    where d.id in (
        select id from notification_deliveries
        where (status = 'pending' and not_before <= now())
           or (status = 'sending' and claimed_at < now() - interval '10 minutes')
        order by not_before
        for update skip locked
        limit p_limit)
    returning d.*
$$;

-- --- Lock-down (0004/0005 pattern) -----------------------------------------------------
do $$
declare t text;
begin
    foreach t in array array['merchant_locations', 'operation_user_locations',
                             'merchant_notification_settings', 'platform_notification_rules',
                             'notifications', 'notification_deliveries', 'balance_expirations']
    loop
        execute format('alter table %I enable row level security', t);
        execute format('alter table %I force row level security', t);
        execute format('revoke all on %I from anon, authenticated', t);
        execute format('grant select, insert, update, delete on %I to service_role', t);
    end loop;
end $$;

revoke all on card_last_activity, notifiable_cards, notification_history_view
    from anon, authenticated;
grant select on card_last_activity, notifiable_cards, notification_history_view to service_role;

revoke all on function notification_audience(uuid, text, integer, uuid) from public, anon, authenticated;
revoke all on function enqueue_manual_notification(uuid) from public, anon, authenticated;
revoke all on function claim_notification_deliveries(integer) from public, anon, authenticated;
grant execute on function notification_audience(uuid, text, integer, uuid) to service_role;
grant execute on function enqueue_manual_notification(uuid) to service_role;
grant execute on function claim_notification_deliveries(integer) to service_role;

notify pgrst, 'reload schema';
