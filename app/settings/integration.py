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

# Backfill step budgeting. A cascade step sends one window at a time and never a
# window larger than this many events, bounding the send loop so a step always
# finishes inside MAX_ACTION_EXECUTION_TIME. A fully-fetched window over the cap
# is discarded and the window shrunk (see below), then retried.
MAX_RECORDS_PER_BACKFILL_WINDOW = env.int("MAX_RECORDS_PER_BACKFILL_WINDOW", 5000)
# Floor for the adaptive backfill window (seconds): it shrinks on overflow but
# never below this. At the floor an over-cap window is sent anyway (a burst
# denser than the floor window can hold).
MIN_BACKFILL_WINDOW_SECONDS = env.int("MIN_BACKFILL_WINDOW_SECONDS", 300)
# Safety factor when proportionally shrinking the window on overflow, so the
# resized window aims comfortably below the cap rather than exactly at it.
BACKFILL_WINDOW_SHRINK_SAFETY = env.float("BACKFILL_WINDOW_SHRINK_SAFETY", 0.8)
