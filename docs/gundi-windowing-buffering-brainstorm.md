# Parked Brainstorm — Gundi Windowing/Buffering (bulk writes + downsampling)

**Status:** PARKED mid-brainstorm (not a spec). Resume when ready.
**Date started:** 2026-07-24
**Scope note:** cross-repo Gundi architecture (routing / dispatcher / ER batch
ingest), NOT specific to gundi-integration-movebank. This file just lives here
for convenience.

## Motivating problem

A one-time bulk historical load: one Movebank vulture study with **~169,151,621
records**, already downloaded, to be written to EarthRanger at **full fidelity**
(researchers want native resolution; downsampling is NOT acceptable for this
case).

Findings so far:
- The Movebank **download is not the bottleneck** (works fine).
- The movebank action-runner **backfill** *can* theoretically do it (~1.5–4
  days cascading, tunable via `backfill_max_concurrency`), but re-fetches from
  Movebank (ignores the local download) and, more importantly, feeds Gundi's
  **per-item, real-time-oriented pipeline**.
- The **glaring bottleneck** is writing to EarthRanger **one observation at a
  time**. ER has a **batch write** capability, but using it requires
  **significant changes to Gundi's pipeline**.
- Data-reduction is off the table for THIS load (full fidelity required), but
  the user separately wants **downsampling as a general Gundi capability** for
  other cases.

## Key insight (the unifying idea)

Both **bulk batch-writes** and **downsampling** are the same primitive: a
**stateful windowed buffer** that acts on a *group* of observations instead of
one item at a time.

**Flush policy = (trigger, transform):**
- **Bulk/batch write** = trigger on size (N) or time; transform = **identity**
  → hand the whole group to ER's batch endpoint.
- **Downsampling** = trigger on a time window; transform = **reduce** (one
  representative per window, or thin to a target rate) → emit fewer
  observations.

i.e. batch write is downsampling with an identity reducer + a size trigger.
One config-driven stage, keyed per **(destination, source[, sensor])**.

## Open design decisions (where the real design lives)

1. **Flush triggers** — size vs. time vs. both. Bulk → size (bounded memory,
   big batches); downsampling → time windows (tumbling); real-time steady-state
   → small size + short time to keep latency low. Likely all three knobs, per
   route.
2. **State & bounded memory at burst scale** — a 169M firehose demands
   size-triggered flush and a durable/evictable store (Redis? streaming
   buffer?); never hold a window in memory.
3. **Windowing semantics** — time windows bring watermarks + late-arrival
   handling (Gundi already partly does this for accessory data), plus how a
   downsample picks its representative and handles out-of-order points.
4. **Idempotency on flush** — today `loaded_at` deliberately DEFEATS ER dedup;
   a retried batch after a partial failure would duplicate. A buffered/batch
   model needs a real idempotency key so a re-flush is safe. More important at
   bulk scale, not less.

## Scope tension (unresolved)

The general windowing stage is a genuine Gundi initiative; the 169M load is
one-time + full-fidelity and only needs the **simplest slice** (identity
transform + size trigger). Fork to decide later:
- (a) Build the general primitive and run the load through it, or
- (b) A tactical bulk-load bridge now + the general windowing stage as the
  proper follow-up.

## NEXT QUESTION (where we paused)

Pipeline placement + current topology. Need the user's walk-through of the
stages **integration → Gundi routing → dispatcher → ER**, and where buffer
state already lives. Working instinct: the windowing stage sits **just before
the per-destination dispatcher** (destination-aware so it can call ER's batch
API; keyed by (destination, source); buffer in Redis; size/time flush) —
OR the batching belongs **inside the ER dispatcher itself** (since the batch
API is ER-specific). To be decided with the user.

## Related repos to look at when resuming

gundi-dispatcher-er, gundi-movebank-dispatcher, gundi-core, gundi-client(-v2),
gundi-routing (cdip-routing), earthranger-warehouse (full-res alt. home).
