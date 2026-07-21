-- conpass — program metrics view (Phase 6)
--
-- One row per program with the aggregates the merchant panel shows (ProgramMetrics).
-- Backend-only (service_role reads it; clients are revoked). Active installed passes are
-- APPROXIMATED as active cards for now — nothing flips cards.wallet_installed yet (the
-- Google Wallet "pass saved" callback is deferred), so we don't gate on it.

create or replace view program_metrics_view as
with card_agg as (
    select program_id,
           count(*)                                                    as total_cards,
           count(*) filter (where active)                              as active_cards,
           count(*) filter (where active
                            and created_at >= now() - interval '7 days') as installs_week,
           count(*) filter (where not active)                          as churned_cards
    from cards
    group by program_id
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
       case when coalesce(ca.total_cards, 0) = 0 then 0
            else round(100.0 * ca.churned_cards / ca.total_cards, 1) end as churn_rate,
       case when coalesce(sv.new_customers, 0) = 0 then 0
            else round(100.0 * sv.returned / sv.new_customers, 1) end    as second_visit_rate_30d
from programs p
left join card_agg    ca on ca.program_id = p.id
left join visit_agg   v  on v.program_id  = p.id
left join eligible_agg e  on e.program_id  = p.id
left join second_visit sv on sv.program_id = p.id;

grant select on program_metrics_view to service_role;

notify pgrst, 'reload schema';
