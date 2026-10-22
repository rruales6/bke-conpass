-- conpass — seasonal campaigns, part 2 of 2 (Phase 14)
--
-- Columns for `seasonal_discount` campaigns (NULL for every other kind):
--   occasion          the special date it celebrates ("Día de la Madre")
--   discount_percent  1..100; the free-text offer lives in the existing `reward` column
--   valid_from/until  inclusive dates, Ecuador time; the campaign is active between them
-- One use per card: a seasonal card is issued with rewards_available = 1 and applying
-- the discount is an ordinary `redeem` (0010's rewards_redeemed counts it).
--
-- Billing (owner decision): once a seasonal campaign has ENDED its cards stop counting
-- as active cards — the merchant is not billed for passes that can no longer be used.
-- Abandonment (0012) does not apply to seasonal campaigns, like membership passes.

alter table programs
    add column if not exists occasion         text,
    add column if not exists discount_percent integer
        check (discount_percent is null or discount_percent between 1 and 100),
    add column if not exists valid_from       date,
    add column if not exists valid_until      date;

alter table programs
    add constraint programs_valid_range_chk
        check (valid_from is null or valid_until is null or valid_until >= valid_from),
    add constraint programs_seasonal_shape_chk
        check (type <> 'seasonal_discount'
               or (valid_from is not null and valid_until is not null
                   and (discount_percent is not null or nullif(btrim(reward), '') is not null)));

-- Billable active cards: an ended seasonal campaign's cards no longer count.
create or replace view merchant_active_passes as
    select c.merchant_id,
           count(*) filter (
               where c.active
                 and not (p.type = 'seasonal_discount'
                          and p.valid_until < (now() at time zone 'America/Guayaquil')::date)
           ) as active_pass_count
    from cards c
    join programs p on p.id = c.program_id
    group by c.merchant_id;

create or replace view program_metrics_view as
with last_activity as (
    -- One row per active card: when it was last used (enrolment if never used).
    select c.id, c.program_id,
           greatest(c.created_at, coalesce(max(t.created_at), c.created_at)) as last_at
    from cards c
    left join transactions t on t.card_id = c.id
    where c.active
    group by c.id, c.program_id, c.created_at
),
card_agg as (
    select program_id,
           count(*)                                                    as total_cards,
           count(*) filter (where active)                              as active_cards,
           count(*) filter (where active
                            and created_at >= now() - interval '7 days') as installs_week
    from cards
    group by program_id
),
abandoned_agg as (
    select la.program_id, count(*) as abandoned
    from last_activity la
    join programs p on p.id = la.program_id
    where la.last_at < now() - make_interval(days => coalesce(p.inactivity_days, 90))
    group by la.program_id
),
visit_agg as (
    select c.program_id,
           count(*) filter (where t.kind in ('accrue_stamps', 'accrue_points')) as visits,
           count(*) filter (where t.kind = 'redeem')                            as redemptions
    from transactions t
    join cards c on c.id = t.card_id
    group by c.program_id
),
eligible_agg as (
    -- Active cards exactly one stamp/point step away from a reward.
    select c.program_id, count(*) as eligible
    from cards c
    join programs p on p.id = c.program_id
    where c.active
      and ((p.stamps_for_reward is not null and c.stamps = p.stamps_for_reward - 1)
        or (p.points_for_reward is not null and c.points = p.points_for_reward - 1))
    group by c.program_id
),
newcust as (
    -- Customers first enrolled in this program within 30d, and their accrual count.
    select c.program_id, c.customer_id,
           count(*) filter (where t.kind in ('accrue_stamps', 'accrue_points')) as visits
    from cards c
    left join transactions t on t.card_id = c.id
    where c.created_at >= now() - interval '30 days'
      and c.customer_id is not null
    group by c.program_id, c.customer_id
),
second_visit as (
    select program_id,
           count(*)                          as new_customers,
           count(*) filter (where visits >= 2) as returned
    from newcust
    group by program_id
)
select p.id                                              as program_id,
       coalesce(v.visits, 0)                             as visits,
       coalesce(v.redemptions, 0)                        as redemptions,
       coalesce(ca.active_cards, 0)                      as active_installed_passes,
       coalesce(ca.installs_week, 0)                     as installs_this_week,
       coalesce(e.eligible, 0)                           as eligible_for_reminder,
       case when p.type in ('membership_pass', 'seasonal_discount') then null
            when coalesce(ca.active_cards, 0) = 0 then 0
            else round(100.0 * coalesce(ab.abandoned, 0) / ca.active_cards, 1) end as churn_rate,
       case when coalesce(sv.new_customers, 0) = 0 then 0
            else round(100.0 * sv.returned / sv.new_customers, 1) end    as second_visit_rate_30d,
       case when p.type in ('membership_pass', 'seasonal_discount') then null
            else coalesce(p.inactivity_days, 90) end                     as inactivity_days
from programs p
left join card_agg      ca on ca.program_id = p.id
left join abandoned_agg ab on ab.program_id = p.id
left join visit_agg     v  on v.program_id  = p.id
left join eligible_agg  e  on e.program_id  = p.id
left join second_visit  sv on sv.program_id = p.id;

grant select on program_metrics_view to service_role;
grant select on merchant_active_passes to service_role;

notify pgrst, 'reload schema';
