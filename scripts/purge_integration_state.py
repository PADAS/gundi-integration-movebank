#!/usr/bin/env python3
"""Purge this integration's Redis state (cursors, quiet flags, backfill
watermarks/progress, and backfill job keys) for a single integration.

The Redis instance is SHARED across integrations, so this script only ever
touches keys scoped to the integration_id you pass — never a global flush.
It is a DRY RUN by default: it lists the matching keys and exits. Pass
--apply to actually delete them.

Connection comes from the app settings (REDIS_HOST / REDIS_PORT /
REDIS_STATE_DB), which read the same env vars the service uses. To target a
remote dev Redis, set those env vars (e.g. via a port-forward) before running.

Usage:
    python scripts/purge_integration_state.py <integration_id>            # dry run
    python scripts/purge_integration_state.py <integration_id> --apply    # delete
    python scripts/purge_integration_state.py <integration_id> --apply --include-config
"""
import argparse
import asyncio

import redis.asyncio as redis

from app import settings


def _patterns(integration_id: str, include_config: bool) -> list:
    # A single "integration_state.{iid}.*" glob covers cursors, quiet flags,
    # backfill watermarks, and progress throttles. "backfill.{iid}.*" covers
    # the job meta hash, pending queue, and per-individual configs.
    pats = [
        f"integration_state.{integration_id}.*",
        f"backfill.{integration_id}.*",
    ]
    if include_config:
        pats.append(f"integration_config.{integration_id}")
    return pats


async def _scan(client: redis.Redis, pattern: str) -> list:
    return [key.decode() if isinstance(key, bytes) else key
            async for key in client.scan_iter(match=pattern, count=500)]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("integration_id", help="UUID of the integration to purge")
    parser.add_argument("--apply", action="store_true",
                        help="actually delete (default is a dry run that only lists keys)")
    parser.add_argument("--include-config", action="store_true",
                        help="also drop the portal config cache (integration_config.{id})")
    args = parser.parse_args()

    client = redis.Redis(
        host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_STATE_DB
    )
    print(f"Redis {settings.REDIS_HOST}:{settings.REDIS_PORT} db={settings.REDIS_STATE_DB}")
    print(f"Integration {args.integration_id}  ({'APPLY' if args.apply else 'DRY RUN'})\n")

    total = 0
    try:
        for pattern in _patterns(args.integration_id, args.include_config):
            keys = await _scan(client, pattern)
            print(f"{pattern}  ->  {len(keys)} key(s)")
            for key in keys:
                print(f"    {key}")
            if args.apply and keys:
                await client.delete(*keys)
            total += len(keys)

        verb = "Deleted" if args.apply else "Would delete"
        print(f"\n{verb} {total} key(s).")
        if not args.apply and total:
            print("Re-run with --apply to delete them.")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
