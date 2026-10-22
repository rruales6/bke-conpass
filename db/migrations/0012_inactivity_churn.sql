-- conpass — abandonment rate per campaign (Phase 14)
--
-- churn_rate used to be "% of cards with active = false", but nothing ever sets a card
-- inactive, so it was always 0. It now means ABANDONMENT: the share of active cards
-- with no activity (last accrual/redemption, or enrolment when there is none) in the
-- last N days, where N is the campaign's own `inactivity_days` (NULL = 90).
--
-- Only accumulation campaigns (loyalty_stamps / loyalty_points) have an abandonment
-- rate. A membership pass has no accumulation to abandon, so its churn_rate and
-- inactivity_days are NULL (the API omits them).

alter table programs
    add column if not exists inactivity_days integer
        check (inactivity_days is null or inactivity_days between 1 and 3650);

comment on column programs.inactivity_days is
    'Days without activity after which a card counts as abandoned (accumulation campaigns). NULL = 90.';

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
       case when p.type = 'membership_pass' then null
            when coalesce(ca.active_cards, 0) = 0 then 0
            else round(100.0 * coalesce(ab.abandoned, 0) / ca.active_cards, 1) end as churn_rate,
       case when coalesce(sv.new_customers, 0) = 0 then 0
            else round(100.0 * sv.returned / sv.new_customers, 1) end    as second_visit_rate_30d,
       case when p.type = 'membership_pass' then null
            else coalesce(p.inactivity_days, 90) end                     as inactivity_days
from programs p
left join card_agg      ca on ca.program_id = p.id
left join abandoned_agg ab on ab.program_id = p.id
left join visit_agg     v  on v.program_id  = p.id
left join eligible_agg  e  on e.program_id  = p.id
left join second_visit  sv on sv.program_id = p.id;

grant select on program_metrics_view to service_role;

notify pgrst, 'reload schema';
