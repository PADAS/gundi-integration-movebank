# WD EarthRanger: Mara Raptor individuals missing from the site

Date: 2026-10-26
Context: Jake compared the WD EarthRanger site's subjects against the Mara
Raptor Project profile report (`mara-raptor-project-distribution.tar.gz`,
generated 2026-07-28 from the Movebank CSV export: 169M events, 60
individuals, 58 tags) and found 11 individuals with no subject on WD.

## The 11 individuals, from the report's per-individual summary

Source: `explore-20260728T105433.md`, "Per-individual summary".

| Individual        | Tag           | Fixes | First fix  | Last fix       |
|-------------------|---------------|------:|------------|----------------|
| Tawny Eagle 01    | 192092        | 14.9M | 2019-10-07 | **2024-03-29** |
| Martial Eagle 04  | 5104 †        | 110k  | 2016-11-06 | 2017-07-15     |
| Martial Eagle 09  | 5109 †        | 18k   | 2016-11-26 | 2017-01-23     |
| Martial Eagle 14  | 5114          | 340k  | 2017-01-28 | 2019-08-12     |
| Martial Eagle 16  | 5116          | 209k  | 2017-02-01 | 2019-09-13     |
| Martial Eagle 17  | 5010          | 271k  | 2017-02-08 | 2021-03-16     |
| Martial Eagle 19  | 5109 †        | 190k  | 2017-01-25 | 2018-10-28     |
| Martial Eagle 20  | 3095 + 5105 † | 434k  | 2017-04-12 | 2020-06-27     |
| Martial Eagle 28  | 192103 †      | 1.6M  | 2020-09-22 | 2021-03-17     |
| Martial Eagle 33  | 202886        | 741k  | 2021-04-28 | 2021-06-20     |
| Steppe Eagle 02   | 200592        | 901k  | 2021-01-08 | 2021-09-11     |

† Tag later reused by another individual: 5104 → Martial Eagle 22,
5109 shared by Martial Eagle 09 and 19, 5105 → Martial Eagle 05,
192103 → Martial Eagle 39. Martial Eagle 20 carried two tags.

## Findings

### 1. All 11 are dormant; the steady-state pull cannot create them

The most recent last fix among the 11 is March 2024; most stopped
transmitting in 2017–2021.

`action_pull_observations` fans out `pull_events_for_individual` per
individual. On first sight of an individual the pull seeds its cursor at
*now* and only fetches forward (`app/actions/handlers.py`,
`_seed_pull_cursor_at_end` / `action_pull_events_for_individual`).
EarthRanger creates a subject on its first observation. An individual with
no events after the integration went live therefore never produces an
observation and never gets a subject. Only `action_backfill` can bring these
in.

This is the expected mechanism, not a failed ingestion run.

### 2. Jake's list is likely incomplete, or a backfill partly covered the dormant set

The study has roughly 25 other individuals that are just as dormant and are
*not* on the list: Martial Eagle 01, 02, 03, 05, 06, 07, 10, 15, 21, 29, 30,
34, 36, 37; Tawny Eagle 06, 09, 15, 16; African Hawk-eagle 02; and others
with a last fix before 2025.

Whether those are present on WD decides the diagnosis:

- **Also missing** → no backfill has run against WD (or it ran with a
  `start` later than their data). The 11 are the ones Jake happened to
  notice; the real gap is the whole dormant set.
- **Present** → a backfill did run and these 11 hit a skip path in
  `action_backfill`:
  - An empty range: individuals are only queued when `start < end`. A
    backfill with e.g. `start=2020-01-01` drops Martial Eagle 04, 09, 14,
    16 and 19 outright.
  - `skipped_existing`: an individual with an existing cursor
    (`highest_event_id > 0`) but no `coverage_start` is skipped with the
    WARNING `skipped N individuals with existing collection history but no
    recorded coverage floor; clear their cursor to force a full backfill`.

### 3. Tag reuse is not the cause

The ER source is keyed by Movebank `individual_id` and the subject/source
name by the individual's `local_identifier` (`app/actions/transform.py`,
`_display_name` in `handlers.py`). Martial Eagle 04 and 22 sharing tag 5104
does not merge them. Useful to know when reading the ER side, nothing more.

### 4. Side note: bad timestamps on Martial Eagle 13

The profile's `ts_max` is 2033-02-06, from Martial Eagle 13 (tag 5113).
`transform.py` drops rows with `recorded_at` in the future so they never
reach ER, but Movebank holds future-dated fixes on that tag. Worth passing
to the Mara team.

## Next steps

1. Confirm whether `action_backfill` has been run on the WD integration,
   and with what `start` (`"all"` resolves per individual to its
   `timestamp_start`).
2. Ask Jake to check two or three of the other dormant birds (e.g. Martial
   Eagle 07, Tawny Eagle 16) on WD, to distinguish "no backfill" from
   "backfill skipped these".
3. If a backfill did run, grep the WD integration's Cloud Run logs for
   `Backfill <job_id>: skipped` and `Skip Movebank` lines, which name the
   skip reason per individual.
4. Resolution in either case is a backfill for the affected individuals
   (`individual_ids` set, `start="all"`), with `restart=true` if a prior
   job for the same parameters exists. For `skipped_existing` individuals,
   clear their cursor first (`scripts/purge_integration_state.py`).

## What was not verified

No access from this machine to the WD EarthRanger site, the Gundi portal,
the integration's Redis, Cloud Run logs or Movebank. Everything above is
from the report tarball and the connector's code; the WD-side checks in
"Next steps" are still needed.

## Update 2026-11-08: verified against Gundi and Movebank

Checked read-only via the Gundi API (runner service credentials in
`local/.env.production`) and the Movebank API (the integration's own
`EarthRanger_Integrations` user). Portal activity logs only reach back to
2026-10-05; prod Redis is not reachable from a laptop; gcloud auth for
`chrisdo@earthranger.com` had expired so Cloud Run logs were not read.

- WD integration: `2b736561-bf29-438c-96a9-6dc8aecff8d9`
  "Stratton's Mara Raptor Project (170240210)", owner Wildlife Dynamics,
  destination `https://wildlifedynamics.pamdas.org/`.
- Saved backfill config on the integration: `start=2016-01-01`,
  `individual_ids=[]` (whole study). That start predates every bird's
  first fix, so the "empty range" skip path in finding 2 is ruled out.
- Gundi has **49 sources** for this integration. The study has 60
  individuals with data (plus 7 Wahlberg's Eagles with no deployments).
  The 11 without a source are **exactly Jake's 11**. Nothing else is
  missing, so his list is complete.
- Source `created_at` runs from 2026-07-24 03:05Z to 2026-08-21 15:11Z and
  includes ~25 birds dormant since 2017–2023 (Martial Eagle 01/02/03/05/07
  were created 2026-08-21). A whole-study backfill therefore DID run and
  rolled through the study for about four weeks. Finding 2's "no backfill"
  branch is ruled out; the 11 hit a failure path inside that job.
- Movebank API (as the integration user) returns all 11 with valid
  `timestamp_start`, `gps` in `sensor_type_ids` and the expected event
  counts. Not a Movebank visibility/permission problem, not the
  `no_timestamp_start` / `no_supported_sensors` skips.

### Most likely mechanism

Each of the 11 never delivered a single observation batch, so Gundi never
created a source and ER never created a subject. Two code paths produce
that, and both are quiet at the portal level:

1. **Abandon after consecutive failures.** `action_backfill_events_for_individual`
   re-triggers a failed step up to `BACKFILL_MAX_ATTEMPTS = 5` times, then
   logs one WARNING (`abandoned after N attempts`) and *finalizes the
   individual as completed with 0 observations*. The job summary still
   reads `finished 60/60`. Movebank 500/503s (seen on this integration
   2026-10-09, 10-20, 10-31) and Gundi 400 batch rejections (seen
   2026-10-13) are exactly the exceptions that path catches.
2. **Stranded by the Cloud Run 429 problem.** The per-individual cascade
   and the dispatch-next chain are PubSub messages. From at least
   2026-08-21 the runner was dropping ~270k pushes/day to the dead-letter
   topic (see memory `movebank-prod-ops-reference`). The last source was
   created 2026-08-21. A lost continuation leaves the individual in
   `in_flight` forever and the job never reaches `is_done`; a lost first
   dispatch leaves it `pending`.

Distinguishing 1 from 2 needs the job's Redis snapshot or the August
Cloud Run logs (`Backfill job-…/<individual_id>: abandoned` vs. a job
that never logged `finished`). Either way the remedy is the same.

### Remedy

Run the backfill action on the WD integration for just the 11, e.g.

```json
{"study_id": "170240210",
 "start": "2016-01-01",
 "individual_ids": ["982745369","182295331","182297073","205394805",
                    "205394964","229755797","258264837","263782744",
                    "1276926324","1502985629","1399043972"]}
```

(Tawny Eagle 01, Martial Eagle 04, 09, 14, 16, 17, 19, 20, 28, 33,
Steppe Eagle 02, in that order.) A different `individual_ids` list hashes
to a new job id, so this seeds fresh regardless of the state of the old
whole-study job; the per-job watermarks are keyed by job id too. Each
bird's pull cursor was seeded at the old job's start with `coverage_start`
set, so the range resolves to `[2016-01-01, coverage_start)` and all 11
queue. Tawny Eagle 01 alone is ~14.9M fixes; expect days, and watch for
the 429 dead-lettering before starting (runner is still capped at 2
instances × concurrency 8).

### Code follow-ups

- Abandonment is invisible: surface abandoned individuals in the job's
  activity-log summary (count them separately from `completed`) and emit
  an `integration_custom_log` WARNING naming the individual.
- Consider not counting an abandoned-with-0-observations individual as
  completed, or re-queuing it at the end of the job once.

## Update 2026-11-09 03:00Z: remediation backfill started

Chris triggered the backfill from the portal with the 11 individual ids and
`start=2016-01-01`. Job id `job-44ed17250673`, seeded 03:02:22Z with all 11
queued.

- The first click returned "Action Failed / status 500". Cloud Run logs:
  `action_backfill` hit `NoConnectionSlot` (all 25 Movebank slots held by
  the :00 pull tick's sub-actions). The pull catches that and skips; the
  backfill action does not, so it surfaces as a 500. Clicking again
  between ticks worked. **Code fix:** catch `NoConnectionSlot` in
  `action_backfill` and return a clean "retry shortly" result (or wait for
  a slot with a bounded timeout).
- Within the first minute three individuals logged
  `attempt 1 failed (Rate limit retries exhausted after 3 attempts over 31s)`
  from Movebank 429s. Those count toward `BACKFILL_MAX_ATTEMPTS = 5`
  consecutive failures, and the retry is immediate, so a sustained 429
  burst can abandon an individual within ~3 minutes. This is the most
  likely way the July/August job lost the 11. **Watch item:** grep
  `Backfill job-44ed17250673` for `abandoned` and `attempt [3-5]`.
  **Code fix:** treat `MBRateLimitError` like `NoConnectionSlot` (back off
  and re-trigger without counting an attempt), or add a delay before the
  retry.
- Cloud Run runner logs are retained 30 days (the 400-day bucket is audit
  logs only), so the July/August job's own lines are gone; the abandon vs.
  stranded question for the original run cannot be settled from logs.

Ticket for the code follow-ups: GUNDI-5916 (https://allenai.atlassian.net/browse/GUNDI-5916).

## Update 2026-11-09 18:20Z: the remediation job is overloading the runner

Other Movebank integrations went unhealthy during the day. Runner metrics
(Cloud Monitoring, hourly) before and after the job:

| Metric (per hour)                  | 2026-11-08 typical | 2026-11-09 04Z onward |
|------------------------------------|-------------------:|----------------------:|
| Cloud Run 429 (no instance)        | ~3,000             | 30,000–65,000         |
| Cloud Run 504 (request timeout)    | 0–5                | 300–930               |
| Dead-lettered PubSub messages      | 0–10               | 3,500–8,600           |
| Active instances (mean)            | ~1.2               | 15–16 (pegged at max) |
| Pull skips, Movebank slot exhausted| 4,600–5,400        | 3,800                 |
| Movebank 429 backoffs              | ~800               | ~1,450                |

The Movebank connection budget is NOT the mechanism (slot skips did not
rise). The runner is drowning in its own PubSub messages, and other
integrations' pull sub-actions are the ones being 429'd and dead-lettered.

### Mechanism: duplicate step chains

- Cloud Run `timeoutSeconds` is 300. A backfill step budgets
  `0.8 * MAX_ACTION_EXECUTION_TIME` = 432 s of windows and is only
  hard-cancelled at 540 s. Early steps already took 4–7 min.
- A step over 300 s gets a 504 from Cloud Run, so PubSub redelivers it
  while the original keeps running and publishes its own continuation.
  Both chains continue, so each slow step doubles the chains for that
  individual. The code comment "the PubSub endpoint always acks, there is
  no redelivery" assumes Cloud Run never times out first.
- Dense birds multiplied fastest. Martial Eagle 28 (1276926324) went from
  36 step lines in the 03Z hour to 330–540 per hour and is still running
  at 18:16Z, with ~20 parallel chains at different `scan_from` positions.
  Martial Eagle 17 (229755797) did the same until 10:48Z.
- Each duplicate chain independently hits Movebank 429s, counts attempts,
  and abandons: ME28 was "abandoned" 57+ times, each one incrementing
  `completed`. The job log reads `finished: 355/11 individuals`.
- Once `completed >= total`, `_finalize_backfill_individual` stops
  dispatching from the pending queue. Martial Eagle 33 (1502985629) was
  never dispatched and still has no source.
- Chains for ME16, ME20 and Tawny Eagle 01 stopped after 1–56 steps, far
  short of their ranges (TE01 needs ~1,350). Those continuations were
  most likely dead-lettered during the 03Z–04Z crunch.
- The max-instances change (4→8 at 03:40Z, 8→16 at 04:03Z) did not cause
  the load, but it gave the duplicates room to run; at 4 instances they
  would have been 429'd themselves.

### Immediate action

Cancel the job from the portal: run the backfill action with the same
`study_id`, `start` and `individual_ids` (same job id) and `cancel=true`.
Every chain exits at its next step and redeliveries hit the tombstone.
Expect 504s, dead-letters and 429s to fall within ~30 min. If they do not,
revert max instances to 4.

### Before any re-run (add to GUNDI-5916)

1. Raise Cloud Run `timeoutSeconds` above `MAX_ACTION_EXECUTION_TIME`
   (≥ 600) in `gundi-integrations-v2-infra`, or cap the step budget below
   the Cloud Run timeout.
2. Guard against duplicate delivery: a per-(job, individual) step lease in
   Redis so a second delivery of the same step exits.
3. `record_completion` must not exceed `total`; abandon counts separately;
   `is_done` must not strand the pending queue.
4. Movebank 429 backs off with a delay and does not count as an attempt.

## Update 2026-11-10 17:30Z: the re-runs did not finish either

Chris re-ran the same 11-id backfill twice after the 2026-11-09 cancel, both with
`restart=true` (which wipes the per-individual watermarks, so each run started over):

| run | seeded | ended | how |
|---|---|---|---|
| 23:06Z 2026-11-09 | 9 individuals | cancelled 00:04Z | steps hard-cancelled at 540s; 1/9 completed (probably ME17) |
| 04:53Z 2026-11-10 | 8 individuals | "finished 8/8" 06:03Z | every completion was an abandon; counter overflowed to 39/8 |

Prod ran revision 00023 (created 21:58Z 2026-11-09, `timeoutSeconds` still 300, max 4
instances) for the 04:53Z run. Same mechanism as 2026-11-09: steps hit the 300s Cloud
Run timeout (504s from 04:56Z), PubSub redelivered while the originals kept running,
duplicate chains formed, Cloud Run 429s ran at ~10k per 10 min from 05:10 to 06:10Z and
~8k messages were dead-lettered (other integrations' pulls included). A NoConnectionSlot
hot loop re-triggered ~3,300 steps in 06:02–06:04Z. The abandons were Movebank 429s
(`MBRateLimitError: Rate limit retries exhausted after 3 attempts over 31s`) counted as
attempts.

Per bird after the 04:53Z run (cursor = earliest Movebank timestamp reached; portal
step results + runner logs):

| bird | seeded | result | earliest reached | gap left |
|---|---|---|---|---|
| ME04, ME09 | no (already covered) | completed in the 2026-11-09 run, verify in ER | | |
| ME17 | no (already covered) | completed in the 23:06Z run, verify in ER | | |
| ME14 | yes | abandoned ×16 (dup chains) | 2018-05-08 | 2017-?..2018-05 |
| ME16 | yes | abandoned ×3 | 2017-03-16 | ~6 weeks to its 2017-02-01 start |
| ME19 | yes | abandoned ×20 | 2017-02-10 | small |
| ME20 | yes | chain lost 05:20Z (dead-lettered) | 2019-09-09 | 2019-09 back to start |
| ME28 | yes | chain lost 05:20Z | 2021-01-06 | almost everything (1.55M events) |
| SE02 | yes | chain lost 05:33Z | 2021-03-30 | almost everything (901k events) |
| TE01 | yes | never completed one step | — | everything (14.8M events) |
| ME33 | yes (Movebank individual exists) | never completed one step | — | everything; no ER source yet |

~1.4M observations were delivered in the run, so ER has partial tracks. Treat as partial.

### Recovery (resume, do not restart)
1. Do NOT use `restart=true` again; it deletes the watermarks the abandoned/lost birds
   resume from. The abandon path keeps the watermark and lowers coverage_start to the
   reached cursor, so a plain re-run resumes each bird where it stopped.
2. The finished job still occupies the job id, so a plain re-run returns `already_active`.
   Sequence: same config + `cancel=true` (retires the finished job with a tombstone),
   then same config with `cancel=false, restart=false` → "prior run was cancelled and has
   drained; seeding fresh" and each bird resumes from its watermark.
3. Only after the timeout fix below, and with `backfill_max_concurrency` 1–2.
4. Verify in ER per subject: earliest track point vs. Movebank `timestamp_start`.

### Prevention (ordered)
1. Cloud Run request timeout ≥ 600s for the runner (module variable + terragrunt input),
   or until then set `MAX_ACTION_EXECUTION_TIME=270` via `additional_env_vars` so the
   step budget (216s) and hard-cancel (270s) stay under the 300s timeout.
2. Per-(job, individual) step lease in Redis so a redelivered step cannot run alongside
   the original.
3. `record_completion` bounded by `total`; abandons counted separately; `is_done` must not
   fire while any individual is pending or in flight.
4. Movebank 429 → delayed re-trigger, not an attempt (GUNDI-5916).
5. NoConnectionSlot → delayed re-trigger (Redis backoff key or in-step sleep), not an
   immediate PubSub republish.
6. Portal/UX: `restart` should warn that it discards progress.

### Deployed 2026-11-10 18:56Z
Infra PR #132/#133 (module v2.2.7): prod `movebank-actions-runner` revision 00024 runs with
`timeoutSeconds=600` and `movebank-actions-sub` `max_delivery_attempts=20`. The runner code
fixes (Movebank 429 and NoConnectionSlot as delayed re-triggers, step lease, counter bounds)
are still open in GUNDI-5916, so the recovery re-run should use `backfill_max_concurrency=1`.

### ER 409s on batch delivery (checked 2026-11-10 19:50Z)
WD dispatcher `wildlife-earth-dis-89647e08…` logged 3,986 `409 Conflict` from
`POST /sensors/generic/movebank_mara_raptor_project_170240210/status`, 3,944 of them in the
05:00Z hour of the duplicate-chain run; 39 at 06:xx; none since. ER (`das/sensors/handlers.py`)
skips an observation whose (source, recorded_at) already exists, then bulk-saves; a 409 is the
`UniqueViolation` when a *concurrent* request inserts the same rows between the check and the
save (ER retries once, then returns 409). So 409 == two deliveries of the same observations
racing, i.e. the duplicate chains. erclient maps 409 to `ERClientRateLimitExceeded`; the
dispatcher treats it as transient (not in `PERMANENT_ER_STATUS_CODES={400}`), records no
cooldown (`_scope_for_failure` returns None for 409), publishes partial progress and nacks the
envelope; the redelivery skips delivered items and the previously conflicting rows now exist,
so the retry succeeds. The WD dispatcher subscription has no dead-letter policy, so 409s cost
retries, not data. 0 batch dead-letter notices since 2026-11-09.

Separate, ongoing: since 19:00Z today ~50 of ~450 batch envelopes (11%) failed with
`httpx.ReadTimeout` against ER (erclient read timeout 20s, batches of 200) and were retried.
ER does a per-observation lookup plus subject/source work on each row, so 200-row batches can
exceed 20s under load. Relevant to raising `OBSERVATIONS_BATCH_SIZE` (PR #36): the dispatcher
re-chunks at `ER_BULK_SIZE=200`, so a larger runner batch does not change the ER request size.

### WD dispatcher out-of-memory kills (checked 2026-11-10 20:00Z)
The per-destination ER dispatcher is a Cloud Functions 2nd-gen service provisioned by cdip
(`cdip_admin/deployments/tasks.py`, `functions_v2.ServiceConfig`) with no `available_memory`,
so it runs at the 256 MiB default, with concurrency 8, timeout 120s, max 15 instances. Cloud Run
logged "instance was found to be using too much memory and was terminated" 532 times in the
05:00Z storm hour, 27 at 06:xx, and 18 at 19:xx during the single-chain resume (8 concurrent
200-row batches per 256 MiB instance). A killed instance fails the push, Pub/Sub redelivers,
and progress dedup skips what was delivered, so it costs retries, not data, but it is the
likely source of part of the ~11% ReadTimeout/transport retry rate. Fix: pass
`available_memory` (e.g. 512M) from the dispatcher defaults in cdip and redeploy; or lower
concurrency for this destination. Also bounds any future increase of batch sizes.

### After the dispatcher memory bump (20:07Z to 20:17Z)
WD dispatcher at 512Mi (revision 00013): 0 memory kills, 141 batch envelopes, 15 batch send
failures (~11%): 12 `httpx.ReadTimeout` against ER (erclient read timeout 20s, hard-coded in
`core/dispatchers.py`, no env override) and 3 `409 Conflict`. The 409s now come from the
timeout path: ER keeps processing the 200-row request after the client gives up at 20s, the
nacked envelope is redelivered, and the retry races the still-running original. Levers:
`ER_BULK_SIZE` is env-configurable (default 200) and can be set on the WD service by hand;
the read timeout needs a dispatcher change (pass `data_timeout` from a setting, default 20).
With `ER_BULK_SIZE=100` by hand (revision 00014, 20:21Z): 84 envelopes in 10 min, 2 batch
send failures (both ReadTimeout), 0 409s, 0 memory kills; ~2.4% vs ~11% before. Keep 100 for
this destination (add to the deployment's env_vars so a portal redeploy keeps it) and make the
erclient read timeout a dispatcher setting.

### Overnight 2026-11-10 23:16Z to 2026-11-11 02:21Z: duplicate chains again, without 504s
ME14, ME16, ME19, ME20 completed cleanly (19:15Z to 22:56Z, ~561k observations delivered per
dispatcher spans). Then ME28 (dense tag, ~11k obs per 2 days) hit a NoConnectionSlot storm at
00:00Z to 00:30Z (1,436 immediate re-triggers in 30 min, 24,573 Cloud Run 429s, instances pegged),
the runner was OOM-killed 144 times between 00:30Z and 01:40Z (512Mi, concurrency 8), and Cloud
Run answered the dying requests with 503 "connection to the instance had an error" (120 of them).
Pub/Sub redelivered those steps while their continuations were already published, so ME28 forked
into 4 to 5 parallel chains (three cursors advancing at once at 02:20Z). Movebank 429s then
counted as attempts: ME28 "abandoned after 6/7/8 attempts" ×6, the counter overflowed to
"finished 10/8" at 01:34Z, and the abandons dispatched SE02 (00:58Z) and TE01 (01:06Z) early, so
three birds ran despite concurrency 1. Delivered anyway: ME28 to 2020-11-18 (+1.1M obs), SE02 to
2021-03-11 (+116k), TE01 to 2023-08-02 (+105k). 54 Movebank 429 retries, 6 abandons, 0 504s,
1 dead-lettered message.
So the 600s timeout removed one duplicate source (504) but not the other (503 from instance
death). Both GUNDI-5916 items (slot backoff with delay, 429 not counted as attempt) and the
step lease are needed; the runner also needs more memory (module `limits` input, default
512Mi) or lower concurrency. Recovery remains cancel + plain re-run from watermarks.

### 2026-11-11 02:42Z resume (runner at 1Gi): chains still fork at pull ticks
Resumed after the cancel drained 12 copies; seeded ME28, SE02, TE01, ME33; concurrency 1.
ME28 ran as one chain for an hour. At the 03:40Z pull tick (1,713 pushes in 2 min, 166 Cloud Run
429 aborts, 16 slot backoffs) the chain forked: steps started 03:42, 03:43 and 03:44 with no
completion in between, and all three logged the identical window ("continued at
2020-11-01T07:34:15, 12288 obs"). No 5xx, no memory kills, no 504s this time. So duplicates can
also appear purely from push retries/publishes under a tick storm; the per-(job, individual)
step lease is the only fix that covers every source. `publish_event` has a 20s timeout and no
client retry; a timeout after a successful publish lands in the step's generic except
(attempts++ and re-trigger) which is another fork path.

### State at 2026-11-11 18:23Z (job idle)
Nothing in flight. Complete: ME04, ME09, ME14, ME16, ME17, ME19, ME20, SE02 (SE02's real data
ended at its 2021-01-08 deployment start; it then scanned empty history to 2016-02 before a
429 abandon). Partial, watermarks kept: ME28 to 2020-10-08 (deployment start 2020-09-22, so
about a month of data left), TE01 to 2023-08-02 (14.8M events; years left), ME33 to 2021-06-19
(ER source now exists). Resume recipe unchanged: same config + cancel=true, then same config
with cancel/restart false and backfill_max_concurrency=1, after the step lease / 429 backoff
land and PRs #36/#37 are deployed.

### 2026-11-13 resume on release-20261112 (PRs #36/#37)
22:37Z 2026-11-12: cancel + plain re-run seeded 3 (ME28, ME33, TE01); the clamp log reported all
three starts pulled up to their first records (earliest 2019-10-06 = TE01). ME28 completed 00:06Z
(forked to two copies at a tick; both finalized, so ME33 and TE01 started together). ME33
completed 05:27Z at its first record ≈2021-04-27. TE01 forked to two copies at ~02:00Z and
both abandoned on empty-reason timeouts at 06:05Z around 2023-05-15. Job idle; 10 of 11 birds
complete; TE01 resumes from its watermark. Forks at pull ticks persist without the step lease.

## Update 2026-11-16: resume run outcome (portal evidence; runner logs pending gcloud login)
Chris cancelled and resumed the job three more times with concurrency 1 (11-11 02:42Z → 4 left,
11-12 22:37Z → 3 left, 11-13 16:19Z → 1 left). All 60 Movebank individuals now have ER sources
(ME33 created 2026-11-11). Only Tawny Eagle 01 (14.8M GPS events) remained; from 11-13 16:19Z to
11-14 06:05Z it ran 397 "continued" steps and sent ~4.62M observations, but the step rate grew
from 7/h to 58/h at concurrency 1, i.e. duplicate chains formed again (likely Pub/Sub duplicate
deliveries of the ~2,700 immediate backoff/retry re-publishes; without a step lease every
duplicate becomes an immortal chain). Movebank 429 retries rose with chain count (0/h → 20/h);
a 429 burst at 06:02–06:05Z exhausted 5 attempts on every chain within ~90s and TE01 was
abandoned 7 times. Nothing has run since. TE01 needs: runner-log check of its last cursor,
then cancel + plain resume (concurrency 1). Root fixes remain GUNDI-5916 (delayed re-trigger
for 429/NoConnectionSlot, attempts not counted on 429) and GUNDI-5927 (execution lease).
Runner logs (11-13 16:19Z → 11-14 06:05Z): TE01 resumed at 2023-05-14 and reached
**2022-12-13T20:51Z** (last `continued` 06:00:59Z). 0 504s, 0 hard-cancels; 101 Movebank 429
retries, 10 abandons ("after 6..12 attempts": the per-individual attempt counter is shared by all
parallel chains, so once chains multiply they abandon each other); 87 distinct window cursors were
processed by two or more chains. Job counter overflowed to 10/1.

### 2026-11-16 23:41Z: TE01 resumed on release-20261116 (PR #38)
Prod revision 00027 (`release-20261116-d47afe6`, timeout 600) carries step numbering with a
Lua claim + 30s heartbeat lease, run ids per seed, delayed re-triggers (15s slot / 45s 429) and
429s not counted as attempts. Chris cancelled (tombstone 23:41:21Z) and resumed at concurrency
1; first `backfill` click still 500s on NoConnectionSlot at a pull tick (GUNDI-5916 AC1, open).
TE01 resumed at its 2022-12-13 watermark; first step 23:45:55Z. ~3 years of deployment left.
First 5 hours on release-20261116 (to 04:48Z 11-17): 54 steps, one chain throughout. Pub/Sub
duplicate deliveries of steps 82 and 86 were caught by the claim ("busy", skipped) — roughly
2% of steps, which explains last week's five chains. Three Movebank 429s took the pause path
with no attempt counted. No abandons, no 504s.
08:04Z 11-17: Pub/Sub pushed three extra copies of step 159 within 32s while the original ran
(all claimed "busy", skipped). Pub/Sub push metrics for the same minutes show ~1,100 pushes
per 5 min rejected by Cloud Run with 429 (no instance) at the :55 pull tick, the chronic
runner-capacity issue from September; the retried pushes include backfill steps, so a step can
be re-pushed while an earlier attempt is already running. The claim absorbs it at 45s per copy.
Duplicate source confirmed NOT to be publisher retries (2 publish errors in 10h, none at the
duplicate times). Copies arrive 10–20s apart and cluster when Cloud Run is returning 429
"no instance" to Pub/Sub: a push that Cloud Run reports as 429 can still reach the app, so
Pub/Sub retries a message the app is already running. Follow-up: have the claim return
"duplicate" immediately when the lease's remaining TTL shows a live holder (PTTL > lease -
heartbeat), and only wait when the lease is decaying; cuts the 45s slot hold per copy to ~0.

### 2026-11-20 15:48Z: TE01 nearly done
~1,400 steps and ~16M observations since the 11-16 resume; cursor at **2019-11-14** (deployment
start 2019-10-07), one chain throughout. Nov 17 22:00Z → Nov 20 15:48Z: 21 duplicate deliveries
dropped by the claim, 15 Movebank 429 pauses, 5 Movebank 503s, and 67 fetch-timeout attempts of
which SEVEN runs reached attempt 5 of 5 and survived only because the sixth try succeeded.
Timeouts-as-attempts is the remaining abandon risk (GUNDI-5916 follow-up proposed: pause on
provider timeout, separate consecutive-timeout cap ~20).

## 2026-11-20 17:56Z: DONE
`job-44ed17250673 finished: 1/1 individuals`. TE01's final step (#1589) returned `completed` with
7,508 observations, reaching its clamped start of 2019-10-06 (first Movebank record); the last
`continued` cursor before it was 2019-10-20T04:02Z, so the first two weeks of the deployment are
sparse. Resume run on release-20261116: 1,589 steps over ~3.8 days, one chain throughout, zero
abandons, zero 504s. All 11 originally missing birds (TE01, ME04/09/14/16/17/19/20/28/33, SE02)
are now backfilled to their first records. Remaining verification: in ER, each subject's
earliest track point vs. its Movebank first record. Open follow-ups: GUNDI-5916 (AC1 top-level
NoConnectionSlot, abandoned-individual reporting, counter bounds; plus provider-timeout pause
with its own cap), GUNDI-5927 (template lease), claim-wait shortening for live holders,
dispatcher erclient read timeout setting, cdip PR #499 (dispatcher memory).
