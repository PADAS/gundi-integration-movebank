#!/usr/bin/env python3
"""Verify whether a Movebank study's per-individual `timestamp_end` metadata is
STALE relative to the actual event stream — the suspected reason the pull skips
for "no new data".

The steady-state pull caps its fetch at `min(now, individual.timestamp_end)` and
short-circuits once a sensor cursor reaches `timestamp_end`. If Movebank's
`timestamp_end` (from the get_individuals_by_study metadata) lags real events,
new data is never fetched. This script checks, per individual, whether GPS
events exist STRICTLY AFTER the reported `timestamp_end`.

Read-only: it only issues Movebank direct-read queries; it writes nothing to
Redis, Gundi, or EarthRanger.

Credentials/target come from env (no secrets in the file):
    MOVEBANK_USERNAME   (required)
    MOVEBANK_PASSWORD   (required)
    MOVEBANK_STUDY_ID   (required)
    MOVEBANK_BASE_URL   (default https://www.movebank.org)
    MOVEBANK_INDIVIDUAL_ID  (optional — check just this one)
    MAX_INDIVIDUALS     (optional — cap how many individuals to check)

Run (with network + creds), e.g. inside the test image:
    docker run --rm -e MOVEBANK_USERNAME -e MOVEBANK_PASSWORD -e MOVEBANK_STUDY_ID \
      -v "$PWD/app":/code/app -v "$PWD/scripts":/code/scripts -w /code \
      mb-runner-test python scripts/verify_movebank_timestamp_end.py
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

from dateutil.parser import parse as parse_date

# Make `app` importable regardless of CWD: add the repo root (this file's
# parent-of-parent, e.g. /code when scripts/ is mounted at /code/scripts).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.actions.client as client
from app.actions.client import generate_individuals


def _utc(dt):
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _events_after(mb, study_id, individual_id, after, now):
    """Count GPS events strictly after `after`, and track the latest timestamp.
    Mirrors the pull's query (sensor 653), but with minimum_event_id=0 so no
    cursor filtering hides anything."""
    count = 0
    latest = None
    async for event in mb.get_individual_events_by_time(
        study_id=study_id, individual_id=individual_id,
        timestamp_start=after, timestamp_end=now,
        sensor_type_ids=[client.MovebankClient.SENSOR_TYPE_GPS], minimum_event_id=0,
    ):
        try:
            ts = _utc(parse_date(event.get("timestamp")))
        except Exception:
            continue
        if ts > after:                       # strictly beyond the reported end
            count += 1
            if latest is None or ts > latest:
                latest = ts
    return count, latest


async def main():
    username = os.environ.get("MOVEBANK_USERNAME")
    password = os.environ.get("MOVEBANK_PASSWORD")
    study_id = os.environ.get("MOVEBANK_STUDY_ID")
    base_url = os.environ.get("MOVEBANK_BASE_URL", "https://www.movebank.org")
    only_individual = os.environ.get("MOVEBANK_INDIVIDUAL_ID")
    max_individuals = os.environ.get("MAX_INDIVIDUALS")
    if not (username and password and study_id):
        sys.exit("Set MOVEBANK_USERNAME, MOVEBANK_PASSWORD, and MOVEBANK_STUDY_ID.")

    now = datetime.now(tz=timezone.utc)
    mb_client = client.MovebankClient(base_url=base_url, username=username, password=password)

    stale = 0
    checked = 0
    async with mb_client as mb:
        rows = await mb.get_individuals_by_study(study_id=study_id)
        individuals = list(generate_individuals(rows))
        if only_individual:
            individuals = [i for i in individuals if i.id == only_individual]
        if max_individuals:
            individuals = individuals[: int(max_individuals)]

        print(f"Study {study_id}: checking {len(individuals)} individual(s); now={now.isoformat()}\n")
        for ind in individuals:
            end = ind.timestamp_end
            if end is None:
                print(f"  {ind.id} ({ind.nick_name}): no timestamp_end (pull uses `now` — not affected)")
                continue
            end = _utc(end)
            checked += 1
            count, latest = await _events_after(mb, study_id, ind.id, end, now)
            if count > 0:
                stale += 1
                lag = latest - end
                print(f"  STALE  {ind.id} ({ind.nick_name}): timestamp_end={end.isoformat()} "
                      f"but {count} GPS event(s) after it; latest={latest.isoformat()} (lag {lag})")
            else:
                print(f"  ok     {ind.id} ({ind.nick_name}): timestamp_end={end.isoformat()}, "
                      f"no GPS events after it")

    print(f"\nSummary: {stale}/{checked} individual(s) with real GPS events beyond their "
          f"reported timestamp_end.")
    if stale:
        print("=> CONFIRMED: timestamp_end metadata is stale; the pull's timestamp_end ceiling "
              "is hiding new data.")


if __name__ == "__main__":
    asyncio.run(main())
