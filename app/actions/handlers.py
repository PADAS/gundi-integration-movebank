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


def _query_start_for_sensor(sensor_type_id: int, sensor_start: datetime) -> datetime:
    """Accessory-measurements can arrive hours late, so its query re-reads back
    ACCESSORY_SETTLING_HOURS; GPS is prompt and uses its exact cursor."""
    if sensor_type_id == MovebankClient.MOVEBANK_SENSOR_TYPE_LABEL_TO_ID["accessory-measurements"]:
        return sensor_start - timedelta(hours=settings.ACCESSORY_SETTLING_HOURS)
    return sensor_start


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
    async with mb_client as mb:
        async with movebank_slot(auth_config.username):
            individual_rows = await mb.get_individuals_by_study(study_id=action_config.study_id)

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
    if individual_state is None:
        individual_state = IndividualState(
            individual_id=ind.id, study_id=action_config.study_id, local_identifier=ind.local_identifier
        )

    # Build per-sensor cursors from state; new sensors start at the lookback window.
    default_start = resolved_individual_timestamp_end - timedelta(hours=action_config.maximum_lookback_hours)
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

    # Size the fetch window by event density: high-frequency individuals get a
    # window that should hold roughly HIGH_FREQUENCY_INDIVIDUAL_THRESHOLD events,
    # assuming an even distribution over the individual's active range.
    if (ind.number_of_events or 0) > HIGH_FREQUENCY_INDIVIDUAL_THRESHOLD:
        # resolved_individual_timestamp_end falls back to now when the individual
        # omits timestamp_end, so density sizing works for those individuals too.
        total_seconds = (resolved_individual_timestamp_end - ind.timestamp_start).total_seconds()
        batch_window_size = timedelta(
            seconds=(HIGH_FREQUENCY_INDIVIDUAL_THRESHOLD / ind.number_of_events) * total_seconds
        )
    else:
        batch_window_size = DEFAULT_BATCH_WINDOW
    if batch_window_size <= timedelta(0):
        # A zero/negative window (timestamp_end <= timestamp_start) would never
        # advance current_window_start and spin the fetch loop forever.
        batch_window_size = DEFAULT_BATCH_WINDOW

    logger.info(
        f"For individual {ind.id} ({ind.nick_name}), using a window size of {batch_window_size} "
        f"({ind.number_of_events} events on record)."
    )

    device_name = ind.nick_name or ind.local_identifier or ind.ring_id
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
            events_by_sensor = {}
            for sensor_type_id in sensor_type_ids:
                sensor_start = sensor_type_timestamps[sensor_type_id]
                if sensor_start > end_at:
                    continue  # this sensor is already past the window
                query_start = _query_start_for_sensor(sensor_type_id, sensor_start)
                sensor_events = []
                async with movebank_slot(auth_config.username):
                    async for event in mb.get_individual_events_by_time(
                        study_id=action_config.study_id,
                        individual_id=ind.id,
                        timestamp_start=query_start,
                        timestamp_end=end_at,
                        sensor_type_ids=[sensor_type_id],
                        minimum_event_id=minimum_event_ids[sensor_type_id],
                    ):
                        sensor_events.append(event)
                events_by_sensor[sensor_type_id] = sensor_events
                events.extend(sensor_events)

            if not events_by_sensor:
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
                f"events by sensor: { {k: len(v) for k, v in events_by_sensor.items()} }"
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
    'all' the individual's earliest record (falling back to the lookback floor)."""
    if isinstance(start, datetime):
        return start
    # start == "all"
    if ind.timestamp_start:
        return ind.timestamp_start
    return datetime.now(tz=timezone.utc) - timedelta(days=3650)  # ~10y floor


async def _resolve_end(integration_id: str, ind, now: datetime) -> tuple:
    """Freeze the boundary where backfill meets the steady-state pull: the
    individual's existing pull cursor if any, else now.

    Returns (end, had_existing_cursor). When there was no existing cursor,
    action_backfill claims it itself (seeds it to `now`, see
    _seed_pull_cursor_at_end) so a concurrent steady-state pull run can't
    compute its own lookback-based start and reach back into the range
    backfill is about to cover.
    """
    saved = await state_manager.get_state(integration_id, CURSOR_STATE_ACTION_ID, source_id=ind.id)
    if saved:
        state = IndividualState.parse_obj(saved)
        stamps = [s.latest_timestamp for s in state.sensor_states.values() if s.latest_timestamp]
        if stamps:
            return min(stamps), True
    return now, False


async def _seed_pull_cursor_at_end(integration_id: str, study_id: str, ind, end: datetime) -> None:
    """Claim [end, +inf) for the steady-state pull cursor before backfill starts.

    Without this, an individual with no existing pull cursor lets the */10
    pull compute its own lookback-based start and reach back into the range
    backfill is about to cover — shipping duplicate observations, since
    loaded_at doesn't dedup them.
    """
    state = IndividualState(individual_id=ind.id, study_id=study_id, local_identifier=ind.local_identifier)
    for stid in _supported_sensor_type_ids(ind):
        state.update_sensor_state(stid, end, 0)
    await state_manager.set_state(integration_id, CURSOR_STATE_ACTION_ID, json.loads(state.json()), source_id=ind.id)


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

    ranges = {}
    had_cursor = {}
    for i in individuals:
        end_dt, cursor_existed = await _resolve_end(integration_id, i, now)
        ranges[i.id] = (_resolve_start(action_config.start, i), end_dt)
        had_cursor[i.id] = cursor_existed

    # Only individuals with a non-empty range are worth queuing.
    queued = [i for i in individuals if ranges[i.id][0] < ranges[i.id][1]]
    range_repr = f"[{action_config.start} .. {now.isoformat()})"
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
        if not had_cursor[i.id]:
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

    return {"job_id": job_id, "individuals": len(queued), "dispatched": dispatched}


async def _dispatch_backfill_individual(integration_id, job, individual_id):
    """Read the stored per-individual config and trigger the sub-action.

    Single dispatch path used both for the backfill action's first wave and
    for the sub-action's rolling dispatch-next on completion.
    """
    blob = await job.get_individual_config(individual_id)
    if blob is None:
        logger.warning(f"Backfill {job.job_id}: no stored config for individual {individual_id}; skipping")
        return
    cfg = BackfillEventsForIndividualConfig.parse_raw(blob)
    await job.incr_in_flight()
    await trigger_action(integration_id=integration_id, action_id=BACKFILL_ACTION_ID, config=cfg)


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
        await state_manager.set_state(integration_id, CURSOR_STATE_ACTION_ID,
                                      json.loads(existing_state.json()), source_id=ind.id)
    await job.record_completion(observations)
    await job.decr_in_flight()

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
    window = DEFAULT_BATCH_WINDOW
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
                for stid in sensor_type_ids:
                    query_start = _query_start_for_sensor(stid, current)
                    async with movebank_slot(auth_config.username):
                        async for event in mb.get_individual_events_by_time(
                            study_id=action_config.study_id, individual_id=ind.id,
                            timestamp_start=query_start, timestamp_end=end_at,
                            sensor_type_ids=[stid], minimum_event_id=minimum_event_ids[stid],
                        ):
                            events.append(event)

                device_name = ind.nick_name or ind.local_identifier or ind.ring_id
                observations = [o for e in events if (o := build_observation(event=e, device_name=device_name)) is not None]
                for batch in chunks(observations, OBSERVATIONS_BATCH_SIZE):
                    await send_observations_to_gundi(observations=batch, integration_id=integration_id)

                _advance_watermarks(state, events, sensor_type_ids, sensor_type_timestamps, minimum_event_ids)
                observations_sent += len(observations)
                current = current + window

                # Persist the scan floor together with the sensor states in a
                # single call, every window, regardless of whether events came back.
                blob = json.loads(state.json())
                blob["scan_from"] = current.isoformat()
                await state_manager.set_state(integration_id, BACKFILL_WATERMARK_ACTION_ID,
                                              blob, source_id=watermark_source)
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
