# Add your integration-specific settings here
from app.settings.base import env

# Movebank connection budget, shared across all integrations using the same
# Movebank username (Movebank documents ~31 simultaneous connections per user).
MOVEBANK_MAX_CONNECTIONS = env.int("MOVEBANK_MAX_CONNECTIONS", 25)
# Accessory-measurements records can arrive at Movebank hours after their
# timestamp; the accessory query re-reads this many hours so late arrivals are
# caught (the event-id filter drops already-sent events, so no duplicates).
ACCESSORY_SETTLING_HOURS = env.int("ACCESSORY_SETTLING_HOURS", 12)
# Rolling-queue width for a backfill job: how many individuals are in flight at
# once. Deliberately below MOVEBANK_MAX_CONNECTIONS so backfill never starves
# the steady-state pull or other integrations on the same username.
BACKFILL_MAX_CONCURRENCY = env.int("BACKFILL_MAX_CONCURRENCY", 8)
