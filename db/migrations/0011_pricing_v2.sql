-- conpass — pricing v2 (business model v2.0, Phase 13, D17)
--
-- Prices, inclusions and limits now live in ONE place: the Python plan catalog
-- (conpass_common/plans.py), served to the PWA by GET /plans. The DB keeps only the
-- tier. The old trigger copied per-tier limits and an MRR figure onto each row, and only
-- when they were NULL — so a plan change never updated them (an upgraded merchant kept
-- the old limits). Those copies are dropped rather than corrected: a second copy of the
-- catalog is exactly what drifted.
--
-- Enterprise / white-label is quoted off-catalog, so it stops being a tier. There are no
-- customers yet; any enterprise row (none expected) is folded into pro.

drop trigger if exists subscriptions_tier_defaults on subscriptions;
drop function if exists apply_tier_defaults();

alter table subscriptions
    drop column if exists mrr_usd,
    drop column if exists active_pass_limit,
    drop column if exists program_limit,
    drop column if exists operation_user_limit;

-- Rebuild the enum without 'enterprise' (Postgres cannot drop an enum value in place).
update subscriptions set tier = 'pro' where tier = 'enterprise';
alter table subscriptions alter column tier drop default;
alter type tier rename to tier_v1;
create type tier as enum ('starter', 'growth', 'pro');
alter table subscriptions alter column tier type tier using tier::text::tier;
alter table subscriptions alter column tier set default 'starter';
drop type tier_v1;

-- Billing metric = cards currently ACTIVE. The old view also required wallet_installed,
-- which nothing sets yet (the wallet-saved callback is backlogged), so it always read 0.
-- Same approximation program_metrics_view (0007) already makes.
create or replace view merchant_active_passes as
    select merchant_id, count(*) filter (where active) as active_pass_count
    from cards group by merchant_id;

notify pgrst, 'reload schema';
