-- conpass — Phase 18: notification history counts effective visits only (D21, O3).
--
-- "Volvieron en 48 h" must not count an operation the cashier undid (or its reversal).
-- Same view as 0015 (same columns, same order); only the visit subquery now reads
-- effective_transactions (0017).

create or replace view notification_history_view as
    select n.*,
           case when n.source = 'automation' then coalesce(d.total, 0)
                else n.recipients end  as recipient_count,
           coalesce(d.delivered, 0)  as delivered,
           coalesce(d.visits_48h, 0) as visits_48h
    from notifications n
    left join lateral (
        select count(*) filter (where dd.status not in ('superseded', 'cancelled')) as total,
               count(*) filter (where dd.status in ('sent', 'sent_silent')) as delivered,
               count(*) filter (where dd.status in ('sent', 'sent_silent') and exists (
                   select 1 from effective_transactions t
                   where t.card_id = dd.card_id
                     and t.kind in ('accrue_stamps', 'accrue_points', 'redeem')
                     and t.created_at > dd.sent_at
                     and t.created_at <= dd.sent_at + interval '48 hours')) as visits_48h
        from notification_deliveries dd
        where dd.notification_id = n.id
    ) d on true;

revoke all on notification_history_view from anon, authenticated;
grant select on notification_history_view to service_role;

notify pgrst, 'reload schema';
