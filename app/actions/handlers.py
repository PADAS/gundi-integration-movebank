import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import pydantic
from dateutil.parser import parse as parse_date
from movebank_client import MovebankClient

import app.actions.client as client
from app.actions.core import action_title
from app import settings
from app.actions.backfill_queue import BackfillJob
from app.actions.client import IndividualState, generate_individuals
from app.actions.configurations import (
    AuthenticateConfig,
    BackfillConfig,
    BackfillEventsForIndividualConfig,
    PullObservationsConfig,
    PullEventsForIndividualConfig,
)
from app.actions.transform import _ensure_utc, build_observation, chunks
from app.services.action_scheduler import crontab_schedule, trigger_action
from app.services.activity_logger import activity_logger
from app.services.gundi import send_observations_to_gundi
from app.services.movebank_connections import movebank_slot, NoConnectionSlot
from app.services.state import IntegrationStateManager


logger = logging.getLogger(__name__)
state_manager = IntegrationStateManager()

# Ported from the v1 integration — production-proven values.
HIGH_FREQUENCY_INDIVIDUAL_THRESHOLD = 5000  # events; above this, shrink the fetch window
# Per-run cap, checked between windows: once total processed reaches this, no
# further windows are fetched. A single window may overshoot the cap — everything
# fetched in a window is sent so its cursors can advance consistently.
MAXIMUM_RECORDS_PER_INDIVIDUAL = 2000
DEFAULT_BATCH_WINDOW = timedelta(days=5)
QUIET_PERIOD_SECONDS = 3600  # skip an individual for this long after an empty window
OBSERVATIONS_BATCH_SIZE = 200

CURSOR_STATE_ACTION_ID = "pull_events_for_individual"
QUIET_STATE_ACTION_ID = "pull_events_for_individual_quiet"

BACKFILL_ACTION_ID = "backfill_events_for_individual"
BACKFILL_WATERMARK_ACTION_ID = "backfill_watermark"
BACKFILL_PROGRESS_THROTTLE_SECONDS = 300
BACKFILL_MAX_ATTEMPTS = 5
# Per-step backstop for the backfill cascade: once a single invocation's
# observations reach this, it stops taking more windows, persists its scan
# position, and re-triggers — bounding how long one PubSub-ack'd invocation
# can run even if window sizing (below) underestimates event density.
MAX_RECORDS_PER_BACKFILL_STEP = 10000


def _query_start_for_sensor(sensor_type_id: int, sensor_start: datetime, minimum_event_id: int) -> datetime:
    """Accessory-measurements can arrive hours late, so its query re-reads back
    ACCESSORY_SETTLING_HOURS — but only once there's a real prior cursor for
    this sensor (minimum_event_id > 1, i.e. at least one event already
    recorded). Widening a freshly-seeded cursor (minimum_event_id == 1, no
    prior events — e.g. right after _seed_pull_cursor_at_end) would re-read
    [T-settling, T) with minimum_event_id=1 and re-emit duplicates of
    everything already sent in that span. GPS is prompt and always uses its
    exact cursor."""
    if (sensor_type_id == MovebankClient.MOVEBANK_SENSOR_TYPE_LABEL_TO_ID["accessory-measurements"]
            and minimum_event_id > 1):
        return sensor_start - timedelta(hours=settings.ACCESSORY_SETTLING_HOURS)
    return sensor_start


def _compute_batch_window(number_of_events, span_seconds) -> timedelta:
    """Shared window-sizing logic for the steady-state pull and backfill:
    high-frequency individuals get a window sized to hold roughly
    HIGH_FREQUENCY_INDIVIDUAL_THRESHOLD events over the given span, assuming
    an even distribution; falls back to DEFAULT_BATCH_WINDOW otherwise, or
    when the computed window would be non-positive (which would never
    advance the scan position and spin forever)."""
    if (number_of_events or 0) > HIGH_FREQUENCY_INDIVIDUAL_THRESHOLD:
        window = timedelta(seconds=(HIGH_FREQUENCY_INDIVIDUAL_THRESHOLD / number_of_events) * span_seconds)
    else:
        window = DEFAULT_BATCH_WINDOW
    if window <= timedelta(0):
        window = DEFAULT_BATCH_WINDOW
    return window


def _display_name(ind) -> str:
    """Human-facing name for an individual, used as the observation's
    subject_name/source_name. Prefers the tag's local_identifier, falling
    back to the nick_name and then the ring_id."""
    return ind.local_identifier or ind.nick_name or ind.ring_id


def _supported_sensor_type_ids(ind) -> list:
    """Movebank sensor-type labels this individual reports, mapped to the
    numeric sensor_type_ids this integration knows how to handle."""
    labels = [label.strip().lower() for label in ind.sensor_type_ids.split(",")]
    return [
        MovebankClient.MOVEBANK_SENSOR_TYPE_LABEL_TO_ID[label]
        for label in labels
        if label in MovebankClient.MOVEBANK_SENSOR_TYPE_LABEL_TO_ID
    ]


def _advance_watermarks(state, events, sensor_type_ids, sensor_type_timestamps, minimum_event_ids):
    """Per-sensor cursor advancement shared by the steady-state pull and the
    backfill sub-action: regroups a flat event list by each event's own
    sensor_type_id field, then advances the timestamp/event-id cursor for each
    sensor type from what actually came back (event-id cursor still advances
    on unparseable timestamps so junk records aren't refetched forever)."""
    events_by_sensor = {}
    for e in events:
        try:
            events_by_sensor.setdefault(int(e.get("sensor_type_id")), []).append(e)
        except (TypeError, ValueError):
            continue
    for stid in sensor_type_ids:
        se = events_by_sensor.get(stid, [])
        stamps, ids = [], []
        for e in se:
            try:
                stamps.append(_ensure_utc(parse_date(e.get("timestamp"))))
            except Exception:
                pass
            try:
                ids.append(int(e.get("event_id")))
            except (TypeError, ValueError):
                pass
        if stamps or ids:
            new_latest = max(stamps) if stamps else sensor_type_timestamps[stid]
            new_max = max(ids) if ids else minimum_event_ids[stid] - 1
            state.update_sensor_state(stid, new_latest, new_max)
            sensor_type_timestamps[stid] = new_latest
            minimum_event_ids[stid] = new_max + 1


@action_title("(1) Movebank Credentials")
async def action_auth(integration, action_config: AuthenticateConfig):
    logger.info(
        f"Executing auth action with integration {integration} and action_config {action_config}..."
    )
    mb_client = client.MovebankClient(
        base_url=integration.base_url,
        username=action_config.username,
        password=action_config.password.get_secret_value(),
    )

    try:
        token = await mb_client.get_token()
    except client.MBForbiddenError:
        logger.exception(f"Auth unsuccessful for integration {str(integration.id)}. MB returned 403 (wrong credentials)")
        return {"valid_credentials": False, "message": "Invalid credentials"}
    except client.MBClientError as e:
        logger.exception(f"Auth action failed for integration {str(integration.id)}. Exception: {e}")
        return {"error": "An internal error occurred while trying to test credentials. Please try again later."}
    else:
        if token:
            logger.info(f"Auth successful for integration '{integration.name}'. Token: '{token['api-token']}'")
            return {"valid_credentials": True}
        else:
            logger.error(f"Auth unsuccessful for integration {integration}.")
            return {"valid_credentials": False}


@action_title("(2) Read Movebank Data")
@activity_logger()
@crontab_schedule("*/10 * * * *")  # same cadence as the v1 cronjob
async def action_pull_observations(integration, action_config: PullObservationsConfig):
    """List the study's individuals and trigger one sub-action per individual."""
    integration_id = str(integration.id)
    logger.info(f"Pulling observations for study {action_config.study_id}, integration {integration_id}...")

    auth_config = client.get_auth_config(integration)
    mb_client = client.MovebankClient(
        base_url=integration.base_url,
        username=auth_config.username,
        password=auth_config.password.get_secret_value(),
    )
    try:
        async with mb_client as mb:
            async with movebank_slot(auth_config.username):
                individual_rows = await mb.get_individuals_by_study(study_id=action_config.study_id)
    except NoConnectionSlot:
        # Movebank connection budget exhausted (e.g. a backfill is saturating it).
        # This is a scheduled tick — skip cleanly and let the next one retry,
        # rather than surfacing an action failure and triggering no sub-actions.
        logger.info(
            f"Skipping pull for study {action_config.study_id}: Movebank connection budget "
            "exhausted; the next scheduled tick will retry."
        )
        return {"skipped": "no_connection_slot"}

    individuals = list(generate_individuals(individual_rows))
    logger.info(f"{len(individuals)} individuals found for study {action_config.study_id}")

    triggered = 0
    for ind in individuals:
        await trigger_action(
            integration_id=integration_id,
            action_id="pull_events_for_individual",
            config=PullEventsForIndividualConfig(
                study_id=action_config.study_id,
                individual=ind,
                maximum_lookback_hours=action_config.maximum_lookback_hours,
            ),
        )
        triggered += 1

    return {"individuals_found": len(individuals), "sub_actions_triggered": triggered}


@activity_logger()
async def action_pull_events_for_individual(integration, action_config: PullEventsForIndividualConfig):
    """Fetch events for one individual with per-sensor cursors, transform, and send to Gundi.

    Internal sub-action, triggered by pull_observations once per individual.
    """
    ind = action_config.individual
    integration_id = str(integration.id)
    log_reference = f"study:{action_config.study_id},individual:{ind.id},local_identifier:{ind.local_identifier}"

    if await state_manager.get_state(integration_id, QUIET_STATE_ACTION_ID, source_id=ind.id):
        logger.info(f"Skipping individual {log_reference} for quiet period.")
        return {"skipped": "quiet_period"}

    if ind.timestamp_start is None:
        logger.info(f"Skip Movebank {log_reference} for no timestamp_start.")
        return {"skipped": "no_timestamp_start"}

    now = datetime.now(tz=timezone.utc)
    # Some individuals don't provide timestamp_end, so resolve a reasonable value.
    resolved_individual_timestamp_end = ind.timestamp_end or now
    highest_date = min(now, resolved_individual_timestamp_end)

    sensor_type_ids = _supported_sensor_type_ids(ind)
    if not sensor_type_ids:
        logger.info(f"Skip Movebank {log_reference} — no supported sensor types in '{ind.sensor_type_ids}'.")
        return {"skipped": "no_supported_sensors"}

    saved_state = await state_manager.get_state(integration_id, CURSOR_STATE_ACTION_ID, source_id=ind.id)
    try:
        individual_state = IndividualState.parse_obj(saved_state) if saved_state else None
    except pydantic.ValidationError:
        logger.exception(f"Failed parsing saved state for {log_reference}; starting fresh.")
        individual_state = None
    was_fresh = individual_state is None
    if individual_state is None:
        individual_state = IndividualState(
            individual_id=ind.id, study_id=action_config.study_id, local_identifier=ind.local_identifier
        )

    # Build per-sensor cursors from state; new sensors start at the lookback window.
    default_start = resolved_individual_timestamp_end - timedelta(hours=action_config.maximum_lookback_hours)
    if was_fresh:
        # First-ever cursor for this individual: record the oldest point the pull
        # will cover, so a later backfill knows where to stop (fills [start, this)).
        individual_state.coverage_start = default_start
    sensor_type_timestamps = {}
    minimum_event_ids = {}
    for sensor_type_id in sensor_type_ids:
        sensor_state = individual_state.get_sensor_state(sensor_type_id)
        sensor_type_timestamps[sensor_type_id] = sensor_state.latest_timestamp or default_start
        minimum_event_ids[sensor_type_id] = (sensor_state.highest_event_id or 0) + 1

    earliest_start = min(sensor_type_timestamps.values())
    if earliest_start >= resolved_individual_timestamp_end:
        logger.info(f"Skip Movebank {log_reference} for no new data.")
        return {"skipped": "no_new_data"}

    # Size the fetch window by event density (shared with backfill via
    # _compute_batch_window): high-frequency individuals get a window that
    # should hold roughly HIGH_FREQUENCY_INDIVIDUAL_THRESHOLD events, assuming
    # an even distribution over the individual's active range.
    # resolved_individual_timestamp_end falls back to now when the individual
    # omits timestamp_end, so density sizing works for those individuals too.
    total_seconds = (resolved_individual_timestamp_end - ind.timestamp_start).total_seconds()
    batch_window_size = _compute_batch_window(ind.number_of_events, total_seconds)

    logger.info(
        f"For individual {ind.id} ({ind.nick_name}), using a window size of {batch_window_size} "
        f"({ind.number_of_events} events on record)."
    )

    device_name = _display_name(ind)
    auth_config = client.get_auth_config(integration)
    total_observations_sent = 0
    current_window_start = earliest_start

    mb_client = client.MovebankClient(
        base_url=integration.base_url,
        username=auth_config.username,
        password=auth_config.password.get_secret_value(),
    )
    async with mb_client as mb:
        while current_window_start <= highest_date and total_observations_sent < MAXIMUM_RECORDS_PER_INDIVIDUAL:
            end_at = min(highest_date, current_window_start + batch_window_size)

            # One request per sensor type, each from its own cursor.
            # _advance_watermarks regroups the combined events by each event's
            # own sensor_type_id field; since every request here already filters
            # to a single sensor_type_id, Movebank reliably echoes that same id
            # back on every event it returns.
            events = []
            sensor_event_counts = {}  # per-sensor counts for logging + the fetched-any check
            for sensor_type_id in sensor_type_ids:
                sensor_start = sensor_type_timestamps[sensor_type_id]
                if sensor_start > end_at:
                    continue  # this sensor is already past the window
                query_start = _query_start_for_sensor(sensor_type_id, sensor_start, minimum_event_ids[sensor_type_id])
                count = 0
                try:
                    async with movebank_slot(auth_config.username):
                        async for event in mb.get_individual_events_by_time(
                            study_id=action_config.study_id,
                            individual_id=ind.id,
                            timestamp_start=query_start,
                            timestamp_end=end_at,
                            sensor_type_ids=[sensor_type_id],
                            minimum_event_id=minimum_event_ids[sensor_type_id],
                        ):
                            events.append(event)  # single combined list; no per-sensor copy
                            count += 1
                except NoConnectionSlot:
                    # Budget exhausted (e.g. a backfill is saturating it). Prior
                    # windows already persisted their cursors, so skip cleanly and
                    # resume from the saved cursor on the next scheduled tick —
                    # rather than raising a noisy action failure. This window's
                    # partial fetch is discarded (not yet sent) and re-fetched next
                    # tick; the event-id filter dedups.
                    logger.info(
                        f"Skipping {log_reference}: Movebank connection budget exhausted; "
                        "resuming from the saved cursor on the next tick."
                    )
                    return {"skipped": "no_connection_slot", "observations_sent": total_observations_sent}
                sensor_event_counts[sensor_type_id] = count

            if not sensor_event_counts:
                # Every sensor was already past this window: advance without
                # touching the quiet flag or the saved cursor (v1 behavior).
                current_window_start = current_window_start + batch_window_size
                continue

            observations = [
                obs for event in events
                if (obs := build_observation(event=event, device_name=device_name)) is not None
            ]
            for batch in chunks(observations, OBSERVATIONS_BATCH_SIZE):
                await send_observations_to_gundi(observations=batch, integration_id=integration_id)

            # Advance per-sensor cursors from what actually came back.
            _advance_watermarks(individual_state, events, sensor_type_ids, sensor_type_timestamps, minimum_event_ids)

            logger.info(
                f"Processed Movebank data for {log_reference}: {len(observations)} observations, "
                f"events by sensor: {sensor_event_counts}"
            )

            if events:
                # Persist cursors whenever events were processed — sends happened
                # above, and even if the transform dropped every record, advancing
                # keeps unusable events from being refetched on the next run.
                await state_manager.set_state(
                    integration_id,
                    CURSOR_STATE_ACTION_ID,
                    json.loads(individual_state.json()),
                    source_id=ind.id,
                )
            else:
                # No events in this window: back off from this individual for a while.
                await state_manager.set_state(
                    integration_id,
                    QUIET_STATE_ACTION_ID,
                    {"quiet": True},
                    source_id=ind.id,
                    expire=QUIET_PERIOD_SECONDS,
                )

            total_observations_sent += len(observations)
            current_window_start = current_window_start + batch_window_size

    return {"observations_sent": total_observations_sent}


def _resolve_start(start, ind) -> datetime:
    """A concrete datetime for this individual: the operator's date, or for
    'all' the individual's earliest record (falling back to the lookback floor).

    `start` is the validated config value — the literal "all", or a date string
    (BackfillConfig.start). A datetime is also accepted for programmatic callers.
    """
    if isinstance(start, datetime):
        return start
    if isinstance(start, str) and start.strip().lower() != "all":
        return _ensure_utc(parse_date(start))
    # "all"
    if ind.timestamp_start:
        return ind.timestamp_start
    return datetime.now(tz=timezone.utc) - timedelta(days=3650)  # ~10y floor


async def _seed_pull_cursor_at_end(integration_id: str, study_id: str, ind, end: datetime) -> None:
    """Claim [end, +inf) for the steady-state pull cursor before backfill starts.

    Without this, an individual with no existing pull cursor lets the */10
    pull compute its own lookback-based start and reach back into the range
    backfill is about to cover — shipping duplicate observations, since
    loaded_at doesn't dedup them.
    """
    state = IndividualState(individual_id=ind.id, study_id=study_id, local_identifier=ind.local_identifier)
    state.coverage_start = end  # pull owns [end, +inf); everything before is backfill's to fill
    for stid in _supported_sensor_type_ids(ind):
        state.update_sensor_state(stid, end, 0)
    await state_manager.set_state(integration_id, CURSOR_STATE_ACTION_ID, json.loads(state.json()), source_id=ind.id)


@action_title("(3) Backfill Movebank Data for a Study")
@activity_logger()
async def action_backfill(integration, action_config: BackfillConfig):
    integration_id = str(integration.id)
    now = datetime.now(tz=timezone.utc)
    auth_config = client.get_auth_config(integration)
    mb_client = client.MovebankClient(
        base_url=integration.base_url,
        username=auth_config.username,
        password=auth_config.password.get_secret_value(),
    )
    async with mb_client as mb:
        async with movebank_slot(auth_config.username):
            rows = await mb.get_individuals_by_study(study_id=action_config.study_id)
    individuals = list(generate_individuals(rows))
    if action_config.individual_ids:
        wanted = set(action_config.individual_ids)
        individuals = [i for i in individuals if i.id in wanted]

    # Deterministic job id: a redelivered backfill command resumes the same job.
    job_seed = f"{action_config.study_id}:{sorted(i.id for i in individuals)}:{action_config.start}"
    job_id = "job-" + hashlib.sha256(job_seed.encode()).hexdigest()[:12]
    job = BackfillJob(integration_id, job_id)

    # Idempotent seed: a redelivered/double-clicked backfill command hashes to
    # the SAME job_id. If that job is already active, re-seeding would
    # re-zero its counters and re-RPUSH individuals that are already in
    # flight or already dispatched — double-dispatching them. Bail out early.
    if await job.exists():
        snap = await job.snapshot()
        if snap["in_flight"] == 0 and snap["pending_remaining"] > 0:
            # A prior invocation seeded the job (meta + pending) but crashed
            # before/while dispatching — nothing is in flight yet work is still
            # queued. There's no PubSub redelivery to restart it, so a re-run
            # would otherwise return already_active forever. Resume instead:
            # dispatch up to K from the pending queue (configs were stored at
            # seed time; _dispatch_backfill_individual skips any that are missing).
            logger.info(
                f"Backfill {job_id}: resuming stalled job "
                f"({snap['pending_remaining']} pending, none in flight)"
            )
            k = action_config.backfill_max_concurrency or settings.BACKFILL_MAX_CONCURRENCY
            dispatched = 0
            while dispatched < k:
                next_id = await job.next_individual()
                if next_id is None:
                    break
                await _dispatch_backfill_individual(integration_id, job, next_id)
                dispatched += 1
            return {"job_id": job_id, "resumed": True, "dispatched": dispatched}
        logger.info(f"Backfill {job_id}: job already active, skipping re-seed")
        return {"job_id": job_id, "already_active": True}

    # Resolve each individual's backfill end from its steady-state coverage
    # floor. Backfill fills [start, end); the pull owns [end, +inf).
    #   - no cursor            -> end = now, and seed the pull cursor at now
    #   - coverage_start set   -> end = coverage_start (fill history behind the pull)
    #   - legacy seed (all event_id 0, no coverage_start) -> end = its timestamp
    #   - legacy real cursor (event_id > 0, no coverage_start) -> skip (unknown floor)
    ranges = {}
    seed_needed = set()
    skipped_existing = []
    for i in individuals:
        saved = await state_manager.get_state(integration_id, CURSOR_STATE_ACTION_ID, source_id=i.id)
        if not saved:
            end_dt = now
            seed_needed.add(i.id)
        else:
            state = IndividualState.parse_obj(saved)
            if state.coverage_start is not None:
                end_dt = state.coverage_start
            elif all((ss.highest_event_id or 0) == 0 for ss in state.sensor_states.values()):
                stamps = [ss.latest_timestamp for ss in state.sensor_states.values() if ss.latest_timestamp]
                end_dt = min(stamps) if stamps else now
            else:
                skipped_existing.append(i.id)
                continue
            latest = max((ss.latest_timestamp for ss in state.sensor_states.values() if ss.latest_timestamp), default=None)
            has_real_coverage = any((ss.highest_event_id or 0) > 0 for ss in state.sensor_states.values())
            if has_real_coverage and latest and end_dt > latest - timedelta(hours=settings.ACCESSORY_SETTLING_HOURS):
                logger.warning(
                    f"Backfill {job_id}/{i.id}: coverage span is below the accessory settling "
                    "margin; accessory rows near the boundary may duplicate."
                )
        ranges[i.id] = (_resolve_start(action_config.start, i), end_dt)
    if skipped_existing:
        logger.warning(
            f"Backfill {job_id}: skipped {len(skipped_existing)} individuals with existing collection "
            "history but no recorded coverage floor; clear their cursor to force a full backfill."
        )

    # Only individuals with a non-empty range are worth queuing.
    queued = [i for i in individuals if i.id in ranges and ranges[i.id][0] < ranges[i.id][1]]
    range_repr = f"[{action_config.start} .. per-individual coverage floor)"
    await job.seed([i.id for i in queued], total=len(queued), range_repr=range_repr)

    logger.info(f"Backfill {job_id} for study {action_config.study_id}: {len(queued)} individuals, range {range_repr}")

    # Store each queued individual's sub-action config up front, so the rolling
    # dispatch (here and from the sub-action's finalize step) always reads
    # from the same place regardless of when an individual is popped. Also
    # claim the pull cursor's floor for individuals that don't have one yet,
    # so a concurrent steady-state pull run can't reach back into the range
    # backfill is about to cover (see _seed_pull_cursor_at_end).
    for i in queued:
        start_dt, end_dt = ranges[i.id]
        # Only fresh individuals need a boundary-claim seed; an already-cursored
        # individual's cursor already owns [coverage_start, +inf).
        if i.id in seed_needed:
            await _seed_pull_cursor_at_end(integration_id, action_config.study_id, i, end_dt)
        await job.put_individual_config(
            i.id,
            BackfillEventsForIndividualConfig(
                study_id=action_config.study_id, individual=i,
                job_id=job_id, start=start_dt, end=end_dt,
            ).json(),
        )

    k = action_config.backfill_max_concurrency or settings.BACKFILL_MAX_CONCURRENCY
    dispatched = 0
    while dispatched < k:
        next_id = await job.next_individual()
        if next_id is None:
            break
        await _dispatch_backfill_individual(integration_id, job, next_id)
        dispatched += 1

    return {
        "job_id": job_id, "individuals": len(queued), "dispatched": dispatched,
        "skipped_existing": len(skipped_existing),
    }


async def _dispatch_backfill_individual(integration_id, job, individual_id):
    """Read the stored per-individual config and trigger the sub-action.

    Single dispatch path used both for the backfill action's first wave and
    for the sub-action's rolling dispatch-next on completion. If an
    individual's config is missing (e.g. a lost/evicted Redis entry), this
    advances to the next pending individual instead of stalling the driver
    chain — bounded by the queue's length at call time so a fully-missing
    configs hash can't loop forever.
    """
    snap = await job.snapshot()
    max_attempts = snap["pending_remaining"] + 1  # +1 for the individual_id passed in
    for _ in range(max_attempts):
        if individual_id is None:
            return
        blob = await job.get_individual_config(individual_id)
        if blob is not None:
            cfg = BackfillEventsForIndividualConfig.parse_raw(blob)
            await job.incr_in_flight()
            try:
                await trigger_action(integration_id=integration_id, action_id=BACKFILL_ACTION_ID, config=cfg)
            except Exception:
                # Publish failed (e.g. commands topic unset, transient PubSub
                # error). Roll back the increment and return the individual to
                # the queue so it isn't lost — an inflated in_flight would also
                # block the resume path (which keys on in_flight == 0). Re-raise
                # so a persistent failure stays visible; state is now consistent
                # for a later resume.
                await job.decr_in_flight()
                await job.requeue(individual_id)
                raise
            return
        logger.warning(f"Backfill {job.job_id}: no stored config for individual {individual_id}; skipping")
        individual_id = await job.next_individual()
    logger.warning(f"Backfill {job.job_id}: exhausted pending queue looking for a valid config to dispatch")


async def _finalize_backfill_individual(integration_id, job, ind, action_config, *, observations, state=None):
    # Merge the backfill watermark FORWARD into the steady-state pull cursor:
    # per sensor, take max(existing, backfill) on both the timestamp and the
    # highest event id. This carries backfill's event-ids forward (so the
    # pull's accessory settling re-read dedups against them) without ever
    # moving a running pull's cursor backward — action_backfill may already
    # have claimed a forward cursor for this individual at job start (see
    # _seed_pull_cursor_at_end), and a live pull may have advanced it further
    # still while backfill was running.
    if state is not None:
        existing_raw = await state_manager.get_state(integration_id, CURSOR_STATE_ACTION_ID, source_id=ind.id)
        existing_state = IndividualState.parse_obj(existing_raw) if existing_raw else IndividualState(
            individual_id=ind.id, study_id=action_config.study_id, local_identifier=ind.local_identifier
        )
        for stid_str, backfill_ss in state.sensor_states.items():
            stid = int(stid_str)
            existing_ss = existing_state.get_sensor_state(stid)
            candidate_stamps = [t for t in (existing_ss.latest_timestamp, backfill_ss.latest_timestamp) if t]
            if candidate_stamps:
                new_ts = max(candidate_stamps)
                new_event_id = max(existing_ss.highest_event_id or 0, backfill_ss.highest_event_id or 0)
                existing_state.update_sensor_state(stid, new_ts, new_event_id)
        # Backfill has now covered down to action_config.start, so the combined
        # coverage floor moves there — a later backfill with the same/later start
        # will see an empty range for this individual.
        existing_state.coverage_start = action_config.start
        await state_manager.set_state(integration_id, CURSOR_STATE_ACTION_ID,
                                      json.loads(existing_state.json()), source_id=ind.id)
    await job.record_completion(observations)
    await job.decr_in_flight()

    if state is not None:
        # Best-effort watermark cleanup, AFTER the job counters are updated:
        # the watermark has served its purpose (merged into the pull cursor
        # above), but a Redis hiccup here must not leave in_flight un-decremented
        # and the job stuck. A leftover watermark is harmless (this individual
        # is done; a re-run is refused by the idempotent-seed guard). NOT deleted
        # on the abandon path (state is None) so a re-run resumes from it.
        try:
            await state_manager.delete_state(
                integration_id, BACKFILL_WATERMARK_ACTION_ID, source_id=f"{action_config.job_id}.{ind.id}"
            )
        except Exception as exc:
            logger.warning(f"Backfill {action_config.job_id}/{ind.id}: watermark cleanup failed (harmless): {exc}")

    if await job.is_done():
        snap = await job.snapshot()
        logger.info(f"Backfill {action_config.job_id} finished: {snap['completed']}/{snap['total']} "
                    f"individuals, {snap['observations_sent']} observations")
    else:
        if await state_manager.set_if_absent(
            integration_id, f"backfill_progress.{action_config.job_id}", ttl_seconds=BACKFILL_PROGRESS_THROTTLE_SECONDS
        ):
            snap = await job.snapshot()
            logger.info(f"Backfill {action_config.job_id} progress: {snap['completed']}/{snap['total']} "
                        f"individuals, ~{snap['observations_sent']} observations")
        next_id = await job.next_individual()
        if next_id is not None:
            await _dispatch_backfill_individual(integration_id, job, next_id)

    return {"status": "completed", "observations_sent": observations}


@activity_logger()
async def action_backfill_events_for_individual(integration, action_config: BackfillEventsForIndividualConfig):
    ind = action_config.individual
    integration_id = str(integration.id)
    job = BackfillJob(integration_id, action_config.job_id)
    watermark_source = f"{action_config.job_id}.{ind.id}"
    log_reference = f"job:{action_config.job_id},individual:{ind.id}"

    sensor_type_ids = _supported_sensor_type_ids(ind)
    if not sensor_type_ids:
        return await _finalize_backfill_individual(integration_id, job, ind, action_config, observations=0)

    saved = await state_manager.get_state(integration_id, BACKFILL_WATERMARK_ACTION_ID, source_id=watermark_source)
    state = IndividualState.parse_obj(saved) if saved else IndividualState(
        individual_id=ind.id, study_id=action_config.study_id, local_identifier=ind.local_identifier
    )
    sensor_type_timestamps, minimum_event_ids = {}, {}
    for stid in sensor_type_ids:
        ss = state.get_sensor_state(stid)
        sensor_type_timestamps[stid] = ss.latest_timestamp or action_config.start
        minimum_event_ids[stid] = (ss.highest_event_id or 0) + 1

    # The scan position is a DURABLE floor, persisted every window regardless
    # of whether events came back. Windows are anchored to it (below), not to
    # the per-sensor event cursor above — otherwise a sensor that returns
    # nothing for the whole range would never move its own cursor, and a
    # re-trigger after a budget-exhausted step would recompute `current` from
    # that unmoved cursor and rescan the same empty span forever (livelock).
    persisted_scan_from = None
    if saved and saved.get("scan_from"):
        try:
            persisted_scan_from = _ensure_utc(parse_date(saved["scan_from"]))
        except Exception:
            persisted_scan_from = None

    auth_config = client.get_auth_config(integration)
    deadline = time.monotonic() + 0.8 * settings.MAX_ACTION_EXECUTION_TIME
    span_seconds = (action_config.end - action_config.start).total_seconds()
    window = _compute_batch_window(ind.number_of_events, span_seconds)
    if saved and saved.get("window_seconds"):
        try:
            window = timedelta(seconds=float(saved["window_seconds"]))
        except (TypeError, ValueError):
            pass
    min_window = timedelta(seconds=settings.MIN_BACKFILL_WINDOW_SECONDS)
    observations_sent = 0
    current = persisted_scan_from if persisted_scan_from is not None else min(sensor_type_timestamps.values())

    mb_client = client.MovebankClient(
        base_url=integration.base_url, username=auth_config.username,
        password=auth_config.password.get_secret_value(),
    )
    # Only the fetch/send cascade (and its pre-finalize watermark bookkeeping) is
    # guarded here: NoConnectionSlot and Movebank/transform errors originate in
    # this loop. The completion decision below (continue vs. finalize) must stay
    # OUTSIDE the try — if finalize's own post-processing (dispatch-next PubSub
    # publish, is_done, snapshot) raised while wrapped in this try, an already
    # -completed individual would be retried/abandoned and _finalize would run a
    # second time, double-counting record_completion/decr_in_flight.
    try:
        async with mb_client as mb:
            while current < action_config.end and time.monotonic() < deadline:
                end_at = min(action_config.end, current + window)
                # All sensors sweep the same [current, end_at) window together —
                # anchored to the scan floor, not each sensor's own cursor.
                events = []
                window_interrupted = False
                for stid in sensor_type_ids:
                    # Check BETWEEN per-sensor fetches too, not just between
                    # windows: a dense window's fetches alone can exceed the
                    # budget, and an uncaught CancelledError from a killed
                    # task would leak in_flight and leave a permanent data gap
                    # (main.py's PubSub endpoint always acks — there is no
                    # redelivery to recover a killed step).
                    if time.monotonic() >= deadline:
                        window_interrupted = True
                        break
                    query_start = _query_start_for_sensor(stid, current, minimum_event_ids[stid])
                    async with movebank_slot(auth_config.username):
                        async for event in mb.get_individual_events_by_time(
                            study_id=action_config.study_id, individual_id=ind.id,
                            timestamp_start=query_start, timestamp_end=end_at,
                            sensor_type_ids=[stid], minimum_event_id=minimum_event_ids[stid],
                        ):
                            events.append(event)

                # Overflow guard: only SEND a window we can finish within the
                # budget. A FULLY-fetched window (not deadline-interrupted) with
                # more than the per-window cap is discarded — nothing sent,
                # watermarks and scan floor untouched — and the window is shrunk
                # proportionally, persisted, and retried at the same `current`.
                # Never shrink below the floor; at the floor, fall through and
                # send even if over cap (a burst denser than the floor holds).
                if (not window_interrupted
                        and len(events) > settings.MAX_RECORDS_PER_BACKFILL_WINDOW
                        and window > min_window):
                    shrunk = (window.total_seconds()
                              * settings.MAX_RECORDS_PER_BACKFILL_WINDOW / len(events)
                              * settings.BACKFILL_WINDOW_SHRINK_SAFETY)
                    window = max(min_window, timedelta(seconds=shrunk))
                    blob = json.loads(state.json())
                    blob["scan_from"] = current.isoformat()           # unchanged
                    blob["window_seconds"] = window.total_seconds()
                    await state_manager.set_state(integration_id, BACKFILL_WATERMARK_ACTION_ID,
                                                  blob, source_id=watermark_source)
                    logger.info(
                        f"Backfill {log_reference}: window over cap "
                        f"({len(events)} > {settings.MAX_RECORDS_PER_BACKFILL_WINDOW}); "
                        f"shrunk to {window.total_seconds():.0f}s, retrying"
                    )
                    continue
                if (not window_interrupted
                        and len(events) > settings.MAX_RECORDS_PER_BACKFILL_WINDOW):
                    logger.warning(
                        f"Backfill {log_reference}: window at floor "
                        f"({settings.MIN_BACKFILL_WINDOW_SECONDS}s) still over cap "
                        f"({len(events)} events); sending anyway"
                    )

                device_name = _display_name(ind)
                observations = [o for e in events if (o := build_observation(event=e, device_name=device_name)) is not None]
                for batch in chunks(observations, OBSERVATIONS_BATCH_SIZE):
                    await send_observations_to_gundi(observations=batch, integration_id=integration_id)

                _advance_watermarks(state, events, sensor_type_ids, sensor_type_timestamps, minimum_event_ids)
                observations_sent += len(observations)
                if not window_interrupted:
                    # Only advance past this window if EVERY sensor was fetched
                    # for it; an interrupted window is retried in full next
                    # step (safe: per-sensor minimum_event_id already advanced
                    # for whichever sensors DID get fetched, so no duplicates).
                    current = current + window

                # Persist the scan floor together with the sensor states in a
                # single call, every window, regardless of whether events came back.
                blob = json.loads(state.json())
                blob["scan_from"] = current.isoformat()
                blob["window_seconds"] = window.total_seconds()
                await state_manager.set_state(integration_id, BACKFILL_WATERMARK_ACTION_ID,
                                              blob, source_id=watermark_source)

                # Per-step backstop: a dense window can return far more than
                # expected even with density-based sizing. Stop taking more
                # windows once this step's total crosses the cap, rather than
                # risking the invocation running past its execution budget.
                if window_interrupted or observations_sent >= MAX_RECORDS_PER_BACKFILL_STEP:
                    break
    except asyncio.CancelledError:
        # Hard execution-timeout: asyncio.wait_for in the action runner cancels
        # this task, raising CancelledError (a BaseException — the handlers below
        # do NOT catch it). scan_from + window_seconds are persisted every
        # window, so no data is lost; a later trigger resumes from the last
        # completed window. We do not attempt async recovery here (re-trigger /
        # in_flight unwind): awaits during cancellation are themselves cancelled
        # and unreliable. Recover a job wedged by a timeout with restart=true.
        logger.warning(
            f"Backfill {log_reference}: step hard-cancelled at scan_from={current.isoformat()}; "
            "resume on next trigger or re-run with restart=true"
        )
        raise
    except NoConnectionSlot:
        # No connection slot available: re-trigger THIS individual (same in-flight
        # unit continues) rather than losing the step or double-counting in-flight.
        await trigger_action(integration_id=integration_id, action_id=BACKFILL_ACTION_ID, config=action_config)
        logger.info(f"Backfill {action_config.job_id}/{ind.id}: no connection slot, backing off")
        return {"status": "backoff"}
    except Exception as exc:
        attempts = await job.incr_attempts(ind.id)
        if attempts <= BACKFILL_MAX_ATTEMPTS:
            await trigger_action(integration_id=integration_id, action_id=BACKFILL_ACTION_ID, config=action_config)
            logger.info(f"Backfill {action_config.job_id}/{ind.id}: attempt {attempts} failed ({exc}); retrying")
            return {"status": "retry", "attempts": attempts}
        logger.warning(f"Backfill {action_config.job_id}/{ind.id}: abandoned after {attempts} attempts: {exc}")
        result = await _finalize_backfill_individual(integration_id, job, ind, action_config, observations=0)
        return {"status": "abandoned", **{k: v for k, v in result.items() if k != "status"}}

    # A successful step (fetch/send loop completed without raising) resets the
    # attempts counter — so transient errors spread thinly across a long
    # cascade don't accumulate toward abandonment; only CONSECUTIVE failures
    # count against BACKFILL_MAX_ATTEMPTS.
    await job.reset_attempts(ind.id)

    if current < action_config.end:
        # Budget exhausted mid-range: continue THIS individual on the next step.
        await trigger_action(
            integration_id=integration_id, action_id=BACKFILL_ACTION_ID, config=action_config,
        )
        logger.info(f"Backfill {log_reference} continued at {current.isoformat()} ({observations_sent} obs this step)")
        return {"status": "continued", "observations_sent": observations_sent}

    return await _finalize_backfill_individual(
        integration_id, job, ind, action_config, observations=observations_sent, state=state
    )
