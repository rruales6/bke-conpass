-- conpass — console reports (Phase 18, D21)
--
-- SQL aggregation behind GET /reports/summary, /reports/overview, /reports/export and the
-- extra ProgramMetrics fields. Every function is called by the reports/programs Lambdas
-- with the service client (client.rpc), never by a browser role.
--
-- Definitions (all days are Ecuador days, America/Guayaquil):
--   * Only EFFECTIVE operations count: effective_transactions (0017) drops undone
--     operations and their reversals.
--   * Visit      = accrue_stamps / accrue_points.   Redemption = redeem (not birthday_redeem).
--   * Returned   = the card has a visit or a redemption on a LATER Ecuador day than its
--     enrolment day, at most 30 days after it. The stamp given at sign-up shares the
--     enrolment day, so it never counts.
--   * Cohort     = LOYALTY cards (stamps / points) enrolled (cards.created_at) in
--     [p_from, p_to]. Discount cards are single use, so they would only lower the rate;
--     their results show in the per-campaign visits / redemptions (owner decision, Phase 18).
--   * p_locations (uuid[], NULL = no filter) is a manager's branch scope or an explicit
--     branch filter: cohort cards by cards.enrolled_location_id, activity by
--     transactions.location_id.
--   * p_program (NULL = every campaign) filters through cards.program_id.
--
-- Parameters p_from / p_to are inclusive Ecuador dates.

-- --- Helpers ----------------------------------------------------------------------------
create or replace function report_ec_date(p_ts timestamptz)
returns date
language sql stable security invoker set search_path = public as $$
    select (p_ts at time zone 'America/Guayaquil')::date
$$;

-- One row per cohort card, flagged when it came back within 30 days.
create or replace function report_cohort_cards(
    p_merchant uuid, p_program uuid, p_locations uuid[], p_from date, p_to date)
returns table (card_id uuid, program_id uuid, enrolled_location_id uuid,
               enrolled_on date, returned boolean)
language sql stable security invoker set search_path = public as $$
    select c.id, c.program_id, c.enrolled_location_id, report_ec_date(c.created_at),
           exists (
               select 1
               from effective_transactions t
               where t.card_id = c.id
                 and t.kind in ('accrue_stamps', 'accrue_points', 'redeem')
                 and report_ec_date(t.created_at) > report_ec_date(c.created_at)
                 and report_ec_date(t.created_at) <= report_ec_date(c.created_at) + 30
           )
    from cards c
    where c.merchant_id = p_merchant
      and c.type in ('loyalty_stamps', 'loyalty_points')
      and (p_program is null or c.program_id = p_program)
      and (p_locations is null or c.enrolled_location_id = any (p_locations))
      and report_ec_date(c.created_at) between p_from and p_to
$$;

-- --- Second visit -----------------------------------------------------------------------
-- One row: cards enrolled in the window and how many of them came back.
create or replace function report_second_visit(
    p_merchant uuid, p_program uuid, p_locations uuid[], p_from date, p_to date)
returns table (new_customers integer, returned integer)
language sql stable security invoker set search_path = public as $$
    select count(*)::integer,
           (count(*) filter (where k.returned))::integer
    from report_cohort_cards(p_merchant, p_program, p_locations, p_from, p_to) k
$$;

-- One row per week (Monday start, Ecuador) that has at least one new card. The caller
-- fills the empty weeks. p_from should be a Monday.
create or replace function report_cohort_weeks(
    p_merchant uuid, p_program uuid, p_locations uuid[], p_from date, p_to date)
returns table (week_start date, new_customers integer, returned integer)
language sql stable security invoker set search_path = public as $$
    select date_trunc('week', k.enrolled_on::timestamp)::date,
           count(*)::integer,
           (count(*) filter (where k.returned))::integer
    from report_cohort_cards(p_merchant, p_program, p_locations, p_from, p_to) k
    group by 1
    order by 1
$$;

-- --- Visits and redemptions -------------------------------------------------------------
create or replace function report_activity(
    p_merchant uuid, p_program uuid, p_locations uuid[], p_from date, p_to date)
returns table (visits integer, redemptions integer)
language sql stable security invoker set search_path = public as $$
    select (count(*) filter (where t.kind in ('accrue_stamps', 'accrue_points')))::integer,
           (count(*) filter (where t.kind = 'redeem'))::integer
    from effective_transactions t
    join cards c on c.id = t.card_id
    where t.merchant_id = p_merchant
      and (p_program is null or c.program_id = p_program)
      and (p_locations is null or t.location_id = any (p_locations))
      and report_ec_date(t.created_at) between p_from and p_to
$$;

-- --- Per campaign (membership passes are left out) ----------------------------------------
create or replace function report_campaigns(
    p_merchant uuid, p_program uuid, p_locations uuid[], p_from date, p_to date)
returns table (program_id uuid, name text, type text, visits integer, redemptions integer,
               new_customers integer, returned integer)
language sql stable security invoker set search_path = public as $$
    select p.id, p.name, p.type::text,
           coalesce(a.visits, 0), coalesce(a.redemptions, 0),
           coalesce(s.new_customers, 0), coalesce(s.returned, 0)
    from programs p
    left join lateral (
        select (count(*) filter (where t.kind in ('accrue_stamps', 'accrue_points')))::integer
                   as visits,
               (count(*) filter (where t.kind = 'redeem'))::integer as redemptions
        from effective_transactions t
        join cards c on c.id = t.card_id
        where c.program_id = p.id
          and t.merchant_id = p_merchant
          and (p_locations is null or t.location_id = any (p_locations))
          and report_ec_date(t.created_at) between p_from and p_to
    ) a on true
    left join lateral (
        select count(*)::integer as new_customers,
               (count(*) filter (where k.returned))::integer as returned
        from report_cohort_cards(p_merchant, p.id, p_locations, p_from, p_to) k
    ) s on true
    where p.merchant_id = p_merchant
      and p.type <> 'membership_pass'
      and (p_program is null or p.id = p_program)
    order by p.created_at, p.id
$$;

-- --- Per level: ACTIVE loyalty cards by their persisted level -----------------------------
create or replace function report_levels(
    p_merchant uuid, p_program uuid, p_locations uuid[])
returns table (level integer, count integer)
language sql stable security invoker set search_path = public as $$
    select c.level, count(*)::integer
    from cards c
    where c.merchant_id = p_merchant
      and c.active
      and c.type in ('loyalty_stamps', 'loyalty_points')
      and (p_program is null or c.program_id = p_program)
      and (p_locations is null or c.enrolled_location_id = any (p_locations))
    group by c.level
    order by c.level
$$;

-- --- Per branch: every ACTIVE location of the merchant (inside p_locations when given) ----
create or replace function report_branches(
    p_merchant uuid, p_program uuid, p_locations uuid[], p_from date, p_to date)
returns table (location_id uuid, name text, new_customers integer, visits integer,
               returned integer)
language sql stable security invoker set search_path = public as $$
    select l.id, l.name,
           coalesce(s.new_customers, 0), coalesce(v.visits, 0), coalesce(s.returned, 0)
    from merchant_locations l
    left join lateral (
        select count(*)::integer as new_customers,
               (count(*) filter (where k.returned))::integer as returned
        from report_cohort_cards(p_merchant, p_program, array[l.id], p_from, p_to) k
    ) s on true
    left join lateral (
        select count(*)::integer as visits
        from effective_transactions t
        join cards c on c.id = t.card_id
        where t.location_id = l.id
          and t.merchant_id = p_merchant
          and t.kind in ('accrue_stamps', 'accrue_points')
          and (p_program is null or c.program_id = p_program)
          and report_ec_date(t.created_at) between p_from and p_to
    ) v on true
    where l.merchant_id = p_merchant
      and l.active
      and (p_locations is null or l.id = any (p_locations))
    order by l.created_at, l.name, l.id
$$;

-- --- Home figures: one row per campaign of the merchant, every status ---------------------
-- active_cards: active and not paused. redemptions_30d: the last 30 Ecuador days, today
-- included. new_cards_7d: the last 7 Ecuador days, today included.
create or replace function report_program_figures(
    p_merchant uuid, p_locations uuid[], p_today date)
returns table (program_id uuid, active_cards integer, redemptions_30d integer,
               new_cards_7d integer)
language sql stable security invoker set search_path = public as $$
    select p.id,
           (select count(*) from cards c
             where c.program_id = p.id and c.active and c.paused_at is null
               and (p_locations is null or c.enrolled_location_id = any (p_locations))
           )::integer,
           (select count(*) from effective_transactions t
             join cards c on c.id = t.card_id
             where c.program_id = p.id and t.merchant_id = p_merchant and t.kind = 'redeem'
               and (p_locations is null or t.location_id = any (p_locations))
               and report_ec_date(t.created_at) between p_today - 29 and p_today
           )::integer,
           (select count(*) from cards c
             where c.program_id = p.id
               and (p_locations is null or c.enrolled_location_id = any (p_locations))
               and report_ec_date(c.created_at) between p_today - 6 and p_today
           )::integer
    from programs p
    where p.merchant_id = p_merchant
    order by p.created_at, p.id
$$;

-- --- Redemptions list (export), newest first ---------------------------------------------
create or replace function report_redemptions(
    p_merchant uuid, p_program uuid, p_locations uuid[], p_from date, p_to date)
returns table (occurred_at timestamptz, customer_name text, program_name text,
               branch_name text)
language sql stable security invoker set search_path = public as $$
    select t.created_at, coalesce(cu.full_name, c.holder_name), p.name, l.name
    from effective_transactions t
    join cards c on c.id = t.card_id
    join programs p on p.id = c.program_id
    left join customers cu on cu.id = c.customer_id
    left join merchant_locations l on l.id = t.location_id
    where t.merchant_id = p_merchant
      and t.kind = 'redeem'
      and (p_program is null or c.program_id = p_program)
      and (p_locations is null or t.location_id = any (p_locations))
      and report_ec_date(t.created_at) between p_from and p_to
    order by t.created_at desc, t.id
$$;

-- --- program_metrics_view, aligned with the console (same columns as 0014) ----------------
-- * abandonment window is fixed at 90 days (D21: the per-campaign field is retired);
-- * visits / redemptions / last activity read effective operations only;
-- * second_visit_rate_30d follows the reports definition (later Ecuador day, 30 days).
create or replace view program_metrics_view as
with card_agg as (
    select program_id,
           count(*)                                                    as total_cards,
           count(*) filter (where active)                              as active_cards,
           count(*) filter (where active
                            and created_at >= now() - interval '7 days') as installs_week
    from cards
    group by program_id
),
abandoned_agg as (
    select c.program_id, count(*) as abandoned
    from cards c
    join card_last_activity la on la.card_id = c.id
    where c.active
      and la.last_at < now() - interval '90 days'
    group by c.program_id
),
visit_agg as (
    select c.program_id,
           count(*) filter (where t.kind in ('accrue_stamps', 'accrue_points')) as visits,
           count(*) filter (where t.kind = 'redeem')                            as redemptions
    from effective_transactions t
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
second_visit as (
    select p.id as program_id,
           count(k.card_id)                          as new_customers,
           count(k.card_id) filter (where k.returned) as returned
    from programs p
    cross join lateral report_cohort_cards(
        p.merchant_id, p.id, null,
        report_ec_date(now()) - 29, report_ec_date(now())) k
    group by p.id
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
            else 90 end                                                  as inactivity_days
from programs p
left join card_agg      ca on ca.program_id = p.id
left join abandoned_agg ab on ab.program_id = p.id
left join visit_agg     v  on v.program_id  = p.id
left join eligible_agg  e  on e.program_id  = p.id
left join second_visit  sv on sv.program_id = p.id;

grant select on program_metrics_view to service_role;

-- --- Lock-down (0015 pattern) -------------------------------------------------------------
revoke all on function report_ec_date(timestamptz) from public, anon, authenticated;
revoke all on function report_cohort_cards(uuid, uuid, uuid[], date, date)
    from public, anon, authenticated;
revoke all on function report_second_visit(uuid, uuid, uuid[], date, date)
    from public, anon, authenticated;
revoke all on function report_cohort_weeks(uuid, uuid, uuid[], date, date)
    from public, anon, authenticated;
revoke all on function report_activity(uuid, uuid, uuid[], date, date)
    from public, anon, authenticated;
revoke all on function report_campaigns(uuid, uuid, uuid[], date, date)
    from public, anon, authenticated;
revoke all on function report_levels(uuid, uuid, uuid[]) from public, anon, authenticated;
revoke all on function report_branches(uuid, uuid, uuid[], date, date)
    from public, anon, authenticated;
revoke all on function report_program_figures(uuid, uuid[], date)
    from public, anon, authenticated;
revoke all on function report_redemptions(uuid, uuid, uuid[], date, date)
    from public, anon, authenticated;

grant execute on function report_ec_date(timestamptz) to service_role;
grant execute on function report_cohort_cards(uuid, uuid, uuid[], date, date) to service_role;
grant execute on function report_second_visit(uuid, uuid, uuid[], date, date) to service_role;
grant execute on function report_cohort_weeks(uuid, uuid, uuid[], date, date) to service_role;
grant execute on function report_activity(uuid, uuid, uuid[], date, date) to service_role;
grant execute on function report_campaigns(uuid, uuid, uuid[], date, date) to service_role;
grant execute on function report_levels(uuid, uuid, uuid[]) to service_role;
grant execute on function report_branches(uuid, uuid, uuid[], date, date) to service_role;
grant execute on function report_program_figures(uuid, uuid[], date) to service_role;
grant execute on function report_redemptions(uuid, uuid, uuid[], date, date) to service_role;

notify pgrst, 'reload schema';
