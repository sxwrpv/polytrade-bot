-- Timestamp for the closing fence: set whenever copy_positions.status flips to
-- 'closing'. Lets the engine age-gate recovery of rows stuck mid-close (crash
-- or uncertain SELL) without racing a close that is legitimately in flight.
ALTER TABLE copy_positions ADD COLUMN IF NOT EXISTS closing_at TEXT;
