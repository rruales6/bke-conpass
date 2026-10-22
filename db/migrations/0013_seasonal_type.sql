-- conpass — seasonal campaigns, part 1 of 2 (Phase 14)
--
-- A new campaign kind: `seasonal_discount` ("campaña de temporada"), a discount for a
-- special date, usable once per customer while the campaign is active.
--
-- Its own migration on purpose: Postgres refuses to USE a new enum value inside the
-- transaction that added it, and 0014 references it in constraints and views.

alter type program_type add value if not exists 'seasonal_discount';
