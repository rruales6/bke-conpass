-- conpass — Phase 12: card language
--
-- The wallet pass renders its labels in the language the customer had selected on the
-- enrollment form (D16), not the merchant's. That choice is captured once, at enrollment,
-- and stuck to the CARD (not the customer) so it stays put even if a future profile-edit
-- flow lets the customer change their contact-language preference independently.

alter table cards add column if not exists language text not null default 'es'
    check (language in ('es', 'en'));

-- New column on an existing table needs PostgREST to reload before the Data API sees it.
notify pgrst, 'reload schema';
