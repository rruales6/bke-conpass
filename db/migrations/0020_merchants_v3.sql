-- conpass — Phase 18 (B3): merchants console v3.0.
--
-- location_stats: per-branch numbers for GET /merchants/{id}/locations, read in ONE query per
-- merchant (no N+1 per location):
--   enrolled_clients  cards whose enrolled_location_id is the branch;
--   visits_30d        effective accruals (a reversed operation and its reversal drop out,
--                     see effective_transactions in 0017) scanned at the branch in the last 30 days.
-- Depends on 0015 (cards.enrolled_location_id, transactions.location_id) and 0017
-- (effective_transactions).

create or replace view location_stats as
    select l.id as location_id,
           l.merchant_id,
           (select count(*) from cards c
             where c.enrolled_location_id = l.id) as enrolled_clients,
           (select count(*) from effective_transactions t
             where t.location_id = l.id
               and t.kind in ('accrue_stamps', 'accrue_points')
               and t.created_at >= now() - interval '30 days') as visits_30d
    from merchant_locations l;

revoke all on location_stats from anon, authenticated;
grant select on location_stats to service_role;

notify pgrst, 'reload schema';
