-- Drop the dead columns the engine never read (verified 2026-07-22, applied
-- while the bot was stopped — pre-0d5fcfc code still SELECTed some of these
-- by name, so this migration must only run against code >= 073cba1):
--   users: copy_multiplier, max_slippage_pct, daily_loss_limit_usd,
--          default_allocation_pct, default_max_position_usd, referral_code
--          (per-wallet twins on followed_traders are the real controls;
--          referral program removed at 36c497b)
--   followed_traders: allocation_pct (legacy portfolio-weight model),
--          max_total_shares (control removed at 0d5fcfc)
ALTER TABLE users DROP COLUMN IF EXISTS copy_multiplier;
ALTER TABLE users DROP COLUMN IF EXISTS max_slippage_pct;
ALTER TABLE users DROP COLUMN IF EXISTS daily_loss_limit_usd;
ALTER TABLE users DROP COLUMN IF EXISTS default_allocation_pct;
ALTER TABLE users DROP COLUMN IF EXISTS default_max_position_usd;
ALTER TABLE users DROP COLUMN IF EXISTS referral_code;
ALTER TABLE followed_traders DROP COLUMN IF EXISTS allocation_pct;
ALTER TABLE followed_traders DROP COLUMN IF EXISTS max_total_shares;
