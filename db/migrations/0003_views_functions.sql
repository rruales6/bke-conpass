-- conpass — derived views & tier-default helpers

-- Tier limits (business model §3). null = unlimited.
create or replace function apply_tier_defaults() returns trigger
    language plpgsql as $$
begin
    if new.active_pass_limit is null and new.program_limit is null then
        case new.tier
            when 'starter' then
                new.active_pass_limit := 250;  new.program_limit := 1;
                new.operation_user_limit := 1; new.mrr_usd := coalesce(nullif(new.mrr_usd,0), 19);
            when 'growth' then
                new.active_pass_limit := 1500; new.program_limit := 3;
                new.operation_user_limit := 5; new.mrr_usd := coalesce(nullif(new.mrr_usd,0), 49);
            when 'pro' then
                new.active_pass_limit := 10000; new.program_limit := 10;
                new.operation_user_limit := null; new.mrr_usd := coalesce(nullif(new.mrr_usd,0), 149);
            when 'enterprise' then
                new.active_pass_limit := null; new.program_limit := null;
                new.operation_user_limit := null; new.mrr_usd := coalesce(nullif(new.mrr_usd,0), 499);
        end case;
    end if;
    new.updated_at := now();
    return new;
end;
$$;

create trigger subscriptions_tier_defaults
    before insert or update of tier on subscriptions
    for each row execute function apply_tier_defaults();

-- Active-pass billing metric per merchant (installed & not deleted).
create or replace view merchant_active_passes as
    select merchant_id, count(*) filter (where wallet_installed and active) as active_pass_count
    from cards group by merchant_id;

-- Redemption report source.
create or replace view redemptions_view as
    select t.id,
           c.holder_name           as customer_name,
           cust.phone              as customer_phone,
           cust.email             as customer_email,
           p.id                   as program_id,
           p.name                 as program,
           p.reward               as reward,
           t.created_at           as redeemed_at,
           p.merchant_id          as merchant_id
    from transactions t
    join cards c     on c.id = t.card_id
    join programs p  on p.id = c.program_id
    left join customers cust on cust.id = c.customer_id
    where t.kind = 'redeem';
