-- Phase 5: SerpAPI round-trip observations carry only the outbound slice; store that fact
-- on the row so the rules engine can prefix alerts without reading data/raw/.
ALTER TABLE observations ADD COLUMN risk_inbound_unverified INTEGER;
