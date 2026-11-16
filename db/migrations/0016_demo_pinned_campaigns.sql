-- Phase 17: fixed campaigns in the public demo.
--
-- The demo tenant is one shared account. The owner keeps two fixed campaigns there
-- ("Tarjeta de acumulación de sellos" and "Tarjeta de acumulación por puntos"); anything
-- a visitor creates is deleted 30 minutes after creation by the notifier's minute job
-- (conpass_common.demo.purge_demo_campaigns), cards and transactions cascading with it.
-- `demo_pinned` marks the campaigns that are never purged and cannot be edited.

alter table programs add column if not exists demo_pinned boolean not null default false;

update programs p
   set demo_pinned = true
  from merchants m
 where m.id = p.merchant_id
   and m.is_demo
   and p.name in ('Tarjeta de acumulación de sellos', 'Tarjeta de acumulación por puntos');

-- The purge scans demo campaigns by age every minute.
create index if not exists programs_demo_unpinned_idx
    on programs (merchant_id, created_at) where not demo_pinned;

notify pgrst, 'reload schema';
