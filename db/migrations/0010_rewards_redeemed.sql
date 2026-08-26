-- conpass — rewards redeemed counter
--
-- `cards.rewards_available` counts rewards earned and not yet claimed (up on
-- completion, down on redeem) — it cannot also answer "how many has this card
-- redeemed in total?" once a redemption decrements it back toward zero. The wallet
-- pass needs to show both numbers, so `rewards_redeemed` is a separate, monotonically
-- increasing counter that only redeem touches.

alter table cards add column if not exists rewards_redeemed integer not null default 0;

-- Backfill from history: cards that redeemed before this column existed must not
-- start back at zero. `transactions.kind = 'redeem'` is one row per redemption.
update cards
set rewards_redeemed = redeemed.count
from (
    select card_id, count(*) as count
    from transactions
    where kind = 'redeem'
    group by card_id
) as redeemed
where cards.id = redeemed.card_id;

-- New column on an existing table needs PostgREST to reload before the Data API sees it.
notify pgrst, 'reload schema';
